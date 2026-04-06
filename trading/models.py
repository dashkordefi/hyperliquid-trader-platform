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
    trading_key_encrypted = models.TextField(
        "Ключ для ордеров (зашифровано)",
        blank=True,
        default="",
        help_text="Fernet; не редактировать вручную.",
    )
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
    hl_testnet = models.BooleanField(
        default=False,
        help_text="Mainnet vs testnet на момент создания заявки (исполнение не зависит от сессии аппрувера).",
    )

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
    withdrawal_bridge_submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Вывод USDC→Arbitrum: Hyperliquid принял withdraw3; ждём FinalizedWithdrawal на Bridge2.",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    blockchain_tx_hash = models.CharField(
        "Хеш транзакции в блокчейне",
        max_length=80,
        blank=True,
        default="",
        help_text="0x… после исполнения (Arbitrum или Ethereum в зависимости от маршрута).",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка (депозит / вывод)"
        verbose_name_plural = "Заявки (депозит / вывод)"

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount} ({self.get_route_display()})"

    def both_approved(self) -> bool:
        if not getattr(settings, "FUNDS_REQUIRE_APPROVALS", True):
            return True
        return (
            self.compliance_approved_at is not None
            and self.middleoffice_approved_at is not None
        )

    @property
    def workflow_status_kind(self) -> str:
        """Класс для стиля: rejected, executed, pending_compliance, pending_mo, pending_blockchain."""
        if self.rejected_at is not None:
            return "rejected"
        if self.executed_at is not None:
            return "executed"
        reqs = getattr(settings, "FUNDS_REQUIRE_APPROVALS", True)
        if reqs:
            if self.compliance_approved_at is None:
                return "pending_compliance"
            if self.middleoffice_approved_at is None:
                return "pending_mo"
        if self.withdrawal_bridge_submitted_at is not None:
            return "pending_arbitrum_finalization"
        return "pending_blockchain"

    @property
    def workflow_status_label(self) -> str:
        if self.rejected_at:
            return "Отклонена"
        if self.executed_at:
            return "Исполнена в блокчейне"
        reqs = getattr(settings, "FUNDS_REQUIRE_APPROVALS", True)
        if reqs:
            if self.compliance_approved_at is None:
                return "Ожидает согласования compliance"
            if self.middleoffice_approved_at is None:
                return "Ожидает согласования middle office"
        if self.withdrawal_bridge_submitted_at is not None:
            return "USDC в пути: ожидаем финализацию на Arbitrum (FinalizedWithdrawal)"
        return "Согласовано, ожидает исполнения в блокчейне"
