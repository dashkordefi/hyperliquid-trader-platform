from __future__ import annotations

from typing import Optional

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import FundsOperationForm, TraderWalletForm
from .hl_read import candle_table_preview, fetch_dashboard_data
from .models import FundsOperationRequest, TraderWallet

SESSION_WALLET_KEY = "active_trader_wallet_id"

GROUP_TRADER = "traders"
GROUP_COMPLIANCE = "compliance_approver"
GROUP_MIDDLEOFFICE = "middleoffice_approver"


def _is_trader(user) -> bool:
    return user.is_authenticated and user.groups.filter(name=GROUP_TRADER).exists()


def _is_compliance(user) -> bool:
    return user.is_authenticated and user.groups.filter(name=GROUP_COMPLIANCE).exists()


def _is_middleoffice(user) -> bool:
    return user.is_authenticated and user.groups.filter(name=GROUP_MIDDLEOFFICE).exists()


def _active_wallet(request: HttpRequest) -> Optional[TraderWallet]:
    wid = request.session.get(SESSION_WALLET_KEY)
    if not wid:
        return None
    return TraderWallet.objects.filter(pk=wid, user=request.user).first()


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
    if request.method == "POST":
        if "select_wallet" in request.POST:
            pk = request.POST.get("wallet_id")
            w = get_object_or_404(TraderWallet, pk=pk, user=request.user)
            request.session[SESSION_WALLET_KEY] = w.pk
            messages.success(request, f"Активный аккаунт: {w.label}")
            return redirect("dashboard")
        if "add_wallet" in request.POST:
            form = TraderWalletForm(request.POST)
            if form.is_valid():
                w = form.save(commit=False)
                w.user = request.user
                w.save()
                if TraderWallet.objects.filter(user=request.user).count() == 1:
                    request.session[SESSION_WALLET_KEY] = w.pk
                messages.success(request, "Кошелёк добавлен.")
                return redirect("wallet_select")
    else:
        form = TraderWalletForm()

    return render(
        request,
        "trading/wallet_select.html",
        {"wallets": wallets, "form": form},
    )


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    if not _is_trader(request.user):
        return redirect("landing")

    wallet = _active_wallet(request)
    if not wallet:
        messages.info(request, "Выберите кошелёк для торговли.")
        return redirect("wallet_select")

    coin = (request.GET.get("coin") or "ETH").strip()
    data = fetch_dashboard_data(wallet.address, coin)
    candle_rows = candle_table_preview(data.get("candles"))

    return render(
        request,
        "trading/dashboard.html",
        {
            "wallet": wallet,
            "coin": coin,
            "data": data,
            "candle_rows": candle_rows,
        },
    )


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

    if request.method == "POST":
        form = FundsOperationForm(request.POST)
        if form.is_valid():
            FundsOperationRequest.objects.create(
                wallet=wallet,
                kind=op_kind,
                route=form.cleaned_data["route"],
                amount=form.cleaned_data["amount"],
                note=form.cleaned_data.get("note") or "",
            )
            messages.success(
                request,
                "Заявка создана. Нужны подтверждения compliance и middle office.",
            )
            return redirect("dashboard")
    else:
        form = FundsOperationForm()

    return render(
        request,
        "trading/operation_form.html",
        {
            "wallet": wallet,
            "kind": kind,
            "form": form,
        },
    )


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

    if op.both_approved():
        messages.info(
            request,
            "Оба согласования получены — заявка готова к исполнению на стороне оркестрации HL.",
        )

    return redirect("approvals_list")
