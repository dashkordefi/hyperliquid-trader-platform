from django.conf import settings
from django.db import models


class TraderWallet(models.Model):
    """Именованный кошелёк трейдера — с ним связывается сессия и запросы к HL."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trader_wallets",
    )
    label = models.CharField("Название аккаунта", max_length=120)
    address = models.CharField("Адрес 0x…", max_length=42)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["label"]
        verbose_name = "Кошелёк трейдера"
        verbose_name_plural = "Кошельки трейдеров"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "address"],
                name="unique_wallet_address_per_user",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.address[:10]}…)"


class FundsOperationRequest(models.Model):
    """Депозит или вывод — после двух аппрувов готов к исполнению (оркестрация HL)."""

    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Депозит на Hyperliquid"
        WITHDRAW = "withdraw", "Вывод с Hyperliquid"

    class Route(models.TextChoices):
        ETH_ETHEREUM = "eth_ethereum", "ETH → Ethereum (Unit)"
        USDC_ARBITRUM = "usdc_arbitrum", "USDC через bridge (Arbitrum)"

    wallet = models.ForeignKey(TraderWallet, on_delete=models.CASCADE, related_name="operations")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    route = models.CharField(max_length=32, choices=Route.choices)
    amount = models.DecimalField(max_digits=24, decimal_places=8)
    note = models.CharField("Комментарий", max_length=500, blank=True)

    compliance_approved_at = models.DateTimeField(null=True, blank=True)
    compliance_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="compliance_approved_ops",
    )
    middleoffice_approved_at = models.DateTimeField(null=True, blank=True)
    middleoffice_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="middleoffice_approved_ops",
    )

    executed_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка (депозит / вывод)"
        verbose_name_plural = "Заявки (депозит / вывод)"

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount} ({self.get_route_display()})"

    def both_approved(self) -> bool:
        return (
            self.compliance_approved_at is not None
            and self.middleoffice_approved_at is not None
        )
