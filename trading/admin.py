from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User

from .models import FundsOperationRequest, TraderWallet

# Переопределяем админку пользователя: группы через raw_id (минимальный виджет).
# filter_horizontal / autocomplete тянут тяжёлые шаблоны; на Python 3.14 + Django 4.2
# при рендере /admin/auth/user/add/ возможны падения в RequestContext (Render).
admin.site.unregister(User)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    raw_id_fields = ("groups",)
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "is_staff",
        "is_active",
        "list_groups",
    )
    list_filter = ("is_staff", "is_superuser", "is_active", "groups")
    search_fields = ("username", "first_name", "last_name", "email")

    @admin.display(description="Группы")
    def list_groups(self, obj):
        return ", ".join(obj.groups.values_list("name", flat=True)) or "—"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(TraderWallet)
class TraderWalletAdmin(admin.ModelAdmin):
    list_display = ("label", "address", "user", "has_trading_key", "created_at")
    search_fields = ("label", "address", "user__username")
    exclude = ("trading_key_encrypted",)

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ("label", "address", "user", "created_at")
        return ()

    @admin.display(description="Ключ ордеров", boolean=True)
    def has_trading_key(self, obj):
        return bool(obj.trading_key_encrypted)


@admin.register(FundsOperationRequest)
class FundsOperationRequestAdmin(admin.ModelAdmin):
    list_display = (
        "kind",
        "amount",
        "route",
        "hl_testnet",
        "wallet",
        "created_at",
        "compliance_approved_at",
        "middleoffice_approved_at",
        "withdrawal_bridge_submitted_at",
        "executed_at",
        "blockchain_tx_hash",
    )
    list_filter = ("kind", "route")
    search_fields = ("blockchain_tx_hash", "wallet__address", "wallet__label")
