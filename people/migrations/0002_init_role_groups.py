# Группы ролей: при каждом migrate на проде появляются все четыре, а не только traders
# (traders часто создаётся раньше через регистрацию people/views.py).

from django.db import migrations

ROLE_GROUPS = (
    "admins",
    "traders",
    "compliance_approver",
    "middleoffice_approver",
)


def create_role_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ROLE_GROUPS:
        Group.objects.get_or_create(name=name)


def noop_reverse(apps, schema_editor):
    # Не удаляем группы при откате — у пользователей могут остаться связи.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("people", "0001_initial"),
        ("auth", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_role_groups, noop_reverse),
    ]
