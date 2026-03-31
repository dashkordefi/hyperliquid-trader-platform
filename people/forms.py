from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import PersonRecord


class RegistrationForm(UserCreationForm):
    """Регистрация с русскими подписями и явным текстом про занятый логин."""

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = _("Имя пользователя (логин)")
        self.fields["username"].help_text = _(
            "Должен быть уникальным. Регистр букв не различается: «Trader» и «trader» — один и тот же логин."
        )
        self.fields["password1"].label = _("Пароль")
        self.fields["password2"].label = _("Подтверждение пароля")

    def clean_username(self):
        username = self.cleaned_data.get("username")
        User = get_user_model()
        if username and User.objects.filter(username__iexact=username).exists():
            raise ValidationError(
                _(
                    "Пользователь с таким логином уже есть. Выберите другой логин или войдите, "
                    "если вы уже регистрировались. Совпадение проверяется без учёта регистра букв."
                )
            )
        return username


class PersonRecordForm(forms.ModelForm):
    class Meta:
        model = PersonRecord
        fields = ["last_name", "first_name", "age"]
