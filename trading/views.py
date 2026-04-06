from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import (
    ClosePerpLimitForm,
    ClosePerpMarketForm,
    CreateTraderWalletForm,
    FundsOperationForm,
    PlaceOrderForm,
    SpotTokenTransferForm,
    TraderWalletForm,
    UsdcClassTransferForm,
)
from .hl_network import hl_testnet_enabled
from .hl_read import (
    compute_perp_market_order_size,
    fetch_dashboard_data,
    fetch_candles_for_chart,
    fetch_l2_book_for_dashboard,
    fetch_perp_leverage_setting_for_update,
    fetch_usdc_transfer_max_amount,
    fetch_user_fills_rows,
    fetch_withdraw_limits,
    get_perp_symbol_choices,
    get_spot_symbol_choices,
    resolve_api_coin,
)
from .hyperliquid_account import HyperliquidAccount
from .funds_execution import try_execute_approved_deposit, try_execute_approved_withdraw
from .models import FundsOperationRequest, TraderWallet
from eth_account import Account

from .arbitrum_withdrawal import try_finalize_usdc_withdrawals_for_wallet_arbiscan
from .wallet_crypto import decrypt_trading_key, encrypt_trading_key

SESSION_WALLET_KEY = "active_trader_wallet_id"

GROUP_TRADER = "traders"
GROUP_COMPLIANCE = "compliance_approver"
GROUP_MIDDLEOFFICE = "middleoffice_approver"


def _is_trader(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=GROUP_TRADER).exists()


def _is_compliance(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=GROUP_COMPLIANCE).exists()


def _is_middleoffice(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name=GROUP_MIDDLEOFFICE).exists()


def _pending_usdc_arbitrum_bridge_count(user) -> int:
    return FundsOperationRequest.objects.filter(
        wallet__user=user,
        kind=FundsOperationRequest.Kind.WITHDRAW,
        route=FundsOperationRequest.Route.USDC_ARBITRUM,
        withdrawal_bridge_submitted_at__isnull=False,
        executed_at__isnull=True,
        rejected_at__isnull=True,
    ).count()


@login_required
@require_POST
def funds_bridge_poll(request: HttpRequest) -> HttpResponse:
    """
    Лёгкая финализация вывода USDC→Arbitrum через Arbiscan (вызывается JS раз в минуту).
    Не использует тяжёлый eth_getLogs по RPC.
    """
    if not _is_trader(request.user):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    total = 0
    for w in TraderWallet.objects.filter(user=request.user).order_by("pk"):
        try:
            total += try_finalize_usdc_withdrawals_for_wallet_arbiscan(
                w, max_http_calls=15
            )
        except Exception as e:
            logger.warning("funds_bridge_poll: %s", e)

    n = _pending_usdc_arbitrum_bridge_count(request.user)
    return JsonResponse(
        {
            "ok": True,
            "finalized_count": total,
            "pending_bridge_count": n,
        }
    )


def _spot_transfer_wallet_choices(
    wallet: TraderWallet,
) -> tuple[list[tuple[str, str]], bool]:
    """Список кошельков платформы (кроме текущего) для spotSend и флаг «есть куда переводить»."""
    other = list(TraderWallet.objects.exclude(pk=wallet.pk).order_by("label"))
    if not other:
        return [("", "— Нет других кошельков в системе —")], False
    return (
        [("", "Выберите кошелёк…")]
        + [(w.address.lower(), f"{w.label} — {w.address}") for w in other],
        True,
    )


def _active_wallet(request: HttpRequest) -> Optional[TraderWallet]:
    wid = request.session.get(SESSION_WALLET_KEY)
    if not wid:
        return None
    return TraderWallet.objects.filter(pk=wid, user=request.user).first()


def _safe_post_redirect(request: HttpRequest) -> str:
    """Безопасный next после POST (open redirect)."""
    next_url = (request.POST.get("next") or "").strip()
    default = reverse("dashboard")
    if not next_url:
        return default
    allowed_hosts = {request.get_host()}
    if url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts=allowed_hosts,
        require_https=request.is_secure(),
    ):
        return next_url
    return default


@login_required
@require_POST
def set_hyperliquid_network(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")
    mode = (request.POST.get("mode") or "").strip().lower()
    if mode in ("testnet", "mainnet"):
        request.session["hl_network"] = mode
    return redirect(_safe_post_redirect(request))


def _private_key_for_wallet(wallet: TraderWallet) -> Optional[str]:
    """Сначала ключ кошелька в БД, иначе общий из env (один ключ на весь деплой)."""
    if wallet.trading_key_encrypted:
        try:
            return decrypt_trading_key(wallet.trading_key_encrypted)
        except Exception:
            return None
    pk = getattr(settings, "HYPERLIQUID_TRADING_PRIVATE_KEY", "") or ""
    pk = pk.strip()
    return pk or None


@login_required
def landing(request: HttpRequest) -> HttpResponse:
    if _is_trader(request.user):
        return redirect("wallet_select")
    if _is_compliance(request.user) or _is_middleoffice(request.user):
        return redirect("approvals_list")
    return render(request, "trading/no_role.html")


@login_required
def wallet_select(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallets = TraderWallet.objects.filter(user=request.user)

    if request.method == "POST" and "select_wallet" in request.POST:
        pk = request.POST.get("wallet_id")
        w = get_object_or_404(TraderWallet, pk=pk, user=request.user)
        request.session[SESSION_WALLET_KEY] = w.pk
        messages.success(request, f"Активный аккаунт: {w.label}")
        return redirect("dashboard")

    if request.method == "POST" and "create_wallet" in request.POST:
        create_form = CreateTraderWalletForm(request.POST, prefix="create")
        if create_form.is_valid():
            acct = Account.create()
            pk_hex = acct.key.hex()
            if not pk_hex.startswith("0x"):
                pk_hex = "0x" + pk_hex
            addr = acct.address.lower()
            w = TraderWallet.objects.create(
                user=request.user,
                label=create_form.cleaned_data["label"],
                address=addr,
                trading_key_encrypted=encrypt_trading_key(pk_hex),
            )
            if TraderWallet.objects.filter(user=request.user).count() == 1:
                request.session[SESSION_WALLET_KEY] = w.pk
            messages.success(
                request,
                "Кошелёк создан. Адрес показан в списке; приватный ключ сохранён в базе в зашифрованном виде и не отображается.",
            )
            return redirect("wallet_select")
        form = TraderWalletForm(user=request.user, prefix="import")
    elif request.method == "POST" and "add_wallet" in request.POST:
        form = TraderWalletForm(request.POST, user=request.user, prefix="import")
        if form.is_valid():
            w = form.save(commit=False)
            w.user = request.user
            priv = (form.cleaned_data.get("trading_private_key") or "").strip()
            if priv:
                w.trading_key_encrypted = encrypt_trading_key(priv)
            w.save()
            if TraderWallet.objects.filter(user=request.user).count() == 1:
                request.session[SESSION_WALLET_KEY] = w.pk
            messages.success(request, "Кошелёк добавлен.")
            return redirect("wallet_select")
        create_form = CreateTraderWalletForm(prefix="create")
    else:
        form = TraderWalletForm(user=request.user, prefix="import")
        create_form = CreateTraderWalletForm(prefix="create")

    return render(
        request,
        "trading/wallet_select.html",
        {
            "wallets": wallets,
            "form": form,
            "create_form": create_form,
            "active_wallet_id": request.session.get(SESSION_WALLET_KEY),
        },
    )


@login_required
def wallet_detail(request: HttpRequest, pk: int) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = get_object_or_404(TraderWallet, pk=pk, user=request.user)
    return render(
        request,
        "trading/wallet_detail.html",
        {
            "wallet": wallet,
        },
    )


@login_required
@require_POST
def wallet_delete(request: HttpRequest, pk: int) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = get_object_or_404(TraderWallet, pk=pk, user=request.user)
    label = wallet.label
    if request.session.get(SESSION_WALLET_KEY) == wallet.pk:
        request.session.pop(SESSION_WALLET_KEY, None)
    wallet.delete()
    messages.success(request, f"Кошелёк «{label}» удалён из базы.")
    return redirect("wallet_select")


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.info(request, "Выберите кошелёк для торговли.")
        return redirect("wallet_select")

    market_type = (request.GET.get("market") or "perp").strip().lower()
    if market_type not in ("spot", "perp"):
        market_type = "perp"
    symbol = (request.GET.get("symbol") or "ETH").strip()

    data = fetch_dashboard_data(wallet.address, market_type, symbol)
    perp_symbols = get_perp_symbol_choices()
    spot_symbols = get_spot_symbol_choices()

    place_initial: dict[str, object] = {
        "market_type": market_type,
        "symbol": symbol,
    }
    if market_type == "perp":
        pl = data.get("perp_leverage") or {}
        place_initial["leverage"] = pl.get("initial", 20)
    else:
        place_initial["leverage"] = 1
    place_order_form = PlaceOrderForm(initial=place_initial)
    usdc_transfer_form = UsdcClassTransferForm()
    spot_wallet_choices, has_spot_transfer_destinations = _spot_transfer_wallet_choices(
        wallet
    )
    spot_transfer_form = SpotTokenTransferForm(
        prefix="spot",
        wallet_choices=spot_wallet_choices,
    )
    hl_trading_key_configured = bool(_private_key_for_wallet(wallet))

    return render(
        request,
        "trading/dashboard.html",
        {
            "wallet": wallet,
            "market_type": market_type,
            "symbol": symbol,
            "data": data,
            "perp_symbols": perp_symbols,
            "spot_symbols": spot_symbols,
            "place_order_form": place_order_form,
            "usdc_transfer_form": usdc_transfer_form,
            "spot_transfer_form": spot_transfer_form,
            "has_spot_transfer_destinations": has_spot_transfer_destinations,
            "hl_trading_key_configured": hl_trading_key_configured,
        },
    )


@login_required
def candles_api(request: HttpRequest) -> JsonResponse:
    """Свечи candleSnapshot для графика (без хранения — только HL Info API)."""
    if not _is_trader(request.user):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    market_type = (request.GET.get("market") or "perp").strip().lower()
    if market_type not in ("spot", "perp"):
        market_type = "perp"
    symbol = (request.GET.get("symbol") or "ETH").strip()
    interval = (request.GET.get("interval") or "15m").strip()
    if interval not in (
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "8h",
        "12h",
        "1d",
    ):
        interval = "15m"
    coin = resolve_api_coin(market_type, symbol)
    return JsonResponse(fetch_candles_for_chart(coin, interval=interval))


@login_required
def orderbook_api(request: HttpRequest) -> JsonResponse:
    """JSON l2Book для панели стакана (polling с дашборда)."""
    if not _is_trader(request.user):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    market_type = (request.GET.get("market") or "perp").strip().lower()
    if market_type not in ("spot", "perp"):
        market_type = "perp"
    symbol = (request.GET.get("symbol") or "ETH").strip()
    coin = resolve_api_coin(market_type, symbol)
    return JsonResponse(fetch_l2_book_for_dashboard(coin))


@login_required
def trade_history(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.info(request, "Выберите кошелёк для торговли.")
        return redirect("wallet_select")

    data = fetch_user_fills_rows(wallet.address)
    return render(
        request,
        "trading/trade_history.html",
        {"wallet": wallet, "data": data},
    )


@login_required
def funds_history(request: HttpRequest) -> HttpResponse:
    """Открытые и завершённые заявки на депозит/вывод по всем кошелькам пользователя."""
    if not _is_trader(request.user):
        return redirect("landing")
    base = FundsOperationRequest.objects.filter(wallet__user=request.user).select_related(
        "wallet", "compliance_approved_by", "middleoffice_approved_by"
    )
    pending_operations = list(
        base.filter(executed_at__isnull=True, rejected_at__isnull=True).order_by(
            "-created_at"
        )[:100]
    )
    completed_operations = list(
        base.filter(Q(executed_at__isnull=False) | Q(rejected_at__isnull=False)).order_by(
            "-created_at"
        )[:300]
    )
    return render(
        request,
        "trading/funds_history.html",
        {
            "pending_operations": pending_operations,
            "completed_operations": completed_operations,
        },
    )


@login_required
@require_POST
def usdc_class_transfer(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.error(request, "Выберите кошелёк.")
        return redirect("wallet_select")

    form = UsdcClassTransferForm(request.POST)
    if not form.is_valid():
        for msg in form.errors.values():
            for m in msg:
                messages.error(request, m)
        return redirect(_safe_post_redirect(request))

    pk = _private_key_for_wallet(wallet)
    if not pk:
        messages.error(
            request,
            "Нет ключа для этого кошелька: задайте ключ при создании кошелька "
            "или HYPERLIQUID_TRADING_PRIVATE_KEY в окружении.",
        )
        return redirect(_safe_post_redirect(request))

    try:
        account = HyperliquidAccount(
            private_key=pk,
            testnet=hl_testnet_enabled(),
        )
    except Exception as e:
        messages.error(request, f"Не удалось инициализировать аккаунт: {e}")
        return redirect(_safe_post_redirect(request))

    if account.address.lower() != wallet.address.lower():
        messages.error(
            request,
            "Адрес ключа не совпадает с выбранным кошельком.",
        )
        return redirect(_safe_post_redirect(request))

    direction = form.cleaned_data["direction"]
    if form.cleaned_data.get("transfer_full"):
        max_amt = fetch_usdc_transfer_max_amount(wallet.address, direction)
        if max_amt is None or max_amt <= 0:
            messages.error(
                request,
                "Нет доступного баланса для перевода в этом направлении.",
            )
            return redirect(_safe_post_redirect(request))
        amt = float(max_amt)
    else:
        amt = float(form.cleaned_data["amount"])
    try:
        if direction == "perp_to_spot":
            account.transfer_usdc_perp_to_spot(amt)
        else:
            account.transfer_usdc_spot_to_perp(amt)
        messages.success(
            request,
            "Запрос на перевод USDC отправлен в Hyperliquid.",
        )
    except Exception as e:
        messages.error(request, str(e))

    return redirect(_safe_post_redirect(request))


@login_required
@require_POST
def spot_token_transfer(request: HttpRequest) -> HttpResponse:
    """Перевод спотового актива на другой адрес Hyperliquid (spotSend)."""
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.error(request, "Выберите кошелёк.")
        return redirect("wallet_select")

    spot_choices, has_dest = _spot_transfer_wallet_choices(wallet)
    if not has_dest:
        messages.error(request, "Нет других зарегистрированных кошельков для перевода.")
        return redirect(_safe_post_redirect(request))

    form = SpotTokenTransferForm(
        request.POST,
        prefix="spot",
        wallet_choices=spot_choices,
    )
    if not form.is_valid():
        for msg in form.errors.values():
            for m in msg:
                messages.error(request, m)
        return redirect(_safe_post_redirect(request))

    pk = _private_key_for_wallet(wallet)
    if not pk:
        messages.error(
            request,
            "Нет ключа для этого кошелька: задайте ключ при создании кошелька "
            "или HYPERLIQUID_TRADING_PRIVATE_KEY в окружении.",
        )
        return redirect(_safe_post_redirect(request))

    try:
        account = HyperliquidAccount(
            private_key=pk,
            testnet=hl_testnet_enabled(),
        )
    except Exception as e:
        messages.error(request, f"Не удалось инициализировать аккаунт: {e}")
        return redirect(_safe_post_redirect(request))

    if account.address.lower() != wallet.address.lower():
        messages.error(
            request,
            "Адрес ключа не совпадает с выбранным кошельком.",
        )
        return redirect(_safe_post_redirect(request))

    cleaned = form.cleaned_data
    token_coin = (cleaned.get("token_coin") or "").strip()
    if not token_coin:
        messages.error(request, "Не указан токен.")
        return redirect(_safe_post_redirect(request))

    dest = cleaned["destination"]
    allowed_dest = {
        w.address.lower()
        for w in TraderWallet.objects.exclude(pk=wallet.pk)
    }
    if dest not in allowed_dest:
        messages.error(request, "Получатель должен быть кошельком из списка платформы.")
        return redirect(_safe_post_redirect(request))

    try:
        account.transfer_spot_to_address(
            token_coin,
            float(cleaned["amount"]),
            dest,
        )
        messages.success(
            request,
            f"Перевод {cleaned['amount']} {token_coin} отправлен в Hyperliquid (spotSend).",
        )
    except Exception as e:
        messages.error(request, str(e))

    return redirect(_safe_post_redirect(request))


@login_required
@require_POST
def cancel_open_order(request: HttpRequest) -> HttpResponse:
    """Отмена открытого ордера по OID (Hyperliquid cancel)."""
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.error(request, "Выберите кошелёк.")
        return redirect("wallet_select")

    pk = _private_key_for_wallet(wallet)
    if not pk:
        messages.error(
            request,
            "Нет ключа для этого кошелька: задайте ключ при создании кошелька "
            "или HYPERLIQUID_TRADING_PRIVATE_KEY в окружении.",
        )
        return redirect(_safe_post_redirect(request))

    coin = (request.POST.get("coin") or "").strip()
    oid_raw = (request.POST.get("oid") or "").strip()
    try:
        oid = int(oid_raw)
    except (TypeError, ValueError):
        messages.error(request, "Некорректный номер ордера (OID).")
        return redirect(_safe_post_redirect(request))

    if not coin:
        messages.error(request, "Не указан инструмент (coin).")
        return redirect(_safe_post_redirect(request))

    try:
        account = HyperliquidAccount(
            private_key=pk,
            testnet=hl_testnet_enabled(),
        )
    except Exception as e:
        messages.error(request, f"Не удалось инициализировать аккаунт: {e}")
        return redirect(_safe_post_redirect(request))

    if account.address.lower() != wallet.address.lower():
        messages.error(
            request,
            "Адрес ключа не совпадает с выбранным кошельком.",
        )
        return redirect(_safe_post_redirect(request))

    try:
        account.cancel_order(coin, oid)
        messages.success(request, f"Ордер {oid} ({coin}) отменён.")
    except Exception as e:
        messages.error(request, str(e))

    return redirect(_safe_post_redirect(request))


def _resolve_close_perp_size(
    account: HyperliquidAccount,
    coin: str,
    close_full: bool,
    sz_form: Optional[float],
) -> float:
    szi = account.get_perp_position_szi(coin)
    if szi is None or abs(szi) < 1e-12:
        raise ValueError("Нет открытой позиции по этому инструменту.")
    abs_pos = abs(float(szi))
    if close_full:
        return abs_pos
    if sz_form is None:
        raise ValueError("Укажите размер закрытия.")
    sz = float(sz_form)
    if sz <= 0:
        raise ValueError("Размер должен быть больше нуля.")
    if sz > abs_pos + 1e-6:
        raise ValueError("Размер закрытия не может превышать размер позиции.")
    return sz


@login_required
@require_POST
def close_perp_market(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.error(request, "Выберите кошелёк.")
        return redirect("wallet_select")

    form = ClosePerpMarketForm(request.POST)
    if not form.is_valid():
        for msg in form.errors.values():
            for m in msg:
                messages.error(request, m)
        return redirect(_safe_post_redirect(request))

    pk = _private_key_for_wallet(wallet)
    if not pk:
        messages.error(
            request,
            "Нет ключа для этого кошелька: задайте ключ при создании кошелька "
            "или HYPERLIQUID_TRADING_PRIVATE_KEY в окружении.",
        )
        return redirect(_safe_post_redirect(request))

    try:
        account = HyperliquidAccount(
            private_key=pk,
            testnet=hl_testnet_enabled(),
        )
    except Exception as e:
        messages.error(request, f"Не удалось инициализировать аккаунт: {e}")
        return redirect(_safe_post_redirect(request))

    if account.address.lower() != wallet.address.lower():
        messages.error(
            request,
            "Адрес ключа не совпадает с выбранным кошельком.",
        )
        return redirect(_safe_post_redirect(request))

    coin = (form.cleaned_data.get("coin") or "").strip()
    if not coin:
        messages.error(request, "Не указан инструмент.")
        return redirect(_safe_post_redirect(request))

    close_full = bool(form.cleaned_data.get("close_full"))
    try:
        close_sz = _resolve_close_perp_size(
            account,
            coin,
            close_full,
            form.cleaned_data.get("sz"),
        )
    except ValueError as e:
        messages.error(request, str(e))
        return redirect(_safe_post_redirect(request))

    slip = 0.45 if hl_testnet_enabled() else 0.05
    try:
        result = account.close_perp_market(coin, close_sz, slippage=slip)
    except Exception as e:
        messages.error(request, str(e))
        return redirect(_safe_post_redirect(request))

    if isinstance(result, dict) and result.get("success"):
        messages.success(request, "Рыночная заявка на закрытие отправлена в Hyperliquid.")
    else:
        messages.info(request, str(result)[:500])

    return redirect(_safe_post_redirect(request))


@login_required
@require_POST
def close_perp_limit(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.error(request, "Выберите кошелёк.")
        return redirect("wallet_select")

    form = ClosePerpLimitForm(request.POST)
    if not form.is_valid():
        for msg in form.errors.values():
            for m in msg:
                messages.error(request, m)
        return redirect(_safe_post_redirect(request))

    pk = _private_key_for_wallet(wallet)
    if not pk:
        messages.error(
            request,
            "Нет ключа для этого кошелька: задайте ключ при создании кошелька "
            "или HYPERLIQUID_TRADING_PRIVATE_KEY в окружении.",
        )
        return redirect(_safe_post_redirect(request))

    try:
        account = HyperliquidAccount(
            private_key=pk,
            testnet=hl_testnet_enabled(),
        )
    except Exception as e:
        messages.error(request, f"Не удалось инициализировать аккаунт: {e}")
        return redirect(_safe_post_redirect(request))

    if account.address.lower() != wallet.address.lower():
        messages.error(
            request,
            "Адрес ключа не совпадает с выбранным кошельком.",
        )
        return redirect(_safe_post_redirect(request))

    coin = (form.cleaned_data.get("coin") or "").strip()
    if not coin:
        messages.error(request, "Не указан инструмент.")
        return redirect(_safe_post_redirect(request))

    close_full = bool(form.cleaned_data.get("close_full"))
    try:
        close_sz = _resolve_close_perp_size(
            account,
            coin,
            close_full,
            form.cleaned_data.get("sz"),
        )
    except ValueError as e:
        messages.error(request, str(e))
        return redirect(_safe_post_redirect(request))

    limit_px = float(form.cleaned_data["limit_px"])
    try:
        result = account.close_perp_limit(coin, close_sz, limit_px)
    except Exception as e:
        messages.error(request, str(e))
        return redirect(_safe_post_redirect(request))

    if isinstance(result, dict) and result.get("success"):
        oid = result.get("order_id")
        messages.success(
            request,
            "Лимитный ордер на закрытие отправлен."
            + (f" OID: {oid}" if oid is not None else ""),
        )
    else:
        messages.info(request, str(result)[:500])

    return redirect(_safe_post_redirect(request))


def _place_order_redirect(market_type: str, symbol: str) -> HttpResponse:
    from urllib.parse import urlencode

    q = urlencode({"market": market_type, "symbol": symbol})
    return redirect(f"{reverse('dashboard')}?{q}")


@login_required
@require_POST
def place_order(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.error(request, "Выберите кошелёк.")
        return redirect("wallet_select")

    form = PlaceOrderForm(request.POST)
    mt = (request.POST.get("market_type") or "perp").strip().lower()
    sym = (request.POST.get("symbol") or "ETH").strip()
    if mt not in ("spot", "perp"):
        mt = "perp"

    if not form.is_valid():
        for msg in form.errors.values():
            for m in msg:
                messages.error(request, m)
        return _place_order_redirect(mt, sym)

    cleaned = form.cleaned_data
    pk = _private_key_for_wallet(wallet)
    if not pk:
        messages.error(
            request,
            "Нет ключа для этого кошелька: задайте ключ при создании кошелька "
            "или HYPERLIQUID_TRADING_PRIVATE_KEY в окружении.",
        )
        return _place_order_redirect(mt, sym)

    try:
        account = HyperliquidAccount(
            private_key=pk,
            testnet=hl_testnet_enabled(),
        )
    except Exception as e:
        messages.error(request, f"Не удалось инициализировать аккаунт: {e}")
        return _place_order_redirect(mt, sym)

    if account.address.lower() != wallet.address.lower():
        messages.error(
            request,
            "Адрес ключа не совпадает с выбранным кошельком: переключите активный кошелёк "
            "или проверьте переменную HYPERLIQUID_TRADING_PRIVATE_KEY.",
        )
        return _place_order_redirect(mt, sym)

    coin = resolve_api_coin(cleaned["market_type"], cleaned["symbol"])
    is_buy = cleaned["side"] == "buy"

    if cleaned["market_type"] == "perp":
        lev = int(cleaned["leverage"])
        lev_setting = fetch_perp_leverage_setting_for_update(wallet.address, coin)
        mx = lev_setting.get("max")
        if mx is not None and lev > mx:
            messages.error(
                request,
                f"Плечо не может быть больше {mx}× для этого инструмента.",
            )
            return _place_order_redirect(mt, sym)
        try:
            account.update_leverage(coin, lev, is_cross=lev_setting["is_cross"])
        except Exception as e:
            messages.error(request, f"Не удалось установить плечо: {e}")
            return _place_order_redirect(mt, sym)

    if cleaned["market_type"] == "perp" and cleaned["order_kind"] == "market":
        try:
            size = compute_perp_market_order_size(
                wallet.address, coin, int(cleaned["leverage"])
            )
        except ValueError as e:
            messages.error(request, str(e))
            return _place_order_redirect(mt, sym)
    else:
        size = float(cleaned["sz"])

    def _place_market() -> dict:
        """Market: mainnet spot — лестница slippage (IoC часто не сходит с 5%); testnet — как раньше."""
        mt = cleaned.get("market_type") or "perp"
        if hl_testnet_enabled():
            # У HL цена агрессивного market (IoC) не может отличаться от reference >80%; slippage >0.8 даёт отказ API.
            slips = [0.55, 0.7, 0.8] if mt == "spot" else [0.18, 0.45]
        elif mt == "spot":
            # Продажа базы: на bid часто меньше ликвидности — выше первый допуск (всё ≤0.8).
            slips = (
                [0.12, 0.2, 0.35, 0.5]
                if not is_buy
                else [0.08, 0.15, 0.25, 0.4]
            )
        else:
            slips = [0.05]
        for slip in slips:
            try:
                return account.place_order(
                    coin,
                    is_buy,
                    size,
                    price=None,
                    order_type="Market",
                    slippage=slip,
                )
            except Exception as e:
                if "could not immediately match" not in str(e).lower():
                    raise
                if slip == slips[-1]:
                    raise

    try:
        if cleaned["order_kind"] == "market":
            result = _place_market()
        else:
            result = account.place_order(
                coin,
                is_buy,
                size,
                price=float(cleaned["limit_px"]),
                order_type="Limit",
                time_in_force="Gtc",
            )
    except Exception as e:
        err = str(e)
        if "could not immediately match" in err.lower():
            if hl_testnet_enabled():
                err += (
                    " На testnet для market уже пробовали увеличенный допуск; если снова так — "
                    "в стакане, скорее всего, нет встречных заявок. Используйте Limit или другую пару."
                )
            elif cleaned.get("market_type") == "spot":
                err += (
                    " Для spot market уже пробовали несколько допусков проскальзывания; "
                    "попробуйте Limit по цене или пару с более глубоким стаканом."
                )
            else:
                err += (
                    " Рыночный ордер должен сразу пересечь стакан: попробуйте Limit по цене или пару с ликвидностью."
                )
        messages.error(request, err)
        return _place_order_redirect(mt, sym)

    if isinstance(result, dict) and result.get("success"):
        oid = result.get("order_id")
        st = result.get("status", "")
        messages.success(
            request,
            f"Ордер принят ({st})." + (f" ID: {oid}" if oid is not None else ""),
        )
    else:
        messages.info(request, str(result)[:500])

    return _place_order_redirect(mt, sym)


@login_required
def operation_request_view(request: HttpRequest, kind: str) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        return redirect("wallet_select")

    if kind not in ("deposit", "withdraw"):
        return redirect("dashboard")

    op_kind = (
        FundsOperationRequest.Kind.DEPOSIT
        if kind == "deposit"
        else FundsOperationRequest.Kind.WITHDRAW
    )

    withdraw_limits = None
    if kind == "withdraw":
        withdraw_limits = fetch_withdraw_limits(wallet.address)

    if request.method == "POST":
        post = request.POST.copy()
        raw_amt = (post.get("amount") or "").strip()
        if raw_amt:
            # Запятая/пробелы как разделители — иначе DecimalField часто даёт «Введите число».
            post["amount"] = raw_amt.replace(",", ".").replace(" ", "").replace("\u00a0", "")

        form = FundsOperationForm(
            post,
            is_withdraw=(kind == "withdraw"),
            withdraw_limits=withdraw_limits,
        )
        if form.is_valid():
            op = FundsOperationRequest.objects.create(
                wallet=wallet,
                kind=op_kind,
                route=form.cleaned_data["route"],
                amount=form.cleaned_data["amount"],
                note=form.cleaned_data.get("note") or "",
                hl_testnet=hl_testnet_enabled(),
            )
            if not getattr(settings, "FUNDS_REQUIRE_APPROVALS", True):
                if op.kind == FundsOperationRequest.Kind.WITHDRAW:
                    ok, exec_msg = try_execute_approved_withdraw(op, request)
                    if not ok:
                        messages.error(request, f"Вывод: {exec_msg}")
                    elif "отправлен" in exec_msg.lower():
                        messages.success(request, exec_msg)
                    elif "уже исполнена" in exec_msg.lower():
                        pass
                    else:
                        messages.info(request, exec_msg)
                else:
                    ok, dep_msg = try_execute_approved_deposit(op, request)
                    if not ok:
                        messages.error(request, f"Депозит: {dep_msg}")
                    elif "уже исполнена" in dep_msg.lower():
                        pass
                    else:
                        messages.success(request, dep_msg)
            elif op_kind == FundsOperationRequest.Kind.DEPOSIT:
                messages.success(
                    request,
                    "Заявка на депозит создана. После согласований compliance и middle office "
                    "транзакция в сеть отправится автоматически (нужен сохранённый ключ кошелька).",
                )
            else:
                messages.success(
                    request,
                    "Заявка создана. Нужны подтверждения compliance и middle office.",
                )
            return redirect("dashboard")
    else:
        form = FundsOperationForm(
            is_withdraw=(kind == "withdraw"),
            withdraw_limits=withdraw_limits,
        )

    ctx: dict = {
        "wallet": wallet,
        "kind": kind,
        "form": form,
        "withdraw_limits": withdraw_limits,
    }

    return render(request, "trading/operation_form.html", ctx)


@login_required
@require_POST
def funds_operation_ack(request: HttpRequest, pk: int) -> HttpResponse:
    """
    Трейдер подтверждает, что вывод/депозит по заявке завершён на стороне сети —
    проставляет executed_at, чтобы убрать строку из ленты (если авто-исполнение не обновило БД).
    """
    if not _is_trader(request.user):
        return redirect("landing")
    op = get_object_or_404(
        FundsOperationRequest, pk=pk, wallet__user=request.user
    )
    if op.executed_at:
        messages.info(request, "Эта заявка уже отмечена исполненной.")
        return redirect("dashboard")
    if op.rejected_at:
        messages.warning(request, "Заявка отклонена — подтверждение не требуется.")
        return redirect("dashboard")
    op.executed_at = timezone.now()
    update_fields = ["executed_at"]
    tx = (request.POST.get("tx_hash") or "").strip()
    if tx.startswith("0x") and len(tx) <= 80:
        op.blockchain_tx_hash = tx[:80]
        update_fields.append("blockchain_tx_hash")
    op.save(update_fields=update_fields)
    messages.success(
        request,
        "Заявка отмечена как выполненная. Строка в ленте скрыта.",
    )
    return redirect("dashboard")


@login_required
@require_POST
def funds_operation_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """
    Трейдер удаляет свою заявку (депозит/вывод), пока она не отмечена исполненной в БД.
    Не отменяет уже отправленный в сеть вывод — только запись в приложении.
    """
    if not _is_trader(request.user):
        return redirect("landing")
    op = get_object_or_404(
        FundsOperationRequest, pk=pk, wallet__user=request.user
    )
    if op.executed_at:
        messages.error(
            request,
            "Нельзя удалить заявку, уже отмеченную как исполненную.",
        )
        return redirect("dashboard")
    kind = op.get_kind_display()
    amt = op.amount
    op.delete()
    messages.success(request, f"Заявка удалена ({kind} {amt}).")
    return redirect("dashboard")


@login_required
def approvals_list(request: HttpRequest) -> HttpResponse:
    if not (_is_compliance(request.user) or _is_middleoffice(request.user)):
        messages.error(request, "Нет доступа к согласованиям.")
        return redirect("landing")

    qs = FundsOperationRequest.objects.select_related(
        "wallet", "wallet__user", "compliance_approved_by", "middleoffice_approved_by"
    ).order_by("-created_at")[:100]

    return render(
        request,
        "trading/approvals.html",
        {"requests": qs},
    )


@login_required
def approval_action(request: HttpRequest, pk: int) -> HttpResponse:
    op = get_object_or_404(FundsOperationRequest, pk=pk)

    if request.method != "POST":
        return redirect("approvals_list")

    action = request.POST.get("action")
    if action not in ("compliance", "middleoffice"):
        return redirect("approvals_list")

    now = timezone.now()
    if action == "compliance":
        if not _is_compliance(request.user):
            messages.error(request, "Нет роли compliance.")
            return redirect("approvals_list")
        if op.compliance_approved_at:
            messages.warning(request, "Compliance уже подтверждён.")
        else:
            op.compliance_approved_at = now
            op.compliance_approved_by = request.user
            op.save(update_fields=["compliance_approved_at", "compliance_approved_by"])
            messages.success(request, "Compliance: подтверждено.")
    else:
        if not _is_middleoffice(request.user):
            messages.error(request, "Нет роли middle office.")
            return redirect("approvals_list")
        if op.middleoffice_approved_at:
            messages.warning(request, "Middle office уже подтверждён.")
        else:
            op.middleoffice_approved_at = now
            op.middleoffice_approved_by = request.user
            op.save(
                update_fields=["middleoffice_approved_at", "middleoffice_approved_by"]
            )
            messages.success(request, "Middle office: подтверждено.")

    op.refresh_from_db()
    if op.both_approved():
        if op.kind == FundsOperationRequest.Kind.WITHDRAW:
            ok, exec_msg = try_execute_approved_withdraw(op, request)
            if not ok:
                messages.error(request, f"Исполнение вывода: {exec_msg}")
            elif "отправлен" in exec_msg.lower():
                messages.success(request, exec_msg)
            elif "уже исполнена" in exec_msg.lower():
                pass
            else:
                messages.info(request, exec_msg)
        elif op.kind == FundsOperationRequest.Kind.DEPOSIT:
            ok, dep_msg = try_execute_approved_deposit(op, request)
            if not ok:
                messages.error(request, f"Исполнение депозита: {dep_msg}")
            elif "уже исполнена" in dep_msg.lower():
                pass
            else:
                messages.success(request, dep_msg)

    return redirect("approvals_list")
