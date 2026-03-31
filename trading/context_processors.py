import logging

from .arbitrum_withdrawal import try_finalize_usdc_withdrawals_for_wallet
from .hl_network import hl_testnet_enabled
from .models import FundsOperationRequest, TraderWallet

logger = logging.getLogger(__name__)


def funds_operation_feed(request):
    """
    Баннер при выводе USDC→Arbitrum до появления tx в сети. FinalizedWithdrawal — по каждому кошельку.
    """
    out = {
        "funds_bridge_banner_ops": None,
    }
    if not request.user.is_authenticated:
        return out
    g = set(request.user.groups.values_list("name", flat=True))
    if not (request.user.is_superuser or "traders" in g):
        return out
    wallets = list(TraderWallet.objects.filter(user=request.user).order_by("label"))
    if not wallets:
        return out
    for w in wallets:
        try:
            try_finalize_usdc_withdrawals_for_wallet(w)
        except Exception as e:
            logger.warning("Проверка FinalizedWithdrawal на Arbitrum: %s", e)
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
