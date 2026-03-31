"""
Финализация вывода USDC по событию FinalizedWithdrawal на Hyperliquid Bridge2 (Arbitrum).
https://github.com/hyperliquid-dex/contracts/blob/master/Bridge2.sol
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

import requests
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from hexbytes import HexBytes
from web3 import Web3

from .models import FundsOperationRequest, TraderWallet

logger = logging.getLogger(__name__)

# Bridge2 на Arbitrum One / Arbitrum Sepolia (testnet).
BRIDGE2_ARBITRUM_MAINNET = Web3.to_checksum_address(
    "0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7"
)
BRIDGE2_ARBITRUM_TESTNET = Web3.to_checksum_address(
    "0x08cfc1B6b2dCF36A1480b99353A354AA8AC56f89"
)

_FINALIZED_EVENT_ABI = {
    "type": "event",
    "name": "FinalizedWithdrawal",
    "anonymous": False,
    "inputs": [
        {"name": "user", "type": "address", "indexed": True},
        {"name": "destination", "type": "address", "indexed": False},
        {"name": "usd", "type": "uint64", "indexed": False},
        {"name": "nonce", "type": "uint64", "indexed": False},
        {"name": "message", "type": "bytes32", "indexed": False},
    ],
}


def _bridge_address(testnet: bool) -> str:
    return BRIDGE2_ARBITRUM_TESTNET if testnet else BRIDGE2_ARBITRUM_MAINNET


def _rpc_url(testnet: bool) -> str:
    from django.conf import settings

    if testnet:
        return getattr(
            settings,
            "ARBITRUM_SEPOLIA_RPC_URL",
            "https://sepolia-rollup.arbitrum.io/rpc",
        )
    return getattr(settings, "ARBITRUM_ONE_RPC_URL", "https://arb1.arbitrum.io/rpc")


def _amount_to_bridge_usd_int(amount: Decimal) -> int:
    """В контракте uint64 usd в микроединицах USDC (6 знаков)."""
    return int(
        (amount * Decimal(10**6)).quantize(Decimal("1"), rounding=ROUND_DOWN)
    )


def _usd_matches_chain(expected: int, actual: int) -> bool:
    """
    Сумма в заявке (Decimal) и usd в событии могут расходиться на несколько
    микро-USDC из-за float в HL API / округления.
    """
    if actual == expected:
        return True
    return abs(actual - expected) <= 2000  # до 0,002 USDC


def _w3(testnet: bool) -> Optional[Web3]:
    url = _rpc_url(testnet)
    try:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 25}))
        if not w3.is_connected():
            return None
        return w3
    except Exception as e:
        logger.warning("Arbitrum RPC недоступен (%s): %s", url, e)
        return None


def _decode_finalized_and_match(
    c: Any,
    log: dict[str, Any],
    wallet_lower: str,
    expected_usd: int,
) -> bool:
    try:
        decoded = c.events.FinalizedWithdrawal().process_log(log)
    except Exception:
        return False
    args = decoded["args"]
    dest = (args.get("destination") or "").lower()
    user_addr = (args.get("user") or "").lower()
    # Вывод на свой кошелёк: user и destination совпадают; на всякий случай принимаем любой из них.
    if dest != wallet_lower and user_addr != wallet_lower:
        return False
    usd = int(args.get("usd", 0))
    return _usd_matches_chain(expected_usd, usd)


def _get_logs_chunk(
    w3: Web3,
    bridge: str,
    topic0: Any,
    from_block: int,
    to_block: int,
    wallet_topic: Optional[str],
) -> list[Any]:
    """Один вызов get_logs; wallet_topic=None — только topic0 (шире, но надёжнее при капризном RPC)."""
    base: dict[str, Any] = {
        "fromBlock": from_block,
        "toBlock": to_block,
        "address": Web3.to_checksum_address(bridge),
        "topics": [topic0, wallet_topic] if wallet_topic is not None else [topic0],
    }
    return list(w3.eth.get_logs(base))


def _get_logs_range_resilient(
    w3: Web3,
    bridge: str,
    topic0: Any,
    wallet_topic: Optional[str],
    fb: int,
    tb: int,
) -> list[Any]:
    """
    eth_getLogs: при ошибке (слишком большой диапазон/результат) делит пополам вместо
    молчаливого пропуска блоков — из‑за этого заявка могла не закрываться.
    """
    if fb > tb:
        return []
    try:
        return _get_logs_chunk(w3, bridge, topic0, fb, tb, wallet_topic)
    except Exception as e:
        if tb - fb <= 1:
            logger.warning(
                "get_logs окончательно не удался %s-%s topic1=%s: %s",
                fb,
                tb,
                wallet_topic is not None,
                e,
            )
            return []
        mid = (fb + tb) // 2
        left = _get_logs_range_resilient(w3, bridge, topic0, wallet_topic, fb, mid)
        right = _get_logs_range_resilient(w3, bridge, topic0, wallet_topic, mid + 1, tb)
        return left + right


def _find_finalized_log_for_op(
    w3: Web3,
    bridge: str,
    wallet_lower: str,
    expected_usd: int,
    from_block: int,
) -> Optional[dict[str, Any]]:
    """
    Ищет лог FinalizedWithdrawal с destination или user == wallet и usd ≈ expected.
    Сначала узкий фильтр (topic0 + indexed user); если пусто — поиск только по topic0.
    """
    c = w3.eth.contract(
        address=Web3.to_checksum_address(bridge),
        abi=[_FINALIZED_EVENT_ABI],
    )
    latest = int(w3.eth.block_number)
    to_block = latest
    pad = "0x" + wallet_lower[2:].rjust(64, "0")
    topic0 = w3.keccak(text="FinalizedWithdrawal(address,address,uint64,uint64,bytes32)")

    def scan_with_mode(use_wallet_topic: bool) -> Optional[dict[str, Any]]:
        wallet_topic: Optional[str] = pad if use_wallet_topic else None
        step = 2000
        b = max(1, from_block)
        while b <= to_block:
            ce = min(b + step - 1, to_block)
            logs = _get_logs_range_resilient(w3, bridge, topic0, wallet_topic, b, ce)
            for log in logs:
                if _decode_finalized_and_match(c, log, wallet_lower, expected_usd):
                    return log
            b = ce + 1
        return None

    found = scan_with_mode(True)
    if found:
        return found
    found = scan_with_mode(False)
    if found:
        logger.info(
            "FinalizedWithdrawal найден по фильтру только topic0 (без topic1 user) wallet=%s",
            wallet_lower[:12],
        )
    return found


def _finalize_op_from_log(op: FundsOperationRequest, log: dict[str, Any]) -> bool:
    """Проставить executed_at и tx hash по найденному логу (RPC или Arbiscan)."""
    txh = log.get("transactionHash")
    if hasattr(txh, "hex"):
        tx_hex = txh.hex()
    else:
        tx_hex = Web3.to_hex(txh)

    with transaction.atomic():
        locked = FundsOperationRequest.objects.select_for_update().get(pk=op.pk)
        if locked.executed_at or locked.rejected_at:
            return False
        locked.executed_at = timezone.now()
        locked.blockchain_tx_hash = tx_hex[:80]
        locked.save(update_fields=["executed_at", "blockchain_tx_hash"])
    logger.info(
        "USDC withdraw op=%s finalized on Arbitrum tx=%s amount=%s",
        op.pk,
        tx_hex,
        op.amount,
    )
    return True


_TOPIC0_FINALIZED_HEX: Optional[str] = None


def _topic0_finalized_hex() -> str:
    global _TOPIC0_FINALIZED_HEX
    if _TOPIC0_FINALIZED_HEX is None:
        w3 = Web3()
        h = w3.keccak(text="FinalizedWithdrawal(address,address,uint64,uint64,bytes32)")
        _TOPIC0_FINALIZED_HEX = Web3.to_hex(h)
    return _TOPIC0_FINALIZED_HEX


def _wallet_topic_padded(wallet_lower: str) -> str:
    return "0x" + wallet_lower[2:].rjust(64, "0")


def _arbiscan_base_url(testnet: bool) -> str:
    return (
        "https://api-sepolia.arbiscan.io/api"
        if testnet
        else "https://api.arbiscan.io/api"
    )


def _arbiscan_api_key(testnet: bool) -> str:
    from django.conf import settings

    if testnet:
        return (
            getattr(settings, "ARBITRUM_SEPOLIA_ARBISCAN_API_KEY", "")
            or ""
        )
    return getattr(settings, "ARBITRUM_ARBISCAN_API_KEY", "") or ""


def _arbiscan_request_json(
    testnet: bool, params: dict[str, Any], timeout: float = 12.0
) -> Optional[dict[str, Any]]:
    key = _arbiscan_api_key(testnet)
    if not key:
        return None
    base = _arbiscan_base_url(testnet)
    q = {"apikey": key, **params}
    try:
        r = requests.get(base, params=q, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("Arbiscan request %s: %s", params.get("action"), e)
        return None


def _arbiscan_latest_block(testnet: bool) -> Optional[int]:
    j = _arbiscan_request_json(
        testnet, {"module": "proxy", "action": "eth_blockNumber"}
    )
    if not j:
        return None
    res = j.get("result")
    if isinstance(res, str) and res.startswith("0x"):
        try:
            return int(res, 16)
        except ValueError:
            return None
    return None


def _arbiscan_get_logs_chunk(
    testnet: bool,
    bridge: str,
    topic0: str,
    topic1: str,
    from_block: int,
    to_block: int,
) -> list[dict[str, Any]]:
    """Один getLogs; Arbiscan — обычно не больше 1000 блоков на запрос."""
    j = _arbiscan_request_json(
        testnet,
        {
            "module": "logs",
            "action": "getLogs",
            "fromBlock": str(from_block),
            "toBlock": str(to_block),
            "address": Web3.to_checksum_address(bridge),
            "topic0": topic0,
            "topic1": topic1,
            "topic0_1_opr": "and",
        },
    )
    if not j:
        return []
    res = j.get("result")
    if isinstance(res, list):
        return res
    if isinstance(res, str) and res:
        logger.warning("Arbiscan getLogs: %s", res[:200])
    return []


def _normalize_arbiscan_log(entry: dict[str, Any]) -> dict[str, Any]:
    """Привести ответ Arbiscan к виду, который process_log принимает."""

    def _topics() -> list:
        out = []
        for t in entry.get("topics") or []:
            ts = t if isinstance(t, str) else str(t)
            if not ts.startswith("0x"):
                ts = "0x" + ts
            out.append(HexBytes(ts))
        return out

    bn = entry.get("blockNumber")
    if isinstance(bn, str):
        block_number = int(bn, 16) if bn.startswith("0x") else int(bn)
    else:
        block_number = int(bn)

    txh = entry.get("transactionHash", "0x")
    if isinstance(txh, str) and not txh.startswith("0x"):
        txh = "0x" + txh

    li = entry.get("logIndex", "0x0")
    if isinstance(li, str):
        log_index = int(li, 16) if li.startswith("0x") else int(li)
    else:
        log_index = int(li)

    data = entry.get("data") or "0x"
    if isinstance(data, str) and not data.startswith("0x"):
        data = "0x" + data

    return {
        "address": Web3.to_checksum_address(entry["address"]),
        "topics": _topics(),
        "data": data,
        "blockNumber": block_number,
        "transactionHash": HexBytes(txh),
        "logIndex": log_index,
    }


def _estimate_fb_from_submit(op: FundsOperationRequest, latest: int) -> int:
    ts = op.withdrawal_bridge_submitted_at
    if not ts:
        return max(1, latest - 50000)
    secs = max(0, (timezone.now() - ts).total_seconds())
    # ~4 блока/с на Arbitrum L2 (порядок величины)
    return max(1, latest - int(secs * 4 + 8000))


def try_finalize_usdc_withdrawals_for_wallet_arbiscan(
    wallet: TraderWallet,
    *,
    max_http_calls: int = 15,
) -> int:
    """
    Финализация через Arbiscan API (HTTP), без eth_getLogs по RPC.
    До max_http_calls запросов за один вызов (защита от таймаута воркера).
    Курсор в cache (ключ arbiscan_wc:...) продолжает скан при следующем вызове.
    """
    pending = list(
        FundsOperationRequest.objects.filter(
            wallet=wallet,
            kind=FundsOperationRequest.Kind.WITHDRAW,
            route=FundsOperationRequest.Route.USDC_ARBITRUM,
            withdrawal_bridge_submitted_at__isnull=False,
            executed_at__isnull=True,
            rejected_at__isnull=True,
        ).order_by("created_at")
    )
    if not pending:
        return 0

    closed = 0
    wl = wallet.address.lower()
    topic0 = _topic0_finalized_hex()
    topic1 = _wallet_topic_padded(wl)
    dummy_w3 = Web3()

    for testnet in (False, True):
        group = [p for p in pending if bool(p.hl_testnet) == testnet]
        if not group:
            continue
        if not _arbiscan_api_key(testnet):
            logger.warning(
                "Arbiscan API key не задан (testnet=%s) — финализация USDC→Arbitrum пропущена",
                testnet,
            )
            continue

        latest = _arbiscan_latest_block(testnet)
        if latest is None:
            continue

        bridge = _bridge_address(testnet)
        c = dummy_w3.eth.contract(
            address=Web3.to_checksum_address(bridge),
            abi=[_FINALIZED_EVENT_ABI],
        )

        cursor_key = f"arbiscan_wc:{wallet.pk}:{int(testnet)}"
        fb = cache.get(cursor_key)
        if fb is None:
            fb = min(_estimate_fb_from_submit(op, latest) for op in group)
        fb = max(1, min(int(fb), latest))

        http_used = 0
        b = fb
        while b <= latest and http_used < max_http_calls:
            ce = min(b + 999, latest)
            raw_logs = _arbiscan_get_logs_chunk(testnet, bridge, topic0, topic1, b, ce)
            http_used += 1
            for raw in raw_logs:
                try:
                    log = _normalize_arbiscan_log(raw)
                except Exception as e:
                    logger.debug("arbiscan log normalize: %s", e)
                    continue
                for op in list(group):
                    exp = _amount_to_bridge_usd_int(op.amount)
                    if not _decode_finalized_and_match(c, log, wl, exp):
                        continue
                    if _finalize_op_from_log(op, log):
                        closed += 1
                        group.remove(op)
                    else:
                        op.refresh_from_db()
                        if op.executed_at or op.rejected_at:
                            group.remove(op)
            if not group:
                cache.delete(cursor_key)
                break
            b = ce + 1

        if group:
            if b <= latest:
                cache.set(cursor_key, b, timeout=86400 * 7)
            else:
                cache.set(cursor_key, max(1, latest - 12000), timeout=86400 * 7)

    return closed


def try_finalize_usdc_withdrawals_for_wallet(wallet: TraderWallet) -> int:
    """
    Для заявок USDC→Arbitrum с выставленным withdrawal_bridge_submitted_at и без executed_at
    проверяет Bridge2 на Arbitrum: при событии FinalizedWithdrawal с нужной суммой и destination
    проставляет executed_at и tx hash.

    Возвращает число закрытых заявок.
    """
    pending = list(
        FundsOperationRequest.objects.filter(
            wallet=wallet,
            kind=FundsOperationRequest.Kind.WITHDRAW,
            route=FundsOperationRequest.Route.USDC_ARBITRUM,
            withdrawal_bridge_submitted_at__isnull=False,
            executed_at__isnull=True,
            rejected_at__isnull=True,
        ).order_by("created_at")
    )
    if not pending:
        return 0

    closed = 0
    for op in pending:
        testnet = bool(getattr(op, "hl_testnet", False))
        w3 = _w3(testnet)
        if not w3:
            continue

        bridge = _bridge_address(testnet)
        wl = wallet.address.lower()
        latest = int(w3.eth.block_number)
        scan_from = max(1, latest - 250_000)

        expected = _amount_to_bridge_usd_int(op.amount)
        try:
            log = _find_finalized_log_for_op(
                w3, bridge, wl, expected, scan_from
            )
        except Exception as e:
            logger.warning("finalize scan op=%s: %s", op.pk, e)
            continue
        if not log:
            continue
        if _finalize_op_from_log(op, log):
            closed += 1
    return closed
