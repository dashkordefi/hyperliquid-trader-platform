from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User

from .models import FundsOperationRequest, TraderWallet

# Переопределяем админку пользователя: удобный выбор групп (ролей) при создании аккаунтов.
admin.site.unregister(User)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    filter_horizontal = ("groups", "user_permissions")


@admin.register(TraderWallet)
class TraderWalletAdmin(admin.ModelAdmin):
    list_display = ("label", "address", "user", "created_at")
    search_fields = ("label", "address", "user__username")


@admin.register(FundsOperationRequest)
class FundsOperationRequestAdmin(admin.ModelAdmin):
    list_display = ("kind", "amount", "route", "wallet", "created_at", "compliance_approved_at", "middleoffice_approved_at")
    list_filter = ("kind", "route")
