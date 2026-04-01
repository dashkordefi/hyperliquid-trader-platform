from .hl_network import hl_testnet_enabled
from .models import FundsOperationRequest, TraderWallet

# Совпадает с trading.views.SESSION_WALLET_KEY
_SESSION_WALLET_KEY = "active_trader_wallet_id"


def active_trader_wallet_ctx(request):
    """Активный кошелёк из сессии — для строки в шапке рядом с HL Trader."""
    out = {"active_trader_wallet": None}
    if not request.user.is_authenticated:
        return out
    wid = request.session.get(_SESSION_WALLET_KEY)
    if not wid:
        return out
    g = set(request.user.groups.values_list("name", flat=True))
    if not (request.user.is_superuser or "traders" in g):
        return out
    w = TraderWallet.objects.filter(pk=wid, user=request.user).first()
    if w:
        out["active_trader_wallet"] = w
    return out


def funds_operation_feed(request):
    """
    Баннер при выводе USDC→Arbitrum до появления tx в сети.

    Скан FinalizedWithdrawal по RPC (eth_getLogs) не выполняется здесь: он блокирует
    воркер Gunicorn на минуты и даёт WORKER TIMEOUT. Обновление executed_at — через
    management command finalize_arbitrum_withdrawals (cron на Render).
    """
    out = {
        "funds_bridge_banner_ops": None,
    }
    if not request.user.is_authenticated:
        return out
    g = set(request.user.groups.values_list("name", flat=True))
    if not (request.user.is_superuser or "traders" in g):
        return out
    if not TraderWallet.objects.filter(user=request.user).exists():
        return out
    # Баннер: USDC уже ушёл с HL, ожидаем FinalizedWithdrawal на Bridge2 (пропадает после tx в сети).
    out["funds_bridge_banner_ops"] = list(
        FundsOperationRequest.objects.filter(
            wallet__user=request.user,
            kind=FundsOperationRequest.Kind.WITHDRAW,
            route=FundsOperationRequest.Route.USDC_ARBITRUM,
            withdrawal_bridge_submitted_at__isnull=False,
            executed_at__isnull=True,
            rejected_at__isnull=True,
        )
        .select_related("wallet")
        .order_by("-withdrawal_bridge_submitted_at")
    )
    return out


def hyperliquid_network(request):
    """Режим HL для шаблонов (кнопки Mainnet/Testnet, баннер)."""
    tn = hl_testnet_enabled()
    return {
        "hl_network_testnet": tn,
        "hl_network_mode": "testnet" if tn else "mainnet",
    }


def roles(request):
    if not request.user.is_authenticated:
        return {}
    u = request.user
    g = set(u.groups.values_list("name", flat=True))
    su = u.is_superuser
    return {
        "is_trader": su or "traders" in g,
        "is_compliance": su or "compliance_approver" in g,
        "is_middleoffice": su or "middleoffice_approver" in g,
    }
