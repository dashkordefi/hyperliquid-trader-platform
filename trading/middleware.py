from __future__ import annotations

from typing import Callable

from django.http import HttpRequest, HttpResponse

from .hl_network import apply_request_network, reset_network_context


class HyperliquidNetworkMiddleware:
    """Поднимает contextvars для hl_read / ордеров: mainnet vs testnet из сессии."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        token = apply_request_network(request)
        try:
            return self.get_response(request)
        finally:
            reset_network_context(token)
