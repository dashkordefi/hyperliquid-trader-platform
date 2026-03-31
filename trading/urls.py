from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("wallets/", views.wallet_select, name="wallet_select"),
    path("wallets/<int:pk>/delete/", views.wallet_delete, name="wallet_delete"),
    path("wallets/<int:pk>/", views.wallet_detail, name="wallet_detail"),
    path(
        "network/",
        views.set_hyperliquid_network,
        name="set_hyperliquid_network",
    ),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("trades/", views.trade_history, name="trade_history"),
    path("funds/history/", views.funds_history, name="funds_history"),
    path(
        "api/funds/bridge-poll/",
        views.funds_bridge_poll,
        name="funds_bridge_poll",
    ),
    path(
        "dashboard/usdc-transfer/",
        views.usdc_class_transfer,
        name="usdc_class_transfer",
    ),
    path(
        "dashboard/spot-transfer/",
        views.spot_token_transfer,
        name="spot_token_transfer",
    ),
    path("dashboard/place-order/", views.place_order, name="place_order"),
    path(
        "dashboard/cancel-order/",
        views.cancel_open_order,
        name="cancel_open_order",
    ),
    path(
        "dashboard/close-perp/market/",
        views.close_perp_market,
        name="close_perp_market",
    ),
    path(
        "dashboard/close-perp/limit/",
        views.close_perp_limit,
        name="close_perp_limit",
    ),
    path("deposit/", views.operation_request_view, {"kind": "deposit"}, name="deposit_request"),
    path("withdraw/", views.operation_request_view, {"kind": "withdraw"}, name="withdraw_request"),
    path(
        "dashboard/funds/<int:pk>/ack/",
        views.funds_operation_ack,
        name="funds_operation_ack",
    ),
    path(
        "dashboard/funds/<int:pk>/delete/",
        views.funds_operation_delete,
        name="funds_operation_delete",
    ),
    path("approvals/", views.approvals_list, name="approvals_list"),
    path("approvals/<int:pk>/", views.approval_action, name="approval_action"),
]
