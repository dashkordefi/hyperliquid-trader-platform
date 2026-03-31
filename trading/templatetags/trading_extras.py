"""Фильтры шаблонов для торгового приложения."""

from __future__ import annotations

from typing import Any

from django import template

register = template.Library()


@register.filter
def funds_tx_explorer_url(op: Any, testnet: Any = None) -> str:
    """
    URL обозревателя блока для заявки депозита/вывода (по маршруту и hl_network_testnet).
    """
    if op is None:
        return ""
    h = (getattr(op, "blockchain_tx_hash", None) or "").strip()
    if not h.startswith("0x"):
        return ""
    route = getattr(op, "route", "") or ""
    tn = bool(testnet) if testnet is not None else False
    if route == "usdc_arbitrum":
        base = "https://sepolia.arbiscan.io" if tn else "https://arbiscan.io"
    elif route == "eth_ethereum":
        base = "https://sepolia.etherscan.io" if tn else "https://etherscan.io"
    else:
        return ""
    return f"{base}/tx/{h}"
