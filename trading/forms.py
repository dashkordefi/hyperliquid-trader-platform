import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from django import forms

from .models import FundsOperationRequest, TraderWallet


ETH_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _validate_key_matches_address(private_key: str, address: str) -> None:
    from eth_account import Account

    pk = private_key.strip()
    if not pk.startswith("0x"):
        pk = "0x" + pk
    try:
        derived = Account.from_key(pk).address.lower()
    except Exception as exc:
        raise forms.ValidationError("Некорректный приватный ключ.") from exc
    if derived != address.strip().lower():
        raise forms.ValidationError(
            "Приватный ключ не соответствует указанному адресу кошелька."
        )


class TraderWalletForm(forms.ModelForm):
    trading_private_key = forms.CharField(
        label="Приватный ключ для ордеров (необязательно)",
        required=False,
        help_text="Сохраняется зашифровано в БД (ключ шифрования — SECRET_KEY Django). "
        "Можно добавить несколько кошельков с разными ключами — без смены переменных окружения.",
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )

    class Meta:
        model = TraderWallet
        fields = ("label", "address")
        labels = {
            "label": "Название",
            "address": "Адрес кошелька (Ethereum)",
        }

    def __init__(self, *args, user=None, **kwargs):
        self._user = user
        super().__init__(*args, **kwargs)

    def clean_address(self):
        addr = self.cleaned_data["address"].strip()
        if not ETH_ADDR.match(addr):
            raise forms.ValidationError("Укажите корректный адрес вида 0x… (42 символа).")
        addr = addr.lower()
        if self._user is not None:
            if TraderWallet.objects.filter(
                user=self._user, address__iexact=addr
            ).exists():
                raise forms.ValidationError(
                    "Кошелёк с таким адресом уже добавлен. Параметры после создания не меняются — "
                    "при необходимости удалите кошелёк и создайте запись заново."
                )
        return addr

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        priv = (cleaned.get("trading_private_key") or "").strip()
        if priv:
            _validate_key_matches_address(priv, cleaned["address"])
        return cleaned


class CreateTraderWalletForm(forms.Form):
    """Создание нового кошелька: только имя; ключ и адрес генерируются на сервере."""

    label = forms.CharField(
        label="Название",
        max_length=120,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Например, торговый",
                "autocomplete": "off",
            }
        ),
    )

    def clean_label(self):
        label = (self.cleaned_data.get("label") or "").strip()
        if not label:
            raise forms.ValidationError("Введите название кошелька.")
        return label


class SpotTokenTransferForm(forms.Form):
    """Перевод спотового актива на другой кошелёк, зарегистрированный на платформе (spotSend)."""

    destination = forms.ChoiceField(
        label="Кошелёк получателя",
        choices=[],
    )
    amount = forms.DecimalField(
        label="Количество",
        max_digits=40,
        decimal_places=18,
        min_value=Decimal("0"),
        widget=forms.TextInput(
            attrs={
                "placeholder": "0.0",
                "inputmode": "decimal",
                "autocomplete": "off",
            }
        ),
    )
    token_coin = forms.CharField(max_length=128, widget=forms.HiddenInput())

    def __init__(self, *args, wallet_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        if wallet_choices is not None:
            self.fields["destination"].choices = wallet_choices

    def clean_destination(self):
        addr = (self.cleaned_data.get("destination") or "").strip()
        if not addr:
            raise forms.ValidationError("Выберите кошелёк получателя.")
        if not ETH_ADDR.match(addr):
            raise forms.ValidationError("Некорректный адрес в списке.")
        return addr.lower()

    def clean_amount(self):
        amt = self.cleaned_data.get("amount")
        if amt is not None and amt <= 0:
            raise forms.ValidationError("Введите положительное количество.")
        return amt


class PlaceOrderForm(forms.Form):
    """Параметры ордера; ключ — из сохранённого по кошельку или из HYPERLIQUID_TRADING_PRIVATE_KEY."""

    market_type = forms.ChoiceField(
        choices=[("perp", "perp"), ("spot", "spot")],
        widget=forms.HiddenInput,
    )
    symbol = forms.CharField(max_length=128, widget=forms.HiddenInput)
    side = forms.ChoiceField(
        label="Сторона",
        choices=[("buy", "Покупка"), ("sell", "Продажа")],
    )
    order_kind = forms.ChoiceField(
        label="Тип",
        choices=[("market", "Market"), ("limit", "Limit")],
    )
    sz = forms.FloatField(
        label="Размер (size)",
        required=False,
        min_value=1e-12,
    )
    limit_px = forms.FloatField(
        label="Цена (только Limit)",
        required=False,
        min_value=0,
    )
    leverage = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=125,
        widget=forms.HiddenInput,
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        mt = cleaned.get("market_type")
        ok = cleaned.get("order_kind")
        lev = cleaned.get("leverage")

        if mt == "perp":
            if lev is None:
                raise forms.ValidationError("Укажите плечо.")
            if lev < 1:
                raise forms.ValidationError("Плечо должно быть не меньше 1.")

        if ok == "limit":
            px = cleaned.get("limit_px")
            if px is None or float(px) <= 0:
                raise forms.ValidationError(
                    "Для лимитного ордера укажите цену больше нуля."
                )
            sz = cleaned.get("sz")
            if sz is None or float(sz) <= 0:
                raise forms.ValidationError("Укажите положительный размер.")

        if ok == "market" and mt == "spot":
            sz = cleaned.get("sz")
            if sz is None or float(sz) <= 0:
                raise forms.ValidationError("Укажите положительный размер.")

        return cleaned


class UsdcClassTransferForm(forms.Form):
    """Перевод USDC между perp и spot (usdClassTransfer в API Hyperliquid)."""

    direction = forms.ChoiceField(
        label="Направление",
        choices=[
            ("perp_to_spot", "Perp → Spot"),
            ("spot_to_perp", "Spot → Perp"),
        ],
    )
    transfer_full = forms.BooleanField(
        required=False,
        initial=False,
        label="Перевести весь доступный баланс",
    )
    amount = forms.DecimalField(
        label="Сумма (USDC)",
        max_digits=24,
        decimal_places=8,
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "0.0",
                "inputmode": "decimal",
                "autocomplete": "off",
            }
        ),
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        if cleaned.get("transfer_full"):
            return cleaned
        amt = cleaned.get("amount")
        if amt is None:
            raise forms.ValidationError(
                "Укажите сумму или отметьте перевод всего доступного баланса."
            )
        if amt <= 0:
            raise forms.ValidationError("Сумма должна быть больше нуля.")
        return cleaned


class FundsOperationForm(forms.Form):
    route = forms.ChoiceField(
        label="Способ",
        choices=FundsOperationRequest.Route.choices,
    )
    withdraw_all = forms.BooleanField(
        required=False,
        initial=False,
        label="Вывести всё доступное",
    )
    amount = forms.DecimalField(
        label="Сумма",
        max_digits=24,
        decimal_places=8,
        min_value=0,
        required=False,
    )
    note = forms.CharField(
        label="Комментарий",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(
        self,
        *args,
        is_withdraw: bool = False,
        withdraw_limits: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._is_withdraw = is_withdraw
        self._withdraw_limits: dict = (
            withdraw_limits if isinstance(withdraw_limits, dict) else {}
        )
        if not is_withdraw:
            del self.fields["withdraw_all"]
        self.fields["amount"].required = not is_withdraw

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned

        if not self._is_withdraw:
            amt = cleaned.get("amount")
            if amt is None:
                raise forms.ValidationError("Укажите сумму.")
            if amt <= 0:
                raise forms.ValidationError("Сумма должна быть больше нуля.")
            route = cleaned.get("route") or ""
            if route == "usdc_arbitrum" and amt < Decimal("5"):
                raise forms.ValidationError(
                    "Для депозита USDC через bridge Hyperliquid на Arbitrum минимум 5 USDC "
                    "(меньшие суммы контракт не зачисляет)."
                )
            if route == "eth_ethereum" and amt < Decimal("0.007"):
                raise forms.ValidationError(
                    "Для депозита ETH через Unit минимум ~0,007 ETH (как в Hyperliquid)."
                )
            return cleaned

        if cleaned.get("withdraw_all"):
            limits = self._withdraw_limits
            if limits.get("error"):
                raise forms.ValidationError(
                    f"Не удалось определить доступную сумму: {limits['error']}"
                )
            route = cleaned.get("route")
            raw = None
            if route == "usdc_arbitrum":
                raw = limits.get("usdc_arbitrum")
            elif route == "eth_ethereum":
                raw = limits.get("eth_ethereum")
            if raw is None:
                raise forms.ValidationError(
                    "Нет данных о доступной сумме для выбранного способа. Обновите страницу."
                )
            try:
                d = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                raise forms.ValidationError("Некорректные данные лимита вывода.")
            if d <= 0:
                raise forms.ValidationError(
                    "Нет средств для вывода по этому способу."
                )
            cleaned["amount"] = d.quantize(Decimal("0.00000001"))
            return cleaned

        amt = cleaned.get("amount")
        if amt is None:
            raise forms.ValidationError(
                "Укажите сумму или отметьте «Вывести всё доступное»."
            )
        if amt <= 0:
            raise forms.ValidationError("Сумма должна быть больше нуля.")
        return cleaned


class ClosePerpMarketForm(forms.Form):
    coin = forms.CharField(max_length=128)
    close_full = forms.BooleanField(
        required=False,
        initial=False,
        label="Закрыть полностью",
    )
    sz = forms.FloatField(
        label="Размер закрытия",
        required=False,
        min_value=1e-12,
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        if cleaned.get("close_full"):
            return cleaned
        sz = cleaned.get("sz")
        if sz is None or float(sz) <= 0:
            raise forms.ValidationError(
                "Укажите размер закрытия или отметьте «Закрыть полностью»."
            )
        return cleaned


class ClosePerpLimitForm(forms.Form):
    coin = forms.CharField(max_length=128)
    close_full = forms.BooleanField(
        required=False,
        initial=False,
        label="Закрыть полностью",
    )
    sz = forms.FloatField(
        label="Размер закрытия",
        required=False,
        min_value=1e-12,
    )
    limit_px = forms.FloatField(
        label="Цена (лимит)",
        required=False,
        min_value=0,
    )
    next = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean(self):
        cleaned = super().clean()
        if not cleaned:
            return cleaned
        px = cleaned.get("limit_px")
        if px is None or float(px) <= 0:
            raise forms.ValidationError("Укажите цену лимитного ордера.")
        if cleaned.get("close_full"):
            return cleaned
        sz = cleaned.get("sz")
        if sz is None or float(sz) <= 0:
            raise forms.ValidationError(
                "Укажите размер закрытия или отметьте «Закрыть полностью»."
            )
        return cleaned
