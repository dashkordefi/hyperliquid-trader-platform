"""Фильтры шаблонов для торгового приложения."""

from __future__ import annotations

import re
from typing import Any

from django import template

register = template.Library()


def _normalize_tx_hash(raw: str) -> str:
    """
    В БД иногда пишется 64 hex-символа без 0x — Arbiscan ожидает полный 0x… в URL.
    """
    h = (raw or "").strip()
    if not h:
        return ""
    if h.startswith("0x"):
        body = h[2:]
    else:
        body = h
    if len(body) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", body):
        return ""
    return "0x" + body.lower()


@register.filter
def funds_tx_hash_display(op: Any) -> str:
    """Полный хэш с 0x для title/копирования; если формат неизвестен — как в БД."""
    raw = (getattr(op, "blockchain_tx_hash", None) or "").strip()
    n = _normalize_tx_hash(raw)
    return n if n else raw


def _op_testnet_flag(op: Any, template_flag: Any) -> bool:
    """Режим сети заявки (поле hl_testnet), иначе из шаблона (глобальный переключатель HL)."""
    if op is not None and getattr(op, "hl_testnet", None) is not None:
        return bool(op.hl_testnet)
    return bool(template_flag) if template_flag is not None else False


@register.filter
def funds_tx_explorer_url(op: Any, testnet: Any = None) -> str:
    """
    URL обозревателя блока для заявки депозита/вывода (по маршруту и hl_network_testnet).
    """
    if op is None:
        return ""
    h = _normalize_tx_hash(getattr(op, "blockchain_tx_hash", None) or "")
    if not h:
        return ""
    route = getattr(op, "route", "") or ""
    tn = _op_testnet_flag(op, testnet)
    if route == "usdc_arbitrum":
        base = "https://sepolia.arbiscan.io" if tn else "https://arbiscan.io"
    elif route == "eth_ethereum":
        base = "https://sepolia.etherscan.io" if tn else "https://etherscan.io"
    else:
        return ""
    return f"{base}/tx/{h}"


@register.filter
def funds_tx_explorer_label(op: Any, testnet: Any = None) -> str:
    """Подпись ссылки в обозреватель (без хэша в тексте)."""
    if op is None:
        return ""
    route = getattr(op, "route", "") or ""
    tn = _op_testnet_flag(op, testnet)
    if route == "usdc_arbitrum":
        return "Arbiscan (Sepolia)" if tn else "Arbiscan"
    if route == "eth_ethereum":
        return "Etherscan (Sepolia)" if tn else "Etherscan"
    return "Обозреватель"
