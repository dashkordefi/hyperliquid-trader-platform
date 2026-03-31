"""
Клиент POST https://api.hyperliquid.xyz/info — копия для проекта.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping, MutableMapping, Optional, Union

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class HyperliquidInfoError(Exception):
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class HyperliquidInfoClient:
    def __init__(self, base_url: str = DEFAULT_INFO_URL, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, body: Mapping[str, Any]) -> Any:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_text = e.read().decode("utf-8", errors="replace")
            raise HyperliquidInfoError(
                f"HTTP {e.code}: {e.reason}",
                status=e.code,
                body=err_text,
            ) from e
        except urllib.error.URLError as e:
            raise HyperliquidInfoError(f"Сеть: {e.reason}") from e

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise HyperliquidInfoError(f"Не JSON: {raw[:200]!r}") from e

    @staticmethod
    def _with_type(
        type_name: str,
        extra: Optional[MutableMapping[str, Any]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"type": type_name}
        if extra:
            body.update(extra)
        for k, v in kwargs.items():
            if v is not None:
                body[k] = v
        return body

    def meta(self, dex: Optional[str] = None) -> Any:
        return self._post(self._with_type("meta", dex=dex))

    def all_perp_metas(self) -> Any:
        """Все perp-dex (нативный + HIP-3 и др.); см. allPerpMetas в доке HL."""
        return self._post(self._with_type("allPerpMetas"))

    def perp_dexs(self) -> Any:
        """Список perp-dex (имена деплоеров и т.д.); опционально для отладки."""
        return self._post(self._with_type("perpDexs"))

    def spot_meta(self) -> Any:
        return self._post(self._with_type("spotMeta"))

    def meta_and_asset_ctxs(self, dex: Optional[str] = None) -> Any:
        """Перп: [meta, assetCtxs] — в ctx oraclePx, markPx, midPx и т.д."""
        if dex is not None and dex != "":
            return self._post(self._with_type("metaAndAssetCtxs", dex=dex))
        return self._post(self._with_type("metaAndAssetCtxs"))

    def spot_meta_and_asset_ctxs(self) -> Any:
        """Спот: [spotMeta, assetCtxs] — в ctx markPx, midPx (без oracle)."""
        return self._post(self._with_type("spotMetaAndAssetCtxs"))

    def clearinghouse_state(self, user: str) -> Any:
        return self._post(self._with_type("clearinghouseState", user=user))

    def spot_clearinghouse_state(self, user: str) -> Any:
        return self._post(self._with_type("spotClearinghouseState", user=user))

    def all_mids(self, dex: Optional[str] = None) -> Any:
        return self._post(self._with_type("allMids", dex=dex))

    def open_orders(self, user: str, dex: Optional[str] = None) -> Any:
        return self._post(self._with_type("openOrders", user=user, dex=dex))

    def frontend_open_orders(self, user: str, dex: Optional[str] = None) -> Any:
        return self._post(self._with_type("frontendOpenOrders", user=user, dex=dex))

    def user_fills(self, user: str, aggregate_by_time: bool = False) -> Any:
        """История исполнений (до ~2000 последних записей)."""
        body: dict[str, Any] = {"type": "userFills", "user": user}
        if aggregate_by_time:
            body["aggregateByTime"] = True
        return self._post(body)

    def l2_book(
        self,
        coin: str,
        n_sig_figs: Optional[int] = None,
        mantissa: Optional[int] = None,
    ) -> Any:
        return self._post(
            self._with_type("l2Book", coin=coin, nSigFigs=n_sig_figs, mantissa=mantissa)
        )

    def candle_snapshot(
        self,
        coin: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
    ) -> Any:
        req = {
            "coin": coin,
            "interval": interval,
            "startTime": start_time_ms,
            "endTime": end_time_ms,
        }
        return self._post(self._with_type("candleSnapshot", req=req))
