"""
Исполнение согласованных заявок: вывод (HL API), депозит (USDC transfer на Bridge2 / ETH на Unit).
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import TYPE_CHECKING, Optional, Tuple

from django.db import transaction
from django.utils import timezone
from web3 import Web3

from .hl_read import fetch_usdc_transfer_max_amount
from .hyperliquid_account import HyperliquidAccount
from .models import FundsOperationRequest

if TYPE_CHECKING:
    from django.http import HttpRequest

    from .models import TraderWallet

logger = logging.getLogger(__name__)


def _perp_withdrawable_usdc(account: HyperliquidAccount) -> float:
    info = account.get_account_info()
    if not info:
        return 0.0
    margin = info.get("marginSummary") or {}
    w_raw = info.get("withdrawable")
    if w_raw is None:
        w_raw = margin.get("withdrawable", "0")
    try:
        return float(str(w_raw).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _spot_usdc_available(account: HyperliquidAccount, wallet_address: str) -> float:
    d = account.spot_available_balance_decimal("USDC")
    if d and d > 0:
        return float(d)
    alt = fetch_usdc_transfer_max_amount(wallet_address, "spot_to_perp")
    return float(alt) if alt is not None else 0.0


def _ensure_usdc_on_perp_for_bridge(
    account: HyperliquidAccount,
    wallet_address: str,
    amount: float,
) -> None:
    """
    withdraw3 снимает только perp withdrawable. Если USDC лежит на spot,
    переносим spot→perp (usdClassTransfer) порциями, пока withdrawable не хватит.
    """
    eps = 1e-6
    for _round in range(8):
        w = _perp_withdrawable_usdc(account)
        if w + eps >= amount:
            return
        need = amount - w
        spot_avail = _spot_usdc_available(account, wallet_address)
        if spot_avail <= eps:
            raise ValueError(
                f"На perp доступно к выводу {w:.6f} USDC, нужно {amount:.6f}. "
                f"На spot нет USDC для перевода на perp (доступно {spot_avail:.6f}). "
                f"Переведите USDC на perp вручную на дашборде или пополните счёт."
            )
        chunk = min(spot_avail, need)
        chunk_dec = Decimal(str(chunk)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        chunk_f = float(chunk_dec)
        if chunk_f <= eps:
            raise ValueError(
                f"Не удалось сформировать сумму перевода spot→perp (доступно {spot_avail:.6f} USDC)."
            )
        logger.info(
            "funds withdraw: spot→perp %.6f USDC (perp withdrawable %.6f, need %.6f)",
            chunk_f,
            w,
            amount,
        )
        tr = account.transfer_usdc_spot_to_perp(chunk_f)
        if not (isinstance(tr, dict) and tr.get("success")):
            raise ValueError(f"Перевод spot→perp: неожиданный ответ {tr!r}")
        time.sleep(1.6)

    w_final = _perp_withdrawable_usdc(account)
    if w_final + eps < amount:
        raise ValueError(
            f"После переводов spot→perp на perp withdrawable {w_final:.6f} USDC, "
            f"нужно {amount:.6f}. Проверьте баланс и маржу в Hyperliquid."
        )


def _web3_arbitrum(testnet: bool) -> Web3:
    """RPC Arbitrum One / Sepolia для депозита USDC на Bridge2."""
    from django.conf import settings

    url = (
        settings.ARBITRUM_SEPOLIA_RPC_URL
        if testnet
        else settings.ARBITRUM_ONE_RPC_URL
    )
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 120}))
    if not w3.is_connected():
        raise ConnectionError(f"Нет подключения к Arbitrum RPC ({url}).")
    return w3


def _web3_ethereum_l1(testnet: bool) -> Web3:
    """RPC Ethereum mainnet / Sepolia для депозита ETH через Unit."""
    from django.conf import settings

    url = (
        settings.ETHEREUM_SEPOLIA_RPC_URL
        if testnet
        else settings.ETHEREUM_MAINNET_RPC_URL
    )
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 120}))
    if not w3.is_connected():
        raise ConnectionError(f"Нет подключения к Ethereum RPC ({url}).")
    return w3


def _private_key_for_wallet(wallet: "TraderWallet") -> Optional[str]:
    """Дублирует логику views._private_key_for_wallet без циклического импорта."""
    from django.conf import settings

    from .wallet_crypto import decrypt_trading_key

    if wallet.trading_key_encrypted:
        try:
            return decrypt_trading_key(wallet.trading_key_encrypted)
        except Exception:
            return None
    pk = getattr(settings, "HYPERLIQUID_TRADING_PRIVATE_KEY", "") or ""
    pk = pk.strip()
    return pk or None


def try_execute_approved_withdraw(
    op: FundsOperationRequest,
    request: "HttpRequest",
) -> Tuple[bool, str]:
    """
    Если заявка — вывод, оба аппрува есть, исполнение ещё не было —
    подписывает и отправляет withdraw3 (USDC) или spotSend ETH (Ethereum).

    Возвращает (успех_без_ошибки, сообщение_для_пользователя).
    """
    if op.kind != FundsOperationRequest.Kind.WITHDRAW:
        return True, "не вывод — автоматическое исполнение не требуется"

    with transaction.atomic():
        locked = (
            FundsOperationRequest.objects.select_for_update()
            .select_related("wallet")
            .get(pk=op.pk)
        )
        if locked.executed_at or locked.rejected_at:
            return True, "уже исполнена или отклонена"
        if (
            locked.withdrawal_bridge_submitted_at
            and str(locked.route or "") == "usdc_arbitrum"
            and not locked.executed_at
        ):
            return True, "вывод USDC уже отправлен в Hyperliquid, ожидаем финализацию на Arbitrum"
        if not locked.both_approved():
            return True, "ещё не все согласования"

        wallet = locked.wallet
        pk = _private_key_for_wallet(wallet)
        if not pk:
            msg = (
                "Нет приватного ключа для кошелька заявки — задайте ключ у кошелька "
                "или HYPERLIQUID_TRADING_PRIVATE_KEY в окружении."
            )
            logger.error("funds withdraw id=%s: %s", locked.pk, msg)
            return False, msg

        exec_testnet = bool(locked.hl_testnet)
        try:
            account = HyperliquidAccount(private_key=pk, testnet=exec_testnet)
        except Exception as e:
            logger.exception("HyperliquidAccount init for op %s", locked.pk)
            return False, f"Не удалось инициализировать HL: {e}"

        if account.address.lower() != wallet.address.lower():
            msg = "Адрес ключа не совпадает с кошельком заявки."
            logger.error("funds withdraw id=%s: %s", locked.pk, msg)
            return False, msg

        amt = float(locked.amount)
        route = str(locked.route or "")

        try:
            if route == "usdc_arbitrum":
                _ensure_usdc_on_perp_for_bridge(account, wallet.address, amt)
                result = account.withdraw(amt, destination=wallet.address)
            elif route == "eth_ethereum":
                result = account.withdraw_eth(amt, destination_eth_address=wallet.address)
            else:
                return False, f"Неизвестный маршрут: {route}"
        except Exception as e:
            logger.exception("HL withdraw failed op %s", locked.pk)
            return False, str(e)

        if not (isinstance(result, dict) and result.get("success")):
            return False, f"Неожиданный ответ HL: {result!r}"

        now = timezone.now()
        if route == "usdc_arbitrum":
            # Строка в ленте скрывается после FinalizedWithdrawal на Bridge2 (Arbitrum), не после ответа API.
            locked.withdrawal_bridge_submitted_at = now
            locked.save(update_fields=["withdrawal_bridge_submitted_at"])
        else:
            locked.executed_at = now
            locked.save(update_fields=["executed_at"])

    logger.info("funds withdraw id=%s executed via HL", locked.pk)
    if route == "usdc_arbitrum":
        return (
            True,
            "Вывод USDC отправлен в Hyperliquid. Строка в ленте исчезнет после финализации на Arbitrum.",
        )
    return True, "Вывод отправлен в Hyperliquid (запрос принят)."


def try_execute_approved_deposit(
    op: FundsOperationRequest,
    request: "HttpRequest",
) -> Tuple[bool, str]:
    """
    После двух согласований: USDC — transfer на Bridge2 (Arbitrum),
    ETH — отправка на Unit-адрес (Ethereum L1). Нужен приватный ключ кошелька в БД/env.
    """
    if op.kind != FundsOperationRequest.Kind.DEPOSIT:
        return True, "не депозит — автоматическое исполнение не требуется"

    with transaction.atomic():
        locked = (
            FundsOperationRequest.objects.select_for_update()
            .select_related("wallet")
            .get(pk=op.pk)
        )
        if locked.executed_at or locked.rejected_at:
            return True, "уже исполнена или отклонена"
        if not locked.both_approved():
            return True, "ещё не все согласования"

        wallet = locked.wallet
        pk = _private_key_for_wallet(wallet)
        if not pk:
            msg = (
                "Нет приватного ключа для кошелька заявки — задайте ключ у кошелька "
                "или HYPERLIQUID_TRADING_PRIVATE_KEY в окружении."
            )
            logger.error("funds deposit id=%s: %s", locked.pk, msg)
            return False, msg

        exec_testnet = bool(locked.hl_testnet)
        try:
            account = HyperliquidAccount(private_key=pk, testnet=exec_testnet)
        except Exception as e:
            logger.exception("HyperliquidAccount init for deposit op %s", locked.pk)
            return False, f"Не удалось инициализировать HL: {e}"

        if account.address.lower() != wallet.address.lower():
            msg = "Адрес ключа не совпадает с кошельком заявки."
            logger.error("funds deposit id=%s: %s", locked.pk, msg)
            return False, msg

        amt = float(locked.amount)
        route = str(locked.route or "")

        try:
            if route == "usdc_arbitrum":
                w3 = _web3_arbitrum(exec_testnet)
                result = account.deposit_via_bridge(amt, w3)
            elif route == "eth_ethereum":
                w3 = _web3_ethereum_l1(exec_testnet)
                result = account.deposit_eth(amt, w3)
            else:
                return False, f"Неизвестный маршрут депозита: {route}"
        except Exception as e:
            logger.exception("HL deposit failed op %s", locked.pk)
            return False, str(e)

        if not (isinstance(result, dict) and result.get("success")):
            return False, f"Неожиданный ответ депозита: {result!r}"

        txh = result.get("tx_hash") or ""
        if not txh:
            return False, "В ответе депозита нет tx_hash."

        locked.executed_at = timezone.now()
        locked.blockchain_tx_hash = str(txh)[:80]
        locked.save(update_fields=["executed_at", "blockchain_tx_hash"])

    logger.info("funds deposit id=%s on-chain tx=%s", locked.pk, txh)
    short = f"{txh[:10]}…{txh[-6:]}" if len(txh) > 20 else txh
    return True, f"Депозит отправлен в блокчейн (tx {short}). Средства зачисляются на HL обычно в течение минут."
