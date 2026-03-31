# Generated manually for withdrawal_bridge_submitted_at

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0003_funds_operation_blockchain_tx_hash"),
    ]

    operations = [
        migrations.AddField(
            model_name="fundsoperationrequest",
            name="withdrawal_bridge_submitted_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Вывод USDC→Arbitrum: Hyperliquid принял withdraw3; ждём FinalizedWithdrawal на Bridge2.",
                null=True,
            ),
        ),
    ]
