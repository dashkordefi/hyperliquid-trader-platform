"""
Режим Hyperliquid (mainnet / testnet) на запрос: из сессии или из HYPERLIQUID_USE_TESTNET.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

from django.conf import settings
from django.http import HttpRequest

_flag: ContextVar[Optional[bool]] = ContextVar("_hl_testnet", default=None)


def _resolve_flag(request: HttpRequest) -> bool:
    tn = request.session.get("hl_network")
    if tn == "testnet":
        return True
    if tn == "mainnet":
        return False
    return bool(settings.HYPERLIQUID_USE_TESTNET)


def apply_request_network(request: HttpRequest) -> Token:
    """Вызывается из middleware; вернуть token для reset."""
    return _flag.set(_resolve_flag(request))


def reset_network_context(token: Token) -> None:
    _flag.reset(token)


def hl_testnet_enabled() -> bool:
    v = _flag.get()
    if v is None:
        return bool(settings.HYPERLIQUID_USE_TESTNET)
    return v


def hyperliquid_info_url() -> str:
    """Базовый URL Info API для текущего режима (сессия или env)."""
    if hl_testnet_enabled():
        return str(settings.HYPERLIQUID_TESTNET_INFO_URL)
    return str(settings.HYPERLIQUID_MAINNET_INFO_URL)
