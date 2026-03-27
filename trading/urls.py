from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("wallets/", views.wallet_select, name="wallet_select"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("deposit/", views.operation_request_view, {"kind": "deposit"}, name="deposit_request"),
    path("withdraw/", views.operation_request_view, {"kind": "withdraw"}, name="withdraw_request"),
    path("approvals/", views.approvals_list, name="approvals_list"),
    path("approvals/<int:pk>/", views.approval_action, name="approval_action"),
]
