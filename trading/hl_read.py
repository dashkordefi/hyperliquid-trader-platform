"""Сборка табличных данных для дашборда из ответов Info API."""

from __future__ import annotations

import time
from typing import Any

from .hyperliquid_info import HyperliquidInfoClient, HyperliquidInfoError


def fetch_dashboard_data(address: str, coin: str) -> dict[str, Any]:
    client = HyperliquidInfoClient()
    out: dict[str, Any] = {
        "error": None,
        "spot_balances": [],
        "perp_summary": None,
        "perp_positions": [],
        "open_orders": [],
        "mid": None,
        "l2_book": None,
        "candles": None,
        "errors_extra": [],
    }
    try:
        spot = client.spot_clearinghouse_state(address)
        ch = client.clearinghouse_state(address)
        orders = client.frontend_open_orders(address)
        mids = client.all_mids()

        out["spot_balances"] = _spot_rows(spot)
        out["perp_summary"], out["perp_positions"] = _perp_rows(ch)
        out["open_orders"] = _order_rows(orders)
        if isinstance(mids, dict) and coin in mids:
            out["mid"] = mids.get(coin)
    except HyperliquidInfoError as e:
        out["error"] = str(e)
        return out

    try:
        out["l2_book"] = client.l2_book(coin)
    except HyperliquidInfoError as e:
        out["errors_extra"].append(f"l2Book: {e}")

    try:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - 7 * 24 * 60 * 60 * 1000
        out["candles"] = client.candle_snapshot(
            coin, "1h", start_time_ms=start_ms, end_time_ms=end_ms
        )
    except HyperliquidInfoError as e:
        out["errors_extra"].append(f"candleSnapshot: {e}")

    return out


def _spot_rows(spot: Any) -> list[dict[str, Any]]:
    rows = []
    if not isinstance(spot, dict):
        return rows
    for bal in spot.get("balances") or []:
        total = float(bal.get("total") or 0)
        if total == 0:
            continue
        rows.append(
            {
                "coin": bal.get("coin"),
                "token": bal.get("token"),
                "total": bal.get("total"),
                "hold": bal.get("hold"),
                "entry_ntl": bal.get("entryNtl"),
            }
        )
    return rows


def _perp_rows(ch: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(ch, dict):
        return None, []
    ms = ch.get("marginSummary") or {}
    summary = {
        "account_value": ms.get("accountValue"),
        "withdrawable": ms.get("withdrawable"),
        "total_margin_used": ms.get("totalMarginUsed"),
    }
    positions = []
    for a in ch.get("assetPositions") or []:
        p = a.get("position") or {}
        positions.append(
            {
                "coin": p.get("coin"),
                "szi": p.get("szi"),
                "entry_px": p.get("entryPx"),
                "position_value": p.get("positionValue"),
                "unrealized_pnl": p.get("unrealizedPnl"),
            }
        )
    return summary, positions


def _order_rows(orders: Any) -> list[dict[str, Any]]:
    if not isinstance(orders, list):
        return []
    rows = []
    for o in orders[:50]:
        rows.append(
            {
                "coin": o.get("coin"),
                "side": o.get("side"),
                "limit_px": o.get("limitPx"),
                "sz": o.get("sz"),
                "oid": o.get("oid"),
            }
        )
    return rows


def candle_table_preview(candles: Any, limit: int = 12) -> list[dict[str, Any]]:
    """Последние свечи для компактной таблицы (не график)."""
    if not isinstance(candles, list):
        return []
    tail = candles[-limit:] if len(candles) > limit else candles
    out = []
    for c in tail:
        if isinstance(c, dict):
            out.append(
                {
                    "time_ms": c.get("t"),
                    "open": c.get("o"),
                    "high": c.get("h"),
                    "low": c.get("l"),
                    "close": c.get("c"),
                    "volume": c.get("v"),
                }
            )
        elif isinstance(c, (list, tuple)) and len(c) >= 6:
            out.append(
                {
                    "time_ms": c[0],
                    "open": c[1],
                    "high": c[2],
                    "low": c[3],
                    "close": c[4],
                    "volume": c[5],
                }
            )
    return out
