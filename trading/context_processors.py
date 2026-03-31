import logging

from .arbitrum_withdrawal import try_finalize_usdc_withdrawals_for_wallet
from .hl_network import hl_testnet_enabled
from .models import FundsOperationRequest, TraderWallet

logger = logging.getLogger(__name__)

SESSION_ACTIVE_WALLET = "active_trader_wallet_id"


def funds_operation_feed(request):
    """
    Лента заявок на депозит/вывод для активного кошелька трейдера (шапка интерфейса торговли).
    """
    out = {
        "funds_operation_feed": None,
        "funds_operation_feed_wallet": None,
    }
    if not request.user.is_authenticated:
        return out
    g = set(request.user.groups.values_list("name", flat=True))
    if not (request.user.is_superuser or "traders" in g):
        return out
    wid = request.session.get(SESSION_ACTIVE_WALLET)
    if not wid:
        return out
    wallet = TraderWallet.objects.filter(pk=wid, user=request.user).first()
    if not wallet:
        return out
    out["funds_operation_feed_wallet"] = wallet.label
    # USDC→Arbitrum: закрываем заявку по событию FinalizedWithdrawal на Bridge2.
    try:
        try_finalize_usdc_withdrawals_for_wallet(wallet)
    except Exception as e:
        logger.warning("Проверка FinalizedWithdrawal на Arbitrum: %s", e)
    # Только «открытые» заявки: после исполнения (HL) или отклонения строка из ленты убирается.
    out["funds_operation_feed"] = list(
        FundsOperationRequest.objects.filter(
            wallet=wallet,
            executed_at__isnull=True,
            rejected_at__isnull=True,
        )
        .select_related("compliance_approved_by", "middleoffice_approved_by")
        .order_by("-created_at")[:20]
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
