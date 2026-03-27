from django.contrib import admin

from .models import PersonRecord


@admin.register(PersonRecord)
class PersonRecordAdmin(admin.ModelAdmin):
    list_display = ("last_name", "first_name", "age", "created_at")
    search_fields = ("last_name", "first_name")

# Register your models here.
