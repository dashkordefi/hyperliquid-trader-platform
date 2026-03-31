"""Сборка табличных данных для дашборда из ответов Info API."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from django.core.cache import cache

from .hl_network import hl_testnet_enabled, hyperliquid_info_url
from .hyperliquid_info import HyperliquidInfoClient, HyperliquidInfoError

# Списки монет для combobox (Info API); не дергать HL на каждый запрос страницы.
_SYMBOL_CACHE_TTL = 600  # секунд


def _info_client() -> HyperliquidInfoClient:
    """Info endpoint: mainnet или testnet (сессия или env)."""
    return HyperliquidInfoClient(base_url=hyperliquid_info_url())


def _cache_net_suffix() -> str:
    return "_tn" if hl_testnet_enabled() else ""


def _spot_pair_label_from_tokens(tokens: list[Any], token_idx: Any) -> Optional[str]:
    """BASE/QUOTE из spotMeta.tokens и индексов пары."""
    if not isinstance(token_idx, (list, tuple)) or len(token_idx) < 2:
        return None
    b, q = token_idx[0], token_idx[1]
    if not isinstance(b, int) or not isinstance(q, int):
        return None
    if b >= len(tokens) or q >= len(tokens):
        return None
    tb, tq = tokens[b], tokens[q]
    if not isinstance(tb, dict) or not isinstance(tq, dict):
        return None
    bn, qn = tb.get("name"), tq.get("name")
    if not bn or not qn:
        return None
    return f"{bn}/{qn}"


def get_spot_market_meta() -> dict[str, Any]:
    """
    Читаемые названия спот-пар (BASE/QUOTE) + маппинг в coin для API (@N или PURR/USDC).
    В allMids ключи в основном @N; подставляем по паре из токенов.
    Список labels для подсказок в UI — только пары к USDC (иначе при вводе «HYPE» всплывает десятки HYPE/…).
    Маппинг to_api по-прежнему включает все пары для resolve_api_coin.
    """
    cache_key = f"hl_spot_market_meta_v5_usdc_labels{_cache_net_suffix()}"

    def _load() -> dict[str, Any]:
        try:
            client = _info_client()
            sm = client.spot_meta()
            tokens = sm.get("tokens") or []
            universe = sm.get("universe") or []
        except HyperliquidInfoError:
            return {"labels": [], "to_api": {}}

        to_api: dict[str, str] = {}
        labels_set: set[str] = set()

        for u in universe:
            if not isinstance(u, dict):
                continue
            name = u.get("name")
            if not name:
                continue
            name_s = str(name)
            pair = _spot_pair_label_from_tokens(tokens, u.get("tokens"))

            to_api[name_s] = name_s

            if pair:
                pair_s = str(pair)
                to_api[pair_s] = name_s
                # Подсказки: только USDC-quoted (основной рынок HL).
                parts = pair_s.split("/", 1)
                if len(parts) == 2 and parts[1].upper() == "USDC":
                    labels_set.add(pair_s)
            else:
                labels_set.add(name_s)

        labels = sorted(labels_set)
        return {"labels": labels, "to_api": to_api}

    return cache.get_or_set(cache_key, _load, _SYMBOL_CACHE_TTL)


def resolve_api_coin(market_type: str, symbol: str) -> str:
    """
    Строка coin для API Hyperliquid.
    Perp: ETH или HIP-3 вида xyz:TSLA (регистр префикса dex важен — не полный .upper()).
    Spot: HFUN/USDC -> @N; короткий тикер ETH -> UETH/USDC/@151 (allMids знает только @N и PURR/USDC).
    """
    s = (symbol or "").strip()
    if not s:
        return "PURR/USDC" if market_type == "spot" else "ETH"
    if market_type == "perp":
        if ":" in s:
            return s
        return s.upper()

    meta = get_spot_market_meta()
    to_api: dict[str, str] = meta.get("to_api") or {}
    if s in to_api:
        return to_api[s]
    if "/" in s:
        up = s.strip()
        if up in to_api:
            return to_api[up]
        parts = up.upper().split("/", 1)
        if len(parts) == 2:
            a, b = parts[0], parts[1]
            for candidate in (f"{a}/{b}", f"U{a}/{b}"):
                if candidate in to_api:
                    return to_api[candidate]
        return to_api.get(up, up)

    base = s.upper()
    # Часто на HL спот — UBTC/USDC, UETH/USDC (обёрнутые токены), а не BTC/USDC.
    for candidate in (
        f"{base}/USDC",
        f"U{base}/USDC",
        f"{base}/USDH",
        f"U{base}/USDH",
    ):
        if candidate in to_api:
            return to_api[candidate]
    return f"{base}/USDC"


def get_perp_symbol_choices() -> list[str]:
    """
    Все тикеры перпов: allPerpMetas — по каждому dex (в т.ч. HIP-3 как xyz:TSLA).
    Один запрос type=allPerpMetas, не только нативный meta().
    """

    def _load() -> list[str]:
        try:
            client = _info_client()
            data = client.all_perp_metas()
            if not isinstance(data, list):
                return []
            names: list[str] = []
            for dex_meta in data:
                if not isinstance(dex_meta, dict):
                    continue
                for u in dex_meta.get("universe") or []:
                    if isinstance(u, dict) and u.get("name"):
                        names.append(str(u["name"]))
            return sorted(set(names))
        except HyperliquidInfoError:
            return []

    return cache.get_or_set(
        f"hl_perp_symbols_v2_all_dex{_cache_net_suffix()}",
        _load,
        _SYMBOL_CACHE_TTL,
    )


def get_spot_symbol_choices() -> list[str]:
    """Список читаемых спот-пар (BASE/QUOTE), без сырых @N — см. get_spot_market_meta."""
    return list(get_spot_market_meta().get("labels") or [])


def market_display_label(market_type: str, symbol: str) -> str:
    s = (symbol or "").strip() or "ETH"
    if market_type == "perp":
        shown = s if ":" in s else s.upper()
        return f"{shown} · perp"
    if "/" in s:
        return f"{s} · spot"
    return f"{s.upper()}/USDC · spot"


def spot_token_display_name(symbol: str) -> str:
    """
    Короткое имя базового токена для UI (ETH, SOL, PURR), без индекса @N.
    """
    s = (symbol or "").strip()
    if not s:
        return "—"
    if "/" in s:
        base = s.split("/", 1)[0].strip().upper()
    else:
        base = s.upper()
    # Обёртки HL: UETH → ETH, UBTC → BTC
    if len(base) > 1 and base.startswith("U") and base[1:].isalnum():
        base = base[1:]
    return base


def _perp_oracle_reference_px(client: HyperliquidInfoClient, instrument: str) -> Any:
    """oraclePx из metaAndAssetCtxs (reference price для перпов). HIP-3: dex:COIN."""
    if ":" in instrument:
        dex, coin = instrument.split(":", 1)
    else:
        dex, coin = None, instrument
    try:
        if dex:
            resp = client.meta_and_asset_ctxs(dex=dex)
        else:
            resp = client.meta_and_asset_ctxs()
    except HyperliquidInfoError:
        return None
    if not isinstance(resp, list) or len(resp) < 2:
        return None
    meta, ctxs = resp[0], resp[1]
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        return None
    universe = meta.get("universe") or []
    idx: Optional[int] = None
    for i, u in enumerate(universe):
        if isinstance(u, dict) and str(u.get("name") or "") == coin:
            idx = i
            break
    if idx is None or idx >= len(ctxs):
        return None
    actx = ctxs[idx]
    if not isinstance(actx, dict):
        return None
    return actx.get("oraclePx")


def _spot_mark_reference_px(client: HyperliquidInfoClient, instrument: str) -> Any:
    """markPx из spotMetaAndAssetCtxs как reference (у спота в доке нет oraclePx)."""
    try:
        resp = client.spot_meta_and_asset_ctxs()
    except HyperliquidInfoError:
        return None
    if not isinstance(resp, list) or len(resp) < 2:
        return None
    meta, ctxs = resp[0], resp[1]
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        return None
    universe = meta.get("universe") or []
    idx: Optional[int] = None
    for i, u in enumerate(universe):
        if not isinstance(u, dict):
            continue
        if str(u.get("name") or "") == instrument:
            idx = i
            break
    if idx is None or idx >= len(ctxs):
        return None
    actx = ctxs[idx]
    if not isinstance(actx, dict):
        return None
    return actx.get("markPx")


def _mid_price_from_map(mids: dict[str, Any], coin: str) -> Any:
    """Цена из allMids по ключу coin; при отсутствии — по совпадению без учёта регистра."""
    if not coin or not mids:
        return None
    raw = mids.get(coin)
    if raw is not None:
        return raw
    cu = coin.upper()
    for k, v in mids.items():
        if isinstance(k, str) and k.upper() == cu:
            return v
    return None


def _f_any(x: Any) -> float:
    try:
        if x is None:
            return 0.0
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _spot_instrument_bases(instrument: str) -> set[str]:
    """
    Кандидаты coin/token из spotClearinghouseState для совпадения с инструментом
    (UBTC/USDC, @N и т.д.).
    """
    s = (instrument or "").strip()
    if not s:
        return set()
    if s.startswith("@"):
        meta = get_spot_market_meta()
        to_api: dict[str, str] = meta.get("to_api") or {}
        for label, api in to_api.items():
            if str(api) == s and "/" in str(label):
                return _spot_instrument_bases(str(label))
        return set()
    if "/" in s:
        b = s.split("/", 1)[0].strip().upper()
        out = {b}
        if b.startswith("U") and len(b) > 1:
            out.add(b[1:])
        else:
            out.add("U" + b)
        return out
    return {s.upper()}


def _perp_position_coin_matches_instrument(pos_coin: Any, instrument: str) -> bool:
    a = str(pos_coin or "").strip()
    b = (instrument or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if ":" in a and ":" in b:
        return a == b
    if ":" in b and ":" not in a:
        return a == b.split(":", 1)[-1]
    if ":" in a and ":" not in b:
        return b == a.split(":", 1)[-1]
    return a.upper() == b.upper()


def _fetch_perp_universe_entry_for_instrument(
    client: HyperliquidInfoClient, instrument: str
) -> Optional[dict[str, Any]]:
    """Строка universe из metaAndAssetCtxs для перпа (в т.ч. HIP-3 dex:COIN)."""
    if ":" in instrument:
        dex, coin = instrument.split(":", 1)
    else:
        dex, coin = None, instrument
    try:
        if dex:
            resp = client.meta_and_asset_ctxs(dex=dex)
        else:
            resp = client.meta_and_asset_ctxs()
    except HyperliquidInfoError:
        return None
    if not isinstance(resp, list) or not resp:
        return None
    meta = resp[0]
    if not isinstance(meta, dict):
        return None
    for u in meta.get("universe") or []:
        if isinstance(u, dict) and str(u.get("name") or "") == coin:
            return u
    return None


def _universe_max_leverage_by_name(meta: dict[str, Any]) -> dict[str, int]:
    """Имя инструмента в universe → maxLeverage (как в fetch_perp_leverage_panel)."""
    out: dict[str, int] = {}
    for u in meta.get("universe") or []:
        if not isinstance(u, dict):
            continue
        name = u.get("name")
        if name is None:
            continue
        raw_mx = u.get("maxLeverage")
        if raw_mx is None:
            continue
        try:
            out[str(name)] = int(raw_mx)
        except (TypeError, ValueError):
            pass
    return out


def _perp_max_leverage_map_for_coins(
    client: HyperliquidInfoClient, coins: set[str]
) -> dict[str, Optional[int]]:
    """Для списка coin (в т.ч. dex:ASSET) — макс. плечо из meta; main и dex кэшируются."""
    out: dict[str, Optional[int]] = {}
    main_umap: Optional[dict[str, int]] = None
    dex_umaps: dict[str, dict[str, int]] = {}

    def _main_umap() -> dict[str, int]:
        nonlocal main_umap
        if main_umap is None:
            main_umap = {}
            try:
                resp = client.meta_and_asset_ctxs()
                if isinstance(resp, list) and resp and isinstance(resp[0], dict):
                    main_umap = _universe_max_leverage_by_name(resp[0])
            except HyperliquidInfoError:
                pass
        return main_umap

    def _dex_umap(dex: str) -> dict[str, int]:
        if dex not in dex_umaps:
            m: dict[str, int] = {}
            try:
                resp = client.meta_and_asset_ctxs(dex=dex)
                if isinstance(resp, list) and resp and isinstance(resp[0], dict):
                    m = _universe_max_leverage_by_name(resp[0])
            except HyperliquidInfoError:
                pass
            dex_umaps[dex] = m
        return dex_umaps[dex]

    for coin in coins:
        if not coin:
            continue
        if ":" in coin:
            dex = coin.split(":", 1)[0]
            umap = _dex_umap(dex)
        else:
            umap = _main_umap()
        out[coin] = umap.get(coin)
    return out


def _optional_int_leverage(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        x = float(v)
        if math.isnan(x):
            return None
        return int(x)
    except (TypeError, ValueError):
        return None


def _perp_leverage_current_from_aps(
    asset_positions: list[Any], instrument: str
) -> Optional[int]:
    for ap in asset_positions:
        if not isinstance(ap, dict):
            continue
        pos = ap.get("position", ap)
        if not isinstance(pos, dict):
            continue
        if not _perp_position_coin_matches_instrument(pos.get("coin"), instrument):
            continue
        lev = pos.get("leverage")
        if isinstance(lev, dict):
            v = lev.get("value")
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
    return None


def _leverage_type_from_aps(
    asset_positions: list[Any], instrument: str
) -> Optional[str]:
    for ap in asset_positions:
        if not isinstance(ap, dict):
            continue
        pos = ap.get("position", ap)
        if not isinstance(pos, dict):
            continue
        if not _perp_position_coin_matches_instrument(pos.get("coin"), instrument):
            continue
        lev = pos.get("leverage")
        if isinstance(lev, dict):
            t = lev.get("type")
            if t in ("cross", "isolated"):
                return t
    return None


def fetch_perp_leverage_panel(
    client: HyperliquidInfoClient,
    instrument: str,
    asset_positions: list[Any],
) -> dict[str, Any]:
    """
    maxLeverage, текущее плечо по позиции, флаги для updateLeverage (cross / isolated).
    initial — значение для ползунка при загрузке.
    """
    entry = _fetch_perp_universe_entry_for_instrument(client, instrument)
    mx: Optional[int] = None
    only_iso = False
    if isinstance(entry, dict):
        only_iso = bool(entry.get("onlyIsolated"))
        raw_mx = entry.get("maxLeverage")
        if raw_mx is not None:
            try:
                mx = int(raw_mx)
            except (TypeError, ValueError):
                mx = None
    cur = _perp_leverage_current_from_aps(asset_positions, instrument)
    lt = _leverage_type_from_aps(asset_positions, instrument)
    if only_iso:
        is_cross = False
    elif lt is not None:
        is_cross = lt == "cross"
    else:
        is_cross = True
    cap = mx if mx is not None else 125
    if cur is not None:
        initial = max(1, min(int(cur), cap))
    else:
        initial = max(1, min(cap, mx if mx is not None else 20))
    return {
        "max": mx,
        "current": cur,
        "only_isolated": only_iso,
        "is_cross": is_cross,
        "initial": initial,
    }


def _margin_account_value_usd(ch: Any) -> Optional[float]:
    """Стоимость счёта perp (USD) для «всё обеспечение в одну позицию» — marginSummary.accountValue."""
    if not isinstance(ch, dict):
        return None
    ms = ch.get("marginSummary")
    if not isinstance(ms, dict):
        return None
    av = ms.get("accountValue")
    if av is None:
        av = ms.get("totalRawUsd")
    v = _f_any(av)
    return v if v > 0 else None


def compute_perp_market_order_size(address: str, coin: str, leverage: int) -> float:
    """
    Размер маркет-ордера (в монете инструмента): вся маржа perp × плечо / mid.
    Номинал позиции в USDC ≈ accountValue × leverage (cross, одна позиция).
    """
    if leverage < 1:
        raise ValueError("Плечо должно быть не меньше 1.")
    client = _info_client()
    try:
        ch = client.clearinghouse_state(address)
    except HyperliquidInfoError as e:
        raise ValueError(f"Не удалось загрузить счёт: {e}") from e
    av = _margin_account_value_usd(ch)
    if av is None or av <= 0:
        raise ValueError("Нет доступной маржи на perp (account value).")
    mids: dict[str, Any] = {}
    try:
        m = client.all_mids()
        if isinstance(m, dict):
            mids = m
    except HyperliquidInfoError:
        pass
    mid = _mid_price_from_map(mids, coin)
    if mid is None and ":" in coin:
        try:
            dex = coin.split(":")[0]
            m2 = client.all_mids(dex=dex)
            if isinstance(m2, dict):
                mid = _mid_price_from_map(m2, coin)
        except HyperliquidInfoError:
            pass
    if mid is None or float(mid) <= 0:
        raise ValueError("Нет mid-цены для инструмента — не могу рассчитать размер.")
    notional_usd = float(av) * float(leverage)
    size = notional_usd / float(mid)
    if size <= 0 or math.isnan(size):
        raise ValueError("Расчётный размер не получился — проверьте маржу и цену.")
    return size


def fetch_perp_leverage_setting_for_update(
    address: str, instrument: str
) -> dict[str, Any]:
    """Те же данные, что для UI, по свежему clearinghouse (перед updateLeverage)."""
    client = _info_client()
    try:
        ch = client.clearinghouse_state(address)
    except HyperliquidInfoError:
        return {
            "max": None,
            "current": None,
            "only_isolated": False,
            "is_cross": True,
            "initial": 20,
        }
    aps = ch.get("assetPositions") or [] if isinstance(ch, dict) else []
    return fetch_perp_leverage_panel(client, instrument, aps)


def _display_unit_for_order(market_type: str, symbol: str, instrument: str) -> str:
    if market_type == "spot":
        return spot_token_display_name(symbol)
    ins = (instrument or "").strip()
    if ":" in ins:
        return ins.split(":", 1)[-1]
    return ins or "—"


def compute_order_max_sell(
    market_type: str,
    symbol: str,
    instrument: str,
    balance_rows: list[dict[str, Any]],
    perp_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Макс. объём продажи в монетах инструмента: спот — available по базовому токену;
    перп — long size (szi > 0) по выбранному coin.
    """
    unit = _display_unit_for_order(market_type, symbol, instrument)
    if market_type == "spot":
        bases = _spot_instrument_bases(instrument)
        if not bases:
            return {"sz": None, "unit": unit}
        for r in balance_rows:
            if str(r.get("account") or "") != "Spot":
                continue
            coin = str(r.get("coin") or "").upper()
            tok = str(r.get("token") or "").upper()
            if coin not in bases and tok not in bases:
                continue
            avail = r.get("available")
            if avail is None:
                avail = max(
                    0.0,
                    _f_any(r.get("total")) - _f_any(r.get("hold")),
                )
            else:
                avail = float(avail)
            return {"sz": max(0.0, avail), "unit": unit}
        return {"sz": 0.0, "unit": unit}

    for p in perp_positions:
        if _perp_position_coin_matches_instrument(p.get("coin"), instrument):
            sz = _f_any(p.get("size"))
            return {"sz": max(0.0, sz), "unit": unit}
    return {"sz": 0.0, "unit": unit}


def _unified_balance_rows(spot: Any, ch: Any) -> list[dict[str, Any]]:
    """
    Одна таблица как во вкладке Balances в приложении HL: все токены + колонка счёта (Spot / Perp).
    Спот: spotClearinghouseState.balances; перп USDC: marginSummary в clearinghouseState.
    """
    rows: list[dict[str, Any]] = []
    if isinstance(spot, dict):
        for bal in spot.get("balances") or []:
            total_f = _f_any(bal.get("total"))
            hold_f = _f_any(bal.get("hold"))
            if total_f == 0 and hold_f == 0:
                continue
            avail_f = max(0.0, total_f - hold_f)
            rows.append(
                {
                    "coin": bal.get("coin"),
                    "token": bal.get("token"),
                    "total": bal.get("total"),
                    "hold": bal.get("hold"),
                    "available": avail_f,
                    "entry_ntl": bal.get("entryNtl"),
                    "withdrawable": None,
                    "account": "Spot",
                }
            )

    if isinstance(ch, dict):
        ms = ch.get("marginSummary")
        if isinstance(ms, dict):
            tru = ms.get("totalRawUsd")
            av = ms.get("accountValue")
            tmu = ms.get("totalMarginUsed")
            w = ch.get("withdrawable")
            if w is None:
                w = ms.get("withdrawable")
            if any(
                _f_any(x) != 0
                for x in (tru, av, tmu, w)
            ):
                rows.append(
                    {
                        "coin": "USDC",
                        "token": "USDC",
                        "total": tru,
                        "hold": tmu,
                        "available": None,
                        "entry_ntl": None,
                        "withdrawable": w,
                        "account": "Perp",
                    }
                )

    _acct_order = {"Spot": 0, "Perp": 1}
    rows.sort(
        key=lambda r: (
            _acct_order.get(str(r.get("account") or ""), 9),
            str(r.get("coin") or ""),
        )
    )
    return rows


def _decimal_optional(v: Any) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        s = str(v).replace(",", "").replace(" ", "")
        return Decimal(s)
    except (InvalidOperation, ValueError, TypeError):
        return None


def fetch_usdc_transfer_max_amount(address: str, direction: str) -> Optional[Decimal]:
    """
    Актуальный максимум USDC для выбранного направления (как на дашборде:
    perp → spot: withdrawable с запасным perp_usdc; spot → perp: спот USDC).
    """
    client = _info_client()
    try:
        spot = client.spot_clearinghouse_state(address)
        ch = client.clearinghouse_state(address)
    except HyperliquidInfoError:
        return None
    snap = _usdc_snapshot_from_spot_ch(spot, ch)
    if direction == "perp_to_spot":
        w = snap.get("perp_withdrawable")
        if w is None:
            w = snap.get("perp_usdc")
        return _decimal_optional(w)
    if direction == "spot_to_perp":
        return _decimal_optional(snap.get("spot_usdc"))
    return None


def _usdc_snapshot_from_spot_ch(spot: Any, ch: Any) -> dict[str, Any]:
    """USDC на перп-счёте (totalRawUsd) и суммарно на споте — для панели перевода."""
    snap: dict[str, Any] = {
        "perp_usdc": None,
        "spot_usdc": None,
        "perp_withdrawable": None,
    }
    if isinstance(ch, dict):
        ms = ch.get("marginSummary")
        if isinstance(ms, dict):
            snap["perp_usdc"] = ms.get("totalRawUsd")
        w = ch.get("withdrawable")
        if w is None and isinstance(ms, dict):
            w = ms.get("withdrawable")
        snap["perp_withdrawable"] = w
    spot_total = 0.0
    if isinstance(spot, dict):
        for bal in spot.get("balances") or []:
            if not isinstance(bal, dict):
                continue
            tok = str(bal.get("token") or "").upper()
            coin = str(bal.get("coin") or "").upper()
            if tok == "USDC" or coin == "USDC":
                spot_total += _f_any(bal.get("total"))
    snap["spot_usdc"] = str(spot_total) if spot_total else "0"
    return snap


def _spot_eth_available(spot: Any) -> float:
    """Свободный ETH на споте (UETH/WETH/ETH) — верхняя граница для spotSend-вывода ETH."""
    total = 0.0
    if not isinstance(spot, dict):
        return total
    eth_names = {"ETH", "UETH", "WETH"}
    for bal in spot.get("balances") or []:
        if not isinstance(bal, dict):
            continue
        tok = str(bal.get("token") or "").upper()
        coin = str(bal.get("coin") or "").upper()
        if tok not in eth_names and coin not in eth_names:
            continue
        total_f = _f_any(bal.get("total"))
        hold_f = _f_any(bal.get("hold"))
        total += max(0.0, total_f - hold_f)
    return total


def fetch_withdraw_limits(address: str) -> dict[str, Any]:
    """
    Оценка макс. суммы к выводу по маршруту заявки (данные Info API).
    USDC Arbitrum — withdrawable с perp (bridge); ETH Ethereum — доступный ETH на spot.
    """
    out: dict[str, Any] = {
        "usdc_arbitrum": None,
        "eth_ethereum": None,
        "error": None,
    }
    client = _info_client()
    try:
        spot = client.spot_clearinghouse_state(address)
        ch = client.clearinghouse_state(address)
    except HyperliquidInfoError as e:
        out["error"] = str(e)
        return out
    snap = _usdc_snapshot_from_spot_ch(spot, ch)
    w = snap.get("perp_withdrawable")
    if w is not None:
        try:
            out["usdc_arbitrum"] = float(str(w).replace(",", ""))
        except (ValueError, TypeError):
            pass
    out["eth_ethereum"] = _spot_eth_available(spot)
    return out


def fetch_dashboard_data(address: str, market_type: str, symbol: str) -> dict[str, Any]:
    """Данные счёта, ордеров и mid-цена по выбранному инструменту (allMids)."""
    client = _info_client()
    instrument = resolve_api_coin(market_type, symbol)
    out: dict[str, Any] = {
        "error": None,
        "balance_rows": [],
        "perp_positions": [],
        "open_orders": [],
        "instrument_coin": instrument,
        "mid_price": None,
        "reference_price": None,
        "spot_display_token": spot_token_display_name(symbol)
        if market_type == "spot"
        else "",
        "usdc_snapshot": {
            "perp_usdc": None,
            "spot_usdc": None,
            "perp_withdrawable": None,
        },
        "order_max_sell": {"sz": None, "unit": ""},
        "perp_leverage": None,
        "perp_market_auto": None,
    }
    try:
        spot = client.spot_clearinghouse_state(address)
        ch = client.clearinghouse_state(address)
        orders = client.frontend_open_orders(address)

        out["balance_rows"] = _unified_balance_rows(spot, ch)
        out["usdc_snapshot"] = _usdc_snapshot_from_spot_ch(spot, ch)
        int_ids, coin_aliases = _build_order_coin_label_maps(client)
        out["open_orders"] = _order_rows(orders, int_ids, coin_aliases)

        mids: dict[str, Any] = {}
        try:
            m = client.all_mids()
            if isinstance(m, dict):
                mids = m
        except HyperliquidInfoError:
            pass

        mid_val = _mid_price_from_map(mids, instrument)
        if mid_val is None and ":" in instrument:
            try:
                dex = instrument.split(":")[0]
                m2 = client.all_mids(dex=dex)
                if isinstance(m2, dict):
                    mid_val = _mid_price_from_map(m2, instrument)
            except HyperliquidInfoError:
                pass
        out["mid_price"] = mid_val

        if market_type == "perp":
            out["reference_price"] = _perp_oracle_reference_px(client, instrument)
        else:
            out["reference_price"] = _spot_mark_reference_px(client, instrument)

        aps = ch.get("assetPositions") or [] if isinstance(ch, dict) else []
        out["perp_positions"] = perp_positions_like_notebook(aps, mids, client)
        out["order_max_sell"] = compute_order_max_sell(
            market_type, symbol, instrument, out["balance_rows"], out["perp_positions"]
        )
        if market_type == "perp":
            out["perp_leverage"] = fetch_perp_leverage_panel(client, instrument, aps)
            out["perp_market_auto"] = {
                "account_value_usd": _margin_account_value_usd(ch),
            }
        else:
            out["perp_leverage"] = None
            out["perp_market_auto"] = None
    except HyperliquidInfoError as e:
        out["error"] = str(e)
    return out


def flatten_perp_position(pos: dict[str, Any]) -> dict[str, Any]:
    """Как в hyperliquid_info_tests.ipynb: разворачивает leverage и cumFunding."""
    row = dict(pos)
    lev = row.pop("leverage", None)
    if isinstance(lev, dict):
        row["leverage_type"] = lev.get("type")
        row["leverage_value"] = lev.get("value")
        row["leverage_rawUsd"] = lev.get("rawUsd")
    cf = row.pop("cumFunding", None)
    if isinstance(cf, dict):
        row["funding_allTime"] = cf.get("allTime")
        row["funding_sinceOpen"] = cf.get("sinceOpen")
        row["funding_sinceChange"] = cf.get("sinceChange")
    return row


def perp_positions_like_notebook(
    asset_positions: list[Any],
    mids: dict[str, Any],
    client: Optional[HyperliquidInfoClient] = None,
) -> list[dict[str, Any]]:
    """
    Те же колонки, что в ноутбуке perp_positions_like_app:
    coin, size, position value, entry price, mark price, liquidation price, margin;
    плюс leverage_current и max_leverage (из API и meta).
    Mark: из allMids[coin], иначе markPx из позиции.
    """
    rows: list[dict[str, Any]] = []
    for ap in asset_positions:
        if not isinstance(ap, dict):
            continue
        pos = ap.get("position", ap)
        if not isinstance(pos, dict):
            continue
        flat = flatten_perp_position(pos)
        coin = flat.get("coin")
        mark = None
        if coin and mids:
            mark = mids.get(coin)
        if mark is None:
            mark = flat.get("markPx")
        szi_raw = flat.get("szi")
        abs_size = None
        position_side: Optional[str] = None
        try:
            szi_f = float(str(szi_raw).replace(",", ""))
            abs_size = abs(szi_f)
            if szi_f > 0:
                position_side = "long"
            elif szi_f < 0:
                position_side = "short"
        except (TypeError, ValueError):
            pass
        rows.append(
            {
                "coin": coin,
                "size": flat.get("szi"),
                "abs_size": abs_size,
                "position_side": position_side,
                "position_value": flat.get("positionValue"),
                "entry_price": flat.get("entryPx"),
                "mark_price": mark,
                "liquidation_price": flat.get("liquidationPx"),
                "margin": flat.get("marginUsed"),
                "leverage_current": _optional_int_leverage(flat.get("leverage_value")),
                "max_leverage": None,
            }
        )
    if client is not None and rows:
        coin_set = {str(r["coin"]) for r in rows if r.get("coin")}
        max_map = _perp_max_leverage_map_for_coins(client, coin_set)
        for r in rows:
            c = r.get("coin")
            if c:
                r["max_leverage"] = max_map.get(str(c))
    return rows


def _build_order_coin_label_maps(
    client: HyperliquidInfoClient,
) -> tuple[dict[int, str], dict[str, str]]:
    """
    Open orders отдают coin по-разному:
    - перп: «ETH», «xyz:…»
    - спот: числовой asset id (11137) ИЛИ строка «@1137» (индекс пары в spotMeta), см. HL docs.
    int_ids: asset id → подпись; aliases: строковый ключ («@1137», имя из meta) → та же подпись.
    """
    int_ids: dict[int, str] = {}
    aliases: dict[str, str] = {}
    try:
        sm = client.spot_meta()
        tokens = sm.get("tokens") or []
        for u in sm.get("universe") or []:
            if not isinstance(u, dict):
                continue
            idx = u.get("index")
            if not isinstance(idx, int):
                continue
            aid = idx + 10000
            pair = _spot_pair_label_from_tokens(tokens, u.get("tokens"))
            name = u.get("name")
            label = str(pair) if pair else (str(name) if name else str(aid))
            int_ids[aid] = label
            aliases[f"@{idx}"] = label
            if name is not None:
                aliases[str(name)] = label
    except HyperliquidInfoError:
        pass
    try:
        meta = client.meta()
        for i, asset in enumerate(meta.get("universe") or []):
            if isinstance(asset, dict) and asset.get("name"):
                nm = str(asset["name"])
                int_ids[i] = nm
                aliases[nm] = nm
    except HyperliquidInfoError:
        pass
    try:
        apm = client.all_perp_metas()
        if isinstance(apm, list):
            for dex_i, dex_meta in enumerate(apm):
                if not isinstance(dex_meta, dict):
                    continue
                offset = 0 if dex_i == 0 else 110000 + (dex_i - 1) * 10000
                for idx, asset in enumerate(dex_meta.get("universe") or []):
                    if isinstance(asset, dict) and asset.get("name"):
                        nm = str(asset["name"])
                        aid = idx + offset
                        int_ids[aid] = nm
                        aliases[nm] = nm
    except HyperliquidInfoError:
        pass
    return int_ids, aliases


def _order_coin_display(
    coin_raw: Any,
    int_ids: dict[int, str],
    aliases: dict[str, str],
) -> str:
    if coin_raw is None:
        return "—"
    if isinstance(coin_raw, (int, float)):
        aid = int(coin_raw)
        return int_ids.get(aid, str(aid))
    s = str(coin_raw).strip()
    if not s:
        return "—"
    if s in aliases:
        return aliases[s]
    if s.isdigit() or (s.startswith("-") and len(s) > 1 and s[1:].isdigit()):
        aid = int(s)
        return int_ids.get(aid, s)
    # спот: «@N» — индекс пары; если ключ не попал в aliases, пробуем asset id 10000+N
    if s.startswith("@") and len(s) > 1 and s[1:].isdigit():
        n = int(s[1:])
        aid = 10000 + n
        return int_ids.get(aid, aliases.get(s, s))
    return s


def _open_order_side_label(side: Any) -> str:
    """A/B в API → Sell/Buy (как в доке HL: B bid buy, A ask sell)."""
    s = str(side or "").strip().upper()
    if s == "B":
        return "Buy"
    if s == "A":
        return "Sell"
    return str(side) if side not in (None, "") else "—"


def _order_rows(
    orders: Any,
    int_ids: Optional[dict[int, str]] = None,
    aliases: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    if not isinstance(orders, list):
        return []
    iid = int_ids or {}
    als = aliases or {}
    rows = []
    for o in orders[:50]:
        if not isinstance(o, dict):
            continue
        rows.append(
            {
                "coin": _order_coin_display(o.get("coin"), iid, als),
                "side": _open_order_side_label(o.get("side")),
                "limit_px": o.get("limitPx"),
                "sz": o.get("sz"),
                "oid": o.get("oid"),
            }
        )
    return rows


def _fill_time_display(ts: Any) -> str:
    from datetime import datetime, timezone

    if ts is None:
        return "—"
    try:
        ms = int(ts)
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return str(ts)


def fetch_user_fills_rows(address: str) -> dict[str, Any]:
    """История сделок (userFills): coin и сторона — как у открытых ордеров."""
    client = _info_client()
    out: dict[str, Any] = {"rows": [], "error": None}
    try:
        raw = client.user_fills(address)
        if not isinstance(raw, list):
            out["error"] = "Неожиданный ответ userFills"
            return out
        int_ids, aliases = _build_order_coin_label_maps(client)
        combined: list[tuple[int, dict[str, Any]]] = []
        for f in raw:
            if not isinstance(f, dict):
                continue
            ts = f.get("time")
            try:
                ts_sort = int(ts) if ts is not None else 0
            except (TypeError, ValueError):
                ts_sort = 0
            combined.append(
                (
                    ts_sort,
                    {
                        "coin": _order_coin_display(f.get("coin"), int_ids, aliases),
                        "side": _open_order_side_label(f.get("side")),
                        "px": f.get("px"),
                        "sz": f.get("sz"),
                        "fee": f.get("fee"),
                        "fee_token": f.get("feeToken"),
                        "time_display": _fill_time_display(ts),
                        "dir": f.get("dir"),
                        "closed_pnl": f.get("closedPnl"),
                        "oid": f.get("oid"),
                        "crossed": f.get("crossed"),
                        "tid": f.get("tid"),
                    },
                )
            )
        combined.sort(key=lambda x: x[0], reverse=True)
        out["rows"] = [r[1] for r in combined]
    except HyperliquidInfoError as e:
        out["error"] = str(e)
    return out
