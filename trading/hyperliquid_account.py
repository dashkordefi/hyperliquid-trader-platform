"""
Hyperliquid Account Manager
Управление аккаунтом на Hyperliquid: депозиты, выводы и торговля
"""

import os
import time
from typing import Optional, Dict, Any, List
from collections.abc import Mapping
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from eth_account import Account
from web3 import Web3
import requests
from hyperliquid.api import API
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from hyperliquid.utils.signing import (
    get_timestamp_ms,
    sign_l1_action,
    sign_usd_class_transfer_action,
    sign_withdraw_from_bridge_action,
    sign_spot_transfer_action,
)


def _hyperunit_request_headers() -> dict[str, str]:
    """Заголовки для публичного API Unit (hyperunit.xyz). Без User-Agent часть CDN/WAF отвечает 403."""
    return {
        "User-Agent": "HyperliquidTraderPlatform/1.0 (+https://github.com/dashkordefi/hyperliquid-trader-platform)",
        "Accept": "application/json",
    }


def sanitize_spot_meta(spot_meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    В spotMeta иногда есть пары с индексами токенов за пределами len(tokens)
    (актуально для testnet). Официальный SDK в Info.__init__ делает tokens[base] и падает
    с IndexError: list index out of range.
    """
    if not isinstance(spot_meta, dict):
        return spot_meta
    tokens = spot_meta.get("tokens")
    if not isinstance(tokens, list):
        return spot_meta
    n = len(tokens)
    uni = spot_meta.get("universe")
    if not isinstance(uni, list):
        return spot_meta
    filtered: List[dict[str, Any]] = []
    for entry in uni:
        if not isinstance(entry, dict):
            continue
        t = entry.get("tokens")
        if not isinstance(t, (list, tuple)) or len(t) < 2:
            continue
        b, q = t[0], t[1]
        if isinstance(b, int) and isinstance(q, int) and 0 <= b < n and 0 <= q < n:
            filtered.append(entry)
    out = dict(spot_meta)
    out["universe"] = filtered
    return out


def _evm_tx_fee_params(web3: Web3) -> Dict[str, Any]:
    """
    Arbitrum / Ethereum post-London: EIP-1559. На L2 base fee скачет между блоками;
    слабый запас даёт «max fee per gas less than block base fee».
    """
    block = web3.eth.get_block("latest")
    base_fee = getattr(block, "baseFeePerGas", None)
    if base_fee is None and isinstance(block, dict):
        base_fee = block.get("baseFeePerGas")
    if base_fee is None:
        return {"gasPrice": web3.eth.gas_price}
    try:
        priority = web3.eth.max_priority_fee
    except Exception:
        priority = Web3.to_wei(0.1, "gwei")
    # Минимум 0.05 gwei приоритета — иначе на части RPC priority=0
    min_prio = Web3.to_wei(0.05, "gwei")
    if priority < min_prio:
        priority = min_prio
    # Сильный запас: до 3x текущего base + приоритет (Arbitrum часто +10–30% к base между блоками)
    max_fee = int(base_fee * 3 + priority * 2)
    # Нода часто отдаёт безопасную верхнюю границу — не опускаемся ниже неё
    try:
        gp = int(web3.eth.gas_price)
        if gp > max_fee:
            max_fee = gp
    except Exception:
        pass
    # Ещё один нижний порог относительно base (на случай странного gas_price)
    floor = int(base_fee * 2 + priority)
    if max_fee < floor:
        max_fee = floor
    return {
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
    }


class HyperliquidAccount:
    """Класс для управления аккаунтом на Hyperliquid"""
    
    def __init__(
        self,
        private_key: Optional[str] = None,
        base_url: str = constants.MAINNET_API_URL,
        testnet: bool = False
    ):
        """
        Инициализация аккаунта Hyperliquid
        
        Args:
            private_key: Приватный ключ Ethereum кошелька (hex строка с 0x или без).
                        Если не указан, будет попытка взять из переменной окружения HYPERLIQUID_PRIVATE_KEY
            base_url: URL API (по умолчанию mainnet)
            testnet: Использовать testnet (True) или mainnet (False)
        """
        # Получаем приватный ключ из параметра или переменной окружения
        if private_key is None:
            private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY")
            if private_key is None:
                raise ValueError(
                    "Приватный ключ не указан. Укажите его в параметре private_key, например:\n"
                    "  account = HyperliquidAccount(private_key='ваш_приватный_ключ', testnet=False)"
                )
        
        # Удаляем 0x если есть
        if private_key.startswith('0x'):
            private_key = private_key[2:]
        
        self.private_key = private_key
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        
        # Настройка сети
        if testnet:
            self.base_url = constants.TESTNET_API_URL
            self.hyperliquid_chain = "Testnet"
            self.signature_chain_id = "0x66eee"  # Arbitrum Sepolia
            self.bridge_address = "0x08cfc1B6b2dCF36A1480b99353A354AA8AC56f89"
        else:
            self.base_url = constants.MAINNET_API_URL
            self.hyperliquid_chain = "Mainnet"
            self.signature_chain_id = "0xa4b1"  # Arbitrum One
            self.bridge_address = "0x2df1c51e09aecf9cacb7bc98cb1742757f163df7"
        
        api = API(self.base_url)
        try:
            spot_raw = api.post("/info", {"type": "spotMeta"})
        except Exception as e:
            raise RuntimeError(f"Не удалось загрузить spotMeta: {e}") from e
        if not isinstance(spot_raw, dict):
            raise RuntimeError("spotMeta: неожиданный ответ API")
        spot_meta_safe = sanitize_spot_meta(spot_raw)

        self.info = Info(self.base_url, skip_ws=True, spot_meta=spot_meta_safe)
        self.vault_address = None
        self.expires_after = None
        self.exchange = Exchange(self.account, self.base_url, spot_meta=spot_meta_safe)
        self.exchange.vault_address = self.vault_address
        self.exchange.expires_after = self.expires_after
        self._tick_size_cache = {}
        self._spot_aliases = {}

        print(f"Аккаунт инициализирован: {self.address}")
        print(f"Сеть: {self.hyperliquid_chain}")

    def _evm_private_key_hex(self) -> str:
        """Приватный ключ для web3/eth_account (обязателен префикс 0x)."""
        p = self.private_key
        if not isinstance(p, str):
            p = str(p)
        return p if p.startswith("0x") else "0x" + p

    @staticmethod
    def _send_signed_raw_tx(web3: Web3, signed_tx: Any) -> Any:
        """Совместимость web3 v5/v6: rawTransaction vs raw_transaction."""
        raw = getattr(signed_tx, "raw_transaction", None) or getattr(
            signed_tx, "rawTransaction", None
        )
        if raw is None:
            raise RuntimeError("После подписи нет raw-транзакции (проверьте web3/eth_account).")
        return web3.eth.send_raw_transaction(raw)

    def _build_exchange_request_body(
        self, action: Dict[str, Any], nonce: int, signature: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Собрать тело запроса для /exchange без None значений.
        """
        request_body = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
        }
        if self.vault_address is not None:
            request_body["vaultAddress"] = self.vault_address
        if self.expires_after is not None:
            request_body["expiresAfter"] = self.expires_after
        return request_body
    
    def get_account_info(self) -> Dict[str, Any]:
        """
        Получить информацию об аккаунте
        
        Returns:
            Словарь с информацией об аккаунте
        """
        try:
            user_state = self.info.user_state(self.address)
            return user_state
        except Exception as e:
            print(f"Ошибка при получении информации об аккаунте: {e}")
            return {}
    
    def get_meta(self) -> Dict[str, Any]:
        """
        Получить метаданные о доступных активах
        
        Returns:
            Метаданные о рынках
        """
        try:
            meta = self.info.meta()
            return meta
        except Exception as e:
            print(f"Ошибка при получении метаданных: {e}")
            return {}

    def get_asset_id(self, coin: str) -> Optional[int]:
        """
        Получить ID актива по названию монеты
        
        Args:
            coin: Название монеты (например, "BTC")
            
        Returns:
            ID актива или None
        """
        if coin not in self.info.name_to_coin and "/" in coin:
            self._refresh_spot_mapping()

        if coin in self.info.name_to_coin:
            mapped = self.info.name_to_coin[coin]
            return self.info.coin_to_asset.get(mapped)

        if "/" in coin:
            normalized = self._normalize_spot_key(coin)
            mapped = self._spot_aliases.get(normalized)
            if mapped:
                return self.info.coin_to_asset.get(mapped)

        meta = self.get_meta()
        if not meta:
            return None
        
        universe = meta.get("universe", [])
        for idx, asset in enumerate(universe):
            if asset.get("name") == coin:
                return idx
        
        return None

    def _refresh_spot_mapping(self) -> None:
        try:
            spot_meta = self.info.spot_meta()
            if isinstance(spot_meta, dict):
                spot_meta = sanitize_spot_meta(spot_meta)
        except Exception as e:
            print(f"Ошибка при получении spot meta: {e}")
            return

        for spot_info in spot_meta.get("universe", []):
            asset = spot_info["index"] + 10000
            name = spot_info["name"]
            self.info.coin_to_asset[name] = asset
            self.info.name_to_coin[name] = name
            base, quote = spot_info["tokens"]
            base_info = spot_meta["tokens"][base]
            quote_info = spot_meta["tokens"][quote]
            self.info.asset_to_sz_decimals[asset] = base_info["szDecimals"]
            pair_name = f'{base_info["name"]}/{quote_info["name"]}'
            if pair_name not in self.info.name_to_coin:
                self.info.name_to_coin[pair_name] = name

            self._spot_aliases[self._normalize_spot_key(name)] = name
            self._spot_aliases[self._normalize_spot_key(pair_name)] = name
            self._spot_aliases[self._normalize_spot_key(f'{base_info["name"]}-{quote_info["name"]}')] = name
            self._spot_aliases[self._normalize_spot_key(f'{base_info["name"]}{quote_info["name"]}')] = name

    @staticmethod
    def _normalize_spot_key(value: str) -> str:
        return value.replace("/", "").replace("-", "").replace("_", "").replace(" ", "").upper()

    def _get_spot_token_info(self, symbol: str, aliases: Optional[List[str]] = None) -> Dict[str, Any]:
        search_names = [symbol] + (aliases or [])
        search_set = {name.upper() for name in search_names if name}

        try:
            spot_meta = self.info.spot_meta()
            if isinstance(spot_meta, dict):
                spot_meta = sanitize_spot_meta(spot_meta)
        except Exception as e:
            spot_meta = None
            spot_meta_error = e
        else:
            spot_meta_error = None

        if spot_meta:
            tokens = spot_meta.get("tokens", [])
            if isinstance(tokens, dict):
                tokens_iter = tokens.values()
            else:
                tokens_iter = tokens

            for token in tokens_iter:
                name = str(token.get("name", "")).upper()
                if name in search_set:
                    return token

        token = self._get_spot_token_info_from_meta_and_ctxs(search_set)
        if token:
            return token

        if spot_meta_error is not None:
            raise Exception(f"Ошибка при получении spot meta: {spot_meta_error}")
        raise ValueError(f"Не найден spot токен: {symbol}")

    def _get_spot_token_info_from_meta_and_ctxs(self, search_set: set) -> Optional[Dict[str, Any]]:
        payload = {"type": "spotMetaAndAssetCtxs"}
        response = requests.post(
            f"{self.base_url}/info",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30
        )
        try:
            result = response.json()
        except ValueError:
            return None

        if not isinstance(result, list) or not result:
            return None

        tokens = result[0]
        if not isinstance(tokens, list):
            return None

        for token in tokens:
            name = str(token.get("name", "")).upper()
            if name in search_set:
                return token

        return None
    
    def get_eth_deposit_address(self) -> Dict[str, Any]:
        """
        Получить адрес Ethereum для прямого пополнения ETH

        ВАЖНО:
        - Это адрес для прямого депозита ETH через Unit (Hyperliquid)
        - Отправляйте ETH на этот адрес для пополнения аккаунта
        - Адрес уникален для каждого аккаунта

        Returns:
            Словарь с адресом Ethereum и подписями:
            {
                "address": "0x...",  # Ethereum адрес для пополнения
                "signatures": {...}, # Подписи для верификации
                "status": "OK"
            }
        """
        try:
            base_url = (
                "https://api.hyperunit-testnet.xyz"
                if self.hyperliquid_chain == "Testnet"
                else "https://api.hyperunit.xyz"
            )
            api_url = f"{base_url}/gen/ethereum/hyperliquid/eth/{self.address}"

            response = requests.get(
                api_url, timeout=30, headers=_hyperunit_request_headers()
            )
            response.raise_for_status()

            result = response.json()

            if result.get("status") == "OK":
                eth_address = result.get("address")
                signatures = result.get("signatures", {})

                print("✅ Адрес Ethereum для пополнения получен:")
                print(f"   Адрес: {eth_address}")
                print(f"   Статус: {result.get('status')}")
                print("\n💡 Отправьте ETH на этот адрес для пополнения аккаунта")
                print(f"   Адрес аккаунта Hyperliquid: {self.address}")

                return {
                    "success": True,
                    "address": eth_address,
                    "signatures": signatures,
                    "status": result.get("status"),
                    "account_address": self.address
                }
            else:
                error_msg = result.get("error", "Неизвестная ошибка")
                raise Exception(f"Ошибка при получении адреса: {error_msg}")

        except requests.exceptions.RequestException as e:
            raise Exception(f"Ошибка при запросе к API: {e}")
        except Exception as e:
            raise Exception(f"Ошибка при получении адреса Ethereum: {e}")

    def deposit_eth(
        self,
        amount: float,
        web3: Web3,
        from_address: Optional[str] = None,
        gas: Optional[int] = None,
        gas_price: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Депозит ETH через Unit-адрес (Ethereum -> Hyperliquid)

        ВАЖНО:
        - Отправка идет на адрес, полученный через get_eth_deposit_address()
        - Минимальная сумма депозита ETH может быть ограничена Unit (например 0.007 ETH)
        - Требуется Web3, подключенный к сети Ethereum (mainnet/testnet)

        Args:
            amount: Сумма ETH для депозита
            web3: Web3 экземпляр, подключенный к Ethereum RPC
            from_address: Адрес отправителя (по умолчанию адрес аккаунта)
            gas: Лимит газа (если None, будет оценен)
            gas_price: Цена газа (если None, будет использована текущая)

        Returns:
            Результат транзакции
        """
        if amount <= 0:
            raise ValueError("Сумма депозита должна быть больше 0")

        min_eth = 0.007
        if amount < min_eth:
            raise ValueError(f"Минимальная сумма депозита ETH ~ {min_eth} ETH")

        if from_address is None:
            from_address = self.address

        if from_address.lower() != self.address.lower():
            raise ValueError("from_address должен совпадать с адресом аккаунта")

        deposit_info = self.get_eth_deposit_address()
        deposit_address = deposit_info.get("address")
        if not deposit_address:
            raise Exception("Не удалось получить адрес для депозита ETH")

        value_wei = Web3.to_wei(amount, "ether")

        tx = {
            "from": from_address,
            "to": Web3.to_checksum_address(deposit_address),
            "value": value_wei,
            "nonce": web3.eth.get_transaction_count(from_address),
            "chainId": web3.eth.chain_id
        }

        if gas is None:
            gas = web3.eth.estimate_gas(tx)
        tx["gas"] = gas

        if gas_price is not None:
            tx["gasPrice"] = gas_price
        else:
            tx.update(_evm_tx_fee_params(web3))

        signed_tx = web3.eth.account.sign_transaction(
            tx, private_key=self._evm_private_key_hex()
        )
        tx_hash = self._send_signed_raw_tx(web3, signed_tx)

        print(f"Транзакция депозита ETH отправлена: {tx_hash.hex()}")
        print("Ожидание подтверждения...")

        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        if receipt.status == 1:
            print(f"Депозит ETH успешно выполнен! Транзакция: {tx_hash.hex()}")
            print("Средства будут зачислены на аккаунт в течение нескольких минут")
            return {
                "success": True,
                "tx_hash": tx_hash.hex(),
                "amount": amount,
                "token": "ETH",
                "deposit_address": deposit_address
            }
        else:
            raise Exception("Транзакция не была подтверждена")
    
    def deposit_via_bridge(
        self,
        amount: float,
        web3: Web3,
        usdc_contract_address: str = None,
        token: str = "USDC"
    ) -> Dict[str, Any]:
        """
        Депозит токенов через bridge контракт на Arbitrum
        
        ВАЖНО: 
        - Минимальная сумма депозита - 5 USDC (для USDC)
        - Bridge поддерживает только USDC на данный момент
        - BTC и другие активы можно получить через торговлю на платформе
        
        Args:
            amount: Сумма токенов для депозита
            web3: Web3 экземпляр, подключенный к Arbitrum
            usdc_contract_address: Адрес USDC контракта на Arbitrum
                                  Mainnet: 0xaf88d065e77c8cC2239327C5EDb3A432268e5831
                                  Testnet: 0x1baAbB04529D43a73232B713C0FE471f7c7334d5
            token: Тип токена (по умолчанию "USDC"). 
                   ВАЖНО: Bridge поддерживает только USDC. 
                   Для BTC используйте торговлю на платформе или другие механизмы.
        
        Returns:
            Результат транзакции
        """
        # Проверка поддерживаемых токенов
        if token.upper() != "USDC":
            raise ValueError(
                f"Bridge поддерживает только USDC. Получен токен: {token}.\n"
                "Для BTC и других активов:\n"
                "1. Депозитируйте USDC через bridge\n"
                "2. Торгуйте на платформе, чтобы получить BTC или другие активы\n"
                "3. Или используйте другие механизмы депозита, если они доступны"
            )
        
        if amount < 5:
            raise ValueError("Минимальная сумма депозита - 5 USDC")
        
        if usdc_contract_address is None:
            if self.hyperliquid_chain == "Mainnet":
                usdc_contract_address = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
            else:
                usdc_contract_address = "0x1baAbB04529D43a73232B713C0FE471f7c7334d5"
        
        # Получаем контракт USDC
        usdc_abi = [
            {
                "constant": False,
                "inputs": [
                    {"name": "_to", "type": "address"},
                    {"name": "_value", "type": "uint256"}
                ],
                "name": "transfer",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [],
                "name": "decimals",
                "outputs": [{"name": "", "type": "uint8"}],
                "type": "function"
            }
        ]
        
        usdc_contract = web3.eth.contract(
            address=Web3.to_checksum_address(usdc_contract_address),
            abi=usdc_abi
        )
        
        # Конвертируем сумму в wei (USDC имеет 6 decimals)
        decimals = usdc_contract.functions.decimals().call()
        amount_wei = int(amount * (10 ** decimals))
        
        # Проверяем баланс
        balance = usdc_contract.functions.balanceOf(self.address).call()
        if balance < amount_wei:
            raise ValueError(f"Недостаточно USDC. Баланс: {balance / (10 ** decimals):.2f}, требуется: {amount:.2f}")
        
        # EIP-1559 (Arbitrum): не использовать один gasPrice — иначе maxFeePerGas < base fee
        fee = _evm_tx_fee_params(web3)
        tx = usdc_contract.functions.transfer(
            Web3.to_checksum_address(self.bridge_address),
            amount_wei
        ).build_transaction(
            {
                "from": self.address,
                "nonce": web3.eth.get_transaction_count(self.address),
                "gas": 200000,
                "chainId": web3.eth.chain_id,
                **fee,
            }
        )

        signed_tx = web3.eth.account.sign_transaction(
            tx, private_key=self._evm_private_key_hex()
        )
        tx_hash = self._send_signed_raw_tx(web3, signed_tx)
        
        print(f"Транзакция депозита отправлена: {tx_hash.hex()}")
        print("Ожидание подтверждения...")
        
        # Ждем подтверждения
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        
        if receipt.status == 1:
            print(f"Депозит USDC успешно выполнен! Транзакция: {tx_hash.hex()}")
            print("Средства будут зачислены на аккаунт в течение 1 минуты")
            print("\n💡 Для получения BTC:")
            print("   1. Средства зачислены в USDC")
            print("   2. Используйте place_order() для покупки BTC через торговлю")
            print("   3. Или используйте другие доступные механизмы депозита")
            return {
                "success": True,
                "tx_hash": tx_hash.hex(),
                "amount": amount,
                "token": "USDC",
                "note": "Для BTC используйте торговлю на платформе после депозита USDC"
            }
        else:
            raise Exception("Транзакция не была подтверждена")
    
    def withdraw(
        self,
        amount: float,
        destination: str = None
    ) -> Dict[str, Any]:
        """
        Вывод USDC с аккаунта
        
        ВАЖНО: Комиссия за вывод - $1
        
        Args:
            amount: Сумма USDC для вывода
            destination: Адрес получателя (по умолчанию адрес аккаунта)
        
        Returns:
            Результат операции
        """
        if amount <= 0:
            raise ValueError("Сумма вывода должна быть больше 0")
        if destination is None:
            destination = self.address
        
        # Получаем доступный баланс для вывода
        account_info = self.get_account_info()
        if not account_info:
            raise Exception("Не удалось получить информацию об аккаунте")
        
        margin_summary = account_info.get("marginSummary", {})
        withdrawable_raw = account_info.get("withdrawable")
        if withdrawable_raw is None:
            withdrawable_raw = margin_summary.get("withdrawable", "0")
        withdrawable = float(withdrawable_raw)
        
        amount_dec = Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        amount_val = float(amount_dec)
        if amount_val <= 0:
            raise ValueError("Сумма вывода слишком мала после округления")
        if amount_val > withdrawable:
            raise ValueError(
                f"Недостаточно средств для вывода. Доступно: {withdrawable:.6f}, требуется: {amount_val:.6f}"
            )
        
        # Создаем действие для вывода
        timestamp = int(time.time() * 1000)
        
        action = {
            "type": "withdraw3",
            "destination": destination,
            "amount": format(amount_dec, "f"),
            "time": timestamp
        }
        
        # Подписываем действие как user-signed (EIP-712)
        signature = sign_withdraw_from_bridge_action(
            self.account,
            action,
            self.base_url == constants.MAINNET_API_URL
        )
        
        # Формируем запрос
        request_body = self._build_exchange_request_body(action, timestamp, signature)
        
        # Отправляем запрос
        response = requests.post(
            f"{self.base_url}/exchange",
            json=request_body,
            headers={"Content-Type": "application/json"}
        )
        
        try:
            result = response.json()
        except ValueError:
            result = response.text

        if isinstance(result, Mapping) and result.get("status") == "ok":
            print(f"Запрос на вывод успешно отправлен: {amount} USDC")
            print("Вывод будет обработан в течение 3-4 минут")
            return {
                "success": True,
                "amount": amount,
                "destination": destination
            }
        error_msg = None
        if isinstance(result, Mapping):
            response = result.get("response")
            if isinstance(response, Mapping):
                error_msg = response.get("data")
            if not error_msg:
                error_msg = result.get("error") or result.get("message")
        if not error_msg:
            error_msg = str(result)[:500]
        raise Exception(f"Ошибка при выводе (address {self.address}): {error_msg}")

    def withdraw_eth(
        self,
        amount: float,
        destination_eth_address: str
    ) -> Dict[str, Any]:
        """
        Вывод ETH на Ethereum через Hyperunit.

        Args:
            amount: Сумма ETH для вывода
            destination_eth_address: Адрес получателя в сети Ethereum

        Returns:
            Результат операции
        """
        if amount <= 0:
            raise ValueError("Сумма вывода должна быть больше 0")
        if not Web3.is_address(destination_eth_address):
            raise ValueError("Некорректный Ethereum адрес получателя")

        min_eth = 0.007
        if amount < min_eth:
            raise ValueError(f"Минимальная сумма вывода ETH ~ {min_eth} ETH")

        token_info = self._get_spot_token_info("ETH", aliases=["WETH", "UETH"])
        token_id = token_info.get("tokenId")
        if not token_id:
            raise Exception("Не найден tokenId для ETH")
        token_name = token_info.get("name") or "ETH"

        decimals = token_info.get("szDecimals")
        if decimals is None:
            decimals = token_info.get("weiDecimals", 6)
        quant = Decimal("1").scaleb(-int(decimals))
        amount_dec = Decimal(str(amount)).quantize(quant, rounding=ROUND_DOWN)
        if amount_dec <= 0:
            raise ValueError("Сумма вывода слишком мала после округления")

        base_url = "https://api.hyperunit-testnet.xyz"
        if self.base_url == constants.MAINNET_API_URL:
            base_url = "https://api.hyperunit.xyz"

        address_resp = requests.get(
            f"{base_url}/gen/hyperliquid/ethereum/eth/{destination_eth_address}",
            timeout=30,
            headers=_hyperunit_request_headers(),
        )
        try:
            address_result = address_resp.json()
        except ValueError:
            raise Exception(
                f"Некорректный ответ от Hyperunit (status={address_resp.status_code}). "
                f"Текст: {address_resp.text[:500]}"
            )

        protocol_address = address_result.get("address")
        if not protocol_address:
            raise Exception(f"Не удалось получить protocol address: {address_result}")

        timestamp = int(time.time() * 1000)
        action = {
            "type": "spotSend",
            "destination": protocol_address,
            "token": f"{token_name}:{token_id}",
            "amount": format(amount_dec, "f"),
            "time": timestamp
        }

        signature = sign_spot_transfer_action(
            self.account,
            action,
            self.base_url == constants.MAINNET_API_URL
        )

        request_body = self._build_exchange_request_body(action, timestamp, signature)

        response = requests.post(
            f"{self.base_url}/exchange",
            json=request_body,
            headers={"Content-Type": "application/json"}
        )
        try:
            result = response.json()
        except ValueError:
            result = response.text

        if isinstance(result, Mapping) and result.get("status") == "ok":
            print(f"Запрос на вывод {amount_dec} ETH отправлен")
            print("Вывод будет обработан в течение нескольких минут")
            return {
                "success": True,
                "amount": float(amount_dec),
                "destination": destination_eth_address,
                "protocol_address": protocol_address
            }

        error_msg = None
        if isinstance(result, Mapping):
            response_data = result.get("response")
            if isinstance(response_data, Mapping):
                error_msg = response_data.get("data")
            if not error_msg:
                error_msg = result.get("error") or result.get("message")
        if not error_msg:
            error_msg = str(result)[:500]
        raise Exception(f"Ошибка при выводе ETH: {error_msg}")

    def withdraw_eth_to_ethereum(
        self,
        amount: float,
        destination_eth_address: str
    ) -> Dict[str, Any]:
        """
        Алиас для вывода ETH на Ethereum.

        Args:
            amount: Сумма ETH для вывода
            destination_eth_address: Адрес получателя в сети Ethereum

        Returns:
            Результат операции
        """
        return self.withdraw_eth(amount, destination_eth_address)

    def transfer_usdc_perp_to_spot(self, amount: float) -> Dict[str, Any]:
        """
        Перевести USDC с perp аккаунта на spot для торговли.

        Args:
            amount: Сумма USDC для перевода

        Returns:
            Результат операции
        """
        if amount <= 0:
            raise ValueError("Сумма перевода должна быть больше 0")

        account_info = self.get_account_info()
        if account_info:
            margin_summary = account_info.get("marginSummary", {})
            total_raw_usd = float(margin_summary.get("totalRawUsd", "0"))
            if amount > total_raw_usd:
                raise ValueError(
                    f"Недостаточно средств для перевода. "
                    f"Всего: {total_raw_usd:.2f}, требуется: {amount:.2f}"
                )

        amount_dec = Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        amount_str = format(amount_dec, "f")

        timestamp = int(time.time() * 1000)
        action = {
            "type": "usdClassTransfer",
            "amount": amount_str,
            "toPerp": False,
            "nonce": timestamp,
        }

        signature = sign_usd_class_transfer_action(
            self.account,
            action,
            self.base_url == constants.MAINNET_API_URL
        )

        request_body = self._build_exchange_request_body(action, timestamp, signature)

        response = requests.post(
            f"{self.base_url}/exchange",
            json=request_body,
            headers={"Content-Type": "application/json"}
        )
        try:
            result = response.json()
        except ValueError:
            raise Exception(
                f"Некорректный ответ от API (status={response.status_code}). "
                f"Текст: {response.text[:500]}"
            )

        if isinstance(result, dict) and result.get("status") == "ok":
            print(f"Перевод {amount} USDC с perp на spot отправлен")
            return {
                "success": True,
                "amount": amount,
                "direction": "perp_to_spot"
            }

        if isinstance(result, dict):
            response_payload = result.get("response")
            if isinstance(response_payload, dict):
                error_msg = response_payload.get("data", "Неизвестная ошибка")
            else:
                error_msg = str(response_payload) if response_payload is not None else "Неизвестная ошибка"
        elif isinstance(result, str):
            error_msg = result
        else:
            error_msg = response.text[:500] or str(result)

        raise Exception(f"Ошибка при переводе: {error_msg}")

    def transfer_usdc_spot_to_perp(self, amount: float) -> Dict[str, Any]:
        """
        Перевести USDC со spot аккаунта на perp.

        Args:
            amount: Сумма USDC для перевода

        Returns:
            Результат операции
        """
        if amount <= 0:
            raise ValueError("Сумма перевода должна быть больше 0")

        amount_dec = Decimal(str(amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        amount_str = format(amount_dec, "f")

        timestamp = int(time.time() * 1000)
        action = {
            "type": "usdClassTransfer",
            "amount": amount_str,
            "toPerp": True,
            "nonce": timestamp,
        }

        signature = sign_usd_class_transfer_action(
            self.account,
            action,
            self.base_url == constants.MAINNET_API_URL
        )

        request_body = self._build_exchange_request_body(action, timestamp, signature)

        response = requests.post(
            f"{self.base_url}/exchange",
            json=request_body,
            headers={"Content-Type": "application/json"}
        )
        try:
            result = response.json()
        except ValueError:
            raise Exception(
                f"Некорректный ответ от API (status={response.status_code}). "
                f"Текст: {response.text[:500]}"
            )

        if isinstance(result, dict) and result.get("status") == "ok":
            print(f"Перевод {amount} USDC со spot на perp отправлен")
            return {
                "success": True,
                "amount": amount,
                "direction": "spot_to_perp"
            }

        if isinstance(result, dict):
            error_msg = result.get("response", {}).get("data", "Неизвестная ошибка")
        else:
            error_msg = response.text[:500] or str(result)

        raise Exception(f"Ошибка при переводе: {error_msg}")

    def spot_available_balance(self, coin: str) -> float:
        """Доступно к отправке по spot (total − hold) для токена по полю coin из clearinghouse."""
        return float(self.spot_available_balance_decimal(coin))

    def spot_available_balance_decimal(self, coin: str) -> Decimal:
        """Точный доступный баланс (total − hold) для spotSend — без float."""
        spot = self.info.spot_user_state(self.address)
        if not isinstance(spot, dict):
            return Decimal("0")
        c = (coin or "").strip().upper()
        for bal in spot.get("balances") or []:
            if not isinstance(bal, dict):
                continue
            if str(bal.get("coin") or "").upper() != c:
                continue
            total = Decimal(str(bal.get("total") or "0").replace(",", ""))
            hold = Decimal(str(bal.get("hold") or "0").replace(",", ""))
            d = total - hold
            return d if d > 0 else Decimal("0")
        return Decimal("0")

    def transfer_spot_to_address(
        self,
        token_coin: str,
        amount: float,
        destination: str,
    ) -> Dict[str, Any]:
        """
        Перевод спотового актива на другой адрес Hyperliquid (spotSend).

        Args:
            token_coin: имя токена как в балансе (coin), например USDC, HYPE
            amount: количество (в единицах токена)
            destination: 0x-адрес получателя (тот же формат, что кошелёк на HL)
        """
        if amount <= 0:
            raise ValueError("Сумма должна быть больше 0")
        dest = (destination or "").strip()
        if not Web3.is_address(dest):
            raise ValueError("Некорректный адрес получателя")
        dest = dest.lower()
        if dest == self.address.lower():
            raise ValueError("Нельзя перевести на тот же адрес")

        try:
            token_info = self._get_spot_token_info(token_coin.strip())
        except ValueError:
            raise ValueError(f"Не удалось найти метаданные токена «{token_coin}» в spotMeta.")

        token_id = token_info.get("tokenId")
        token_name = token_info.get("name") or token_coin
        if not token_id:
            raise ValueError(f"У токена «{token_coin}» нет tokenId для spotSend.")

        token_str = f"{token_name}:{token_id}"
        sz_decimals = int(token_info.get("szDecimals", 8))
        quant = Decimal("1") / (Decimal("10") ** sz_decimals)

        # Актуальный баланс непосредственно перед отправкой (между кликами мог уйти hold/часть).
        avail_dec = self.spot_available_balance_decimal(token_coin)
        if avail_dec <= 0:
            raise ValueError(f"Нет доступного баланса {token_coin} на spot.")

        amount_dec = Decimal(str(amount)).quantize(quant, rounding=ROUND_DOWN)
        if amount_dec <= 0:
            raise ValueError("Сумма слишком мала после округления по szDecimals токена.")
        if amount_dec > avail_dec:
            raise ValueError(
                f"Недостаточно средств: доступно {avail_dec}, запрошено {amount_dec}."
            )
        # Не больше актуального доступного (на случай гонки после проверки).
        amount_dec = min(amount_dec, avail_dec.quantize(quant, rounding=ROUND_DOWN))
        if amount_dec <= 0:
            raise ValueError("После сверки с балансом сумма стала нулевой — обновите страницу.")

        # SDK делает str(float): на границе баланса float даёт лишние знаки → «Insufficient balance».
        ts = get_timestamp_ms()
        action = {
            "destination": dest,
            "amount": format(amount_dec, "f"),
            "token": token_str,
            "time": ts,
            "type": "spotSend",
        }
        signature = sign_spot_transfer_action(
            self.account,
            action,
            self.base_url == constants.MAINNET_API_URL,
        )
        result = self.exchange._post_action(action, signature, ts)

        if isinstance(result, dict) and result.get("status") == "ok":
            return {
                "success": True,
                "amount": float(amount_dec),
                "token": token_str,
                "destination": dest,
            }

        error_msg = "Неизвестная ошибка"
        if isinstance(result, dict):
            resp = result.get("response")
            if isinstance(resp, str):
                error_msg = resp
            elif isinstance(resp, dict):
                error_msg = resp.get("data", error_msg)
            if error_msg == "Неизвестная ошибка":
                error_msg = str(result.get("error") or result)[:500]
        else:
            error_msg = str(result)[:500]
        hint = ""
        if "insufficient" in error_msg.lower():
            hint = (
                " Если часть токенов уже ушла, возможен повторный запрос (двойной клик) "
                "или граница баланса — обновите страницу и проверьте остаток."
            )
        raise Exception(f"Ошибка spotSend: {error_msg}{hint}")

    def update_leverage(
        self,
        coin: str,
        leverage: int,
        is_cross: bool = True,
    ) -> Dict[str, Any]:
        """Установить плечо для перп-актива (usdClassTransfer / updateLeverage в API HL)."""
        return self.exchange.update_leverage(leverage, coin, is_cross)

    def get_perp_position_szi(self, coin: str) -> Optional[float]:
        """Текущий szi по монете (перп), или None если позиции нет."""
        dex = coin.split(":")[0] if ":" in coin else ""
        try:
            st = self.info.user_state(self.address, dex)
        except Exception:
            return None
        for ap in st.get("assetPositions") or []:
            if not isinstance(ap, dict):
                continue
            pos = ap.get("position", ap)
            if not isinstance(pos, dict):
                continue
            if str(pos.get("coin") or "") != str(coin):
                continue
            szi = pos.get("szi")
            if szi is None:
                return None
            try:
                return float(szi)
            except (TypeError, ValueError):
                return None
        return None

    def _parse_order_result(
        self,
        result: Dict[str, Any],
        coin: str,
        is_buy: bool,
        size_str: str,
        price: Optional[float],
    ) -> Dict[str, Any]:
        if result.get("status") == "ok":
            response_data = result.get("response", {})
            if response_data.get("type") == "order":
                order_data = response_data.get("data", {})
                statuses = order_data.get("statuses", [])
                if statuses:
                    status = statuses[0]
                    if "resting" in status:
                        oid = status["resting"]["oid"]
                        return {
                            "success": True,
                            "order_id": oid,
                            "status": "resting",
                            "coin": coin,
                            "side": "buy" if is_buy else "sell",
                            "size": size_str,
                            "price": price,
                        }
                    if "filled" in status:
                        filled = status["filled"]
                        return {
                            "success": True,
                            "order_id": filled.get("oid"),
                            "status": "filled",
                            "total_sz": filled.get("totalSz"),
                            "avg_px": filled.get("avgPx"),
                            "coin": coin,
                        }
                    if "error" in status:
                        error_msg = status["error"]
                        raise Exception(f"Ошибка при размещении ордера: {error_msg}")
        raise Exception(f"Неожиданный ответ от API: {result}")

    def close_perp_market(
        self,
        coin: str,
        size: float,
        slippage: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Рыночное закрытие перп-позиции (reduce-only через SDK market_close)."""
        szi = self.get_perp_position_szi(coin)
        if szi is None or abs(szi) < 1e-12:
            raise ValueError("Нет открытой позиции по этому инструменту.")
        is_buy = float(szi) < 0
        slip = 0.05 if slippage is None else float(slippage)
        slip = min(slip, 0.8)
        result = self.exchange.market_close(coin, sz=size, slippage=slip)
        return self._parse_order_result(result, coin, is_buy, str(size), None)

    def close_perp_limit(
        self,
        coin: str,
        size: float,
        limit_px: float,
    ) -> Dict[str, Any]:
        """Лимитное закрытие перп-позиции (reduce-only)."""
        szi = self.get_perp_position_szi(coin)
        if szi is None or abs(szi) < 1e-12:
            raise ValueError("Нет открытой позиции по этому инструменту.")
        is_buy = float(szi) < 0
        return self.place_order(
            coin,
            is_buy,
            size,
            price=float(limit_px),
            order_type="Limit",
            reduce_only=True,
            time_in_force="Gtc",
        )

    def place_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        price: Optional[float] = None,
        order_type: str = "Limit",
        reduce_only: bool = False,
        time_in_force: str = "Gtc",
        slippage: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Разместить ордер
        
        Args:
            coin: Название монеты (например, "BTC")
            is_buy: True для покупки, False для продажи
            size: Размер ордера в базовой валюте
            price: Цена для лимитного ордера (None для рыночного)
            order_type: Тип ордера ("Limit" или "Market")
            reduce_only: Только уменьшение позиции
            time_in_force: Время действия ("Gtc", "Ioc", "Alo")
            slippage: Проскальзывание для рыночных ордеров (в процентах)
        
        Returns:
            Результат размещения ордера
        """
        # Получаем ID актива
        asset_id = self.get_asset_id(coin)
        if asset_id is None:
            raise ValueError(f"Монета {coin} не найдена")
        
        # Получаем метаданные для проверки размеров
        sz_decimals = self.info.asset_to_sz_decimals.get(asset_id)
        if sz_decimals is None:
            if asset_id >= 10000:
                # Спотовые активы: берем данные из spot_meta
                self._refresh_spot_mapping()
                sz_decimals = self.info.asset_to_sz_decimals.get(asset_id)
                if sz_decimals is None:
                    try:
                        spot_meta = self.info.spot_meta()
                        if isinstance(spot_meta, dict):
                            spot_meta = sanitize_spot_meta(spot_meta)
                    except Exception as e:
                        raise Exception(f"Не удалось получить spot meta: {e}")
                    universe = spot_meta.get("universe", [])
                    target_idx = asset_id - 10000
                    spot_info = None
                    for u in universe:
                        if isinstance(u, dict) and u.get("index") == target_idx:
                            spot_info = u
                            break
                    if spot_info is None:
                        raise ValueError(f"Неверный ID спотового актива: {asset_id}")
                    base, _quote = spot_info["tokens"]
                    base_info = spot_meta["tokens"][base]
                    sz_decimals = base_info.get("szDecimals", 0)
            else:
                meta = self.get_meta()
                if not meta:
                    raise Exception("Не удалось получить метаданные")
                universe = meta.get("universe", [])
                if asset_id >= len(universe):
                    raise ValueError(f"Неверный ID актива: {asset_id}")
                asset_info = universe[asset_id]
                sz_decimals = asset_info.get("szDecimals", 0)
        
        # Формируем размер ордера с правильным количеством знаков
        min_size = Decimal("1") / (Decimal("10") ** sz_decimals)
        size_dec = Decimal(str(size))
        if size_dec < min_size:
            raise ValueError(
                f"Размер ордера слишком мал для {coin} (minSize={min_size}, szDecimals={sz_decimals}). "
                "Увеличьте size."
            )
        quant = Decimal("1") / (Decimal("10") ** sz_decimals)
        size_str = str(size_dec.quantize(quant, rounding=ROUND_DOWN))
        if Decimal(size_str) <= 0:
            raise ValueError(
                f"Размер ордера слишком мал для {coin} (minSize={min_size}, szDecimals={sz_decimals}). "
                "Увеличьте size."
            )
        # В SDK уходит float; без квантования возможны лишние знаки и отказ при продаже (баланс базы).
        sz_exec = float(size_str)

        # Проверяем параметры
        if order_type == "Market":
            if price is not None:
                raise ValueError("Рыночный ордер не требует цены")
        else:  # Limit order
            if price is None:
                raise ValueError("Лимитный ордер требует указания цены")
        
        # Формируем order_wire вручную для совместимости
        if order_type == "Market":
            order_wire = {
                "a": asset_id,
                "b": is_buy,
                "p": "0",  # Цена не нужна для рыночного ордера
                "s": size_str,
                "r": reduce_only,
                "t": {"market": {}},
            }
            if slippage:
                order_wire["t"]["market"]["slippage"] = str(slippage)
        else:  # Limit order
            tif_map = {
                "Gtc": "Gtc",
                "Ioc": "Ioc",
                "Alo": "Alo"
            }
            tif = tif_map.get(time_in_force, "Gtc")

            if price is not None:
                tick = self._get_tick_size(coin)
                price = self._round_price_to_tick(price, tick, is_buy)
            
            order_wire = {
                "a": asset_id,
                "b": is_buy,
                "p": str(price),
                "s": size_str,
                "r": reduce_only,
                "t": {"limit": {"tif": tif}}
            }
        
        # Размещаем ордер через официальный SDK (корректная подпись)
        if order_type == "Market":
            # Доля (0.1 = 10%). None → как в SDK (~5%), не 0.5 (50%).
            if slippage is None:
                slippage = 0.05
            # Биржа: цена IoC не дальше 80% от reference; иначе «Order price cannot be more than 80% away…».
            slippage = min(float(slippage), 0.8)
            result = self.exchange.market_open(
                coin,
                is_buy,
                sz_exec,
                px=price,
                slippage=slippage,
            )
        else:
            result = self.exchange.order(
                coin,
                is_buy,
                sz_exec,
                price,
                order_type={"limit": {"tif": tif}},
                reduce_only=reduce_only,
            )

        return self._parse_order_result(result, coin, is_buy, size_str, price)

    def _get_tick_size(self, coin: str) -> Optional[Decimal]:
        if coin in self._tick_size_cache:
            return self._tick_size_cache[coin]

        snapshot = self.info.l2_snapshot(coin)
        levels = snapshot.get("levels", [])
        prices = []
        for side in levels:
            for level in side:
                px = level.get("px")
                if px is not None:
                    prices.append(Decimal(str(px)))

        if len(prices) < 2:
            self._tick_size_cache[coin] = None
            return None

        uniq = sorted(set(prices))
        diffs = [uniq[i + 1] - uniq[i] for i in range(len(uniq) - 1) if uniq[i + 1] > uniq[i]]
        tick = min(diffs) if diffs else None
        self._tick_size_cache[coin] = tick
        return tick

    def _round_price_to_tick(self, price: float, tick: Optional[Decimal], is_buy: bool) -> float:
        if tick is None:
            return price

        price_dec = Decimal(str(price))
        rounding = ROUND_DOWN if is_buy else ROUND_UP
        n = (price_dec / tick).to_integral_value(rounding=rounding)
        adjusted = n * tick
        return float(adjusted)
    
    def cancel_order(self, coin: str, order_id: int) -> Dict[str, Any]:
        """
        Отменить ордер
        
        Args:
            coin: Название монеты
            order_id: ID ордера для отмены
        
        Returns:
            Результат отмены
        """
        asset_id = self.get_asset_id(coin)
        if asset_id is None:
            raise ValueError(f"Монета {coin} не найдена")
        
        timestamp = int(time.time() * 1000)
        
        # Формируем cancel request
        cancel_request = {
            "a": asset_id,
            "o": order_id
        }
        
        action = {
            "type": "cancel",
            "cancels": [cancel_request]
        }
        
        signature = sign_l1_action(
            self.account,
            action,
            self.vault_address,
            timestamp,
            self.expires_after,
            self.base_url == constants.MAINNET_API_URL
        )
        
        request_body = self._build_exchange_request_body(action, timestamp, signature)
        
        response = requests.post(
            f"{self.base_url}/exchange",
            json=request_body,
            headers={"Content-Type": "application/json"}
        )
        
        result = response.json()
        
        if result.get("status") == "ok":
            response_data = result.get("response", {})
            if response_data.get("type") == "cancel":
                cancel_data = response_data.get("data", {})
                statuses = cancel_data.get("statuses", [])
                
                if statuses and statuses[0] == "success":
                    print(f"Ордер {order_id} успешно отменен")
                    return {
                        "success": True,
                        "order_id": order_id
                    }
                else:
                    error = statuses[0].get("error", "Неизвестная ошибка") if isinstance(statuses[0], dict) else str(statuses[0])
                    raise Exception(f"Ошибка при отмене ордера: {error}")
        
        raise Exception(f"Неожиданный ответ от API: {result}")


def create_new_account() -> Dict[str, str]:
    """
    Создать новый Ethereum аккаунт для Hyperliquid
    
    Returns:
        Словарь с приватным ключом и адресом
    """
    account = Account.create()
    return {
        "private_key": account.key.hex(),
        "address": account.address
    }


