# Generated manually for hl_testnet snapshot on funds operation

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0004_funds_operation_withdrawal_bridge_submitted_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="fundsoperationrequest",
            name="hl_testnet",
            field=models.BooleanField(
                default=False,
                help_text="Mainnet vs testnet на момент создания заявки (исполнение не зависит от сессии аппрувера).",
            ),
        ),
    ]
