"""
Финализация вывода USDC по событию FinalizedWithdrawal на Hyperliquid Bridge2 (Arbitrum).
https://github.com/hyperliquid-dex/contracts/blob/master/Bridge2.sol
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone
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


def _find_finalized_log_for_op(
    w3: Web3,
    bridge: str,
    wallet_lower: str,
    expected_usd: int,
    from_block: int,
) -> Optional[dict[str, Any]]:
    """
    Ищет лог FinalizedWithdrawal с destination == wallet и usd == expected.
    Фильтр по indexed user (тот же адрес, что и кошелёк) сужает выборку.
    """
    c = w3.eth.contract(
        address=Web3.to_checksum_address(bridge),
        abi=[_FINALIZED_EVENT_ABI],
    )
    latest = int(w3.eth.block_number)
    to_block = latest
    # indexed user — адрес в topic как 32 байта (Hyperliquid при выводе на свой кошелёк совпадает с destination).
    pad = "0x" + wallet_lower[2:].rjust(64, "0")
    topic0 = w3.keccak(text="FinalizedWithdrawal(address,address,uint64,uint64,bytes32)")

    # Несколько чанков — публичные RPC режут огромные get_logs.
    chunk = 40_000
    b = max(1, from_block)
    while b <= to_block:
        chunk_end = min(b + chunk - 1, to_block)
        try:
            logs = w3.eth.get_logs(
                {
                    "fromBlock": b,
                    "toBlock": chunk_end,
                    "address": Web3.to_checksum_address(bridge),
                    "topics": [topic0, pad],
                }
            )
        except Exception as e:
            logger.warning("get_logs %s-%s: %s", b, chunk_end, e)
            b = chunk_end + 1
            continue
        for log in logs:
            try:
                decoded = c.events.FinalizedWithdrawal().process_log(log)
            except Exception:
                continue
            args = decoded["args"]
            dest = (args.get("destination") or "").lower()
            if dest != wallet_lower:
                continue
            usd = int(args.get("usd", 0))
            if usd == expected_usd:
                return log
        b = chunk_end + 1
    return None


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
        txh = log.get("transactionHash")
        if hasattr(txh, "hex"):
            tx_hex = txh.hex()
        else:
            tx_hex = Web3.to_hex(txh)

        with transaction.atomic():
            locked = FundsOperationRequest.objects.select_for_update().get(pk=op.pk)
            if locked.executed_at or locked.rejected_at:
                continue
            locked.executed_at = timezone.now()
            locked.blockchain_tx_hash = tx_hex[:80]
            locked.save(update_fields=["executed_at", "blockchain_tx_hash"])
        closed += 1
        logger.info(
            "USDC withdraw op=%s finalized on Arbitrum tx=%s amount=%s",
            op.pk,
            tx_hex,
            op.amount,
        )
    return closed
