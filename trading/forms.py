import re

from django import forms

from .models import FundsOperationRequest, TraderWallet


ETH_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


class TraderWalletForm(forms.ModelForm):
    class Meta:
        model = TraderWallet
        fields = ("label", "address")
        labels = {
            "label": "Название",
            "address": "Адрес кошелька (Ethereum)",
        }

    def clean_address(self):
        addr = self.cleaned_data["address"].strip()
        if not ETH_ADDR.match(addr):
            raise forms.ValidationError("Укажите корректный адрес вида 0x… (42 символа).")
        return addr


class FundsOperationForm(forms.Form):
    route = forms.ChoiceField(
        label="Способ",
        choices=FundsOperationRequest.Route.choices,
    )
    amount = forms.DecimalField(
        label="Сумма",
        max_digits=24,
        decimal_places=8,
        min_value=0,
    )
    note = forms.CharField(
        label="Комментарий",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
