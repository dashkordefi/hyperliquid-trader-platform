from django import forms

from .models import PersonRecord


class PersonRecordForm(forms.ModelForm):
    class Meta:
        model = PersonRecord
        fields = ["last_name", "first_name", "age"]
