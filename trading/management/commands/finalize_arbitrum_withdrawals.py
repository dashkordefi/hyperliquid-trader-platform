"""
Закрывает заявки USDC→Arbitrum по событию FinalizedWithdrawal на Bridge2 (eth_getLogs).

Не вызывать из HTTP-запроса: скан может долго идти по RPC. Назначение: cron, например на Render:
  python manage.py finalize_arbitrum_withdrawals

Пример cron (каждые 5 минут): см. документацию Render Scheduled Jobs.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from trading.arbitrum_withdrawal import try_finalize_usdc_withdrawals_for_wallet
from trading.models import FundsOperationRequest, TraderWallet


class Command(BaseCommand):
    help = (
        "Проверить Bridge2 на Arbitrum и проставить executed_at для завершённых выводов USDC."
    )

    def handle(self, *args, **options):
        wallet_ids = list(
            FundsOperationRequest.objects.filter(
                kind=FundsOperationRequest.Kind.WITHDRAW,
                route=FundsOperationRequest.Route.USDC_ARBITRUM,
                withdrawal_bridge_submitted_at__isnull=False,
                executed_at__isnull=True,
                rejected_at__isnull=True,
            )
            .values_list("wallet_id", flat=True)
            .distinct()
        )
        if not wallet_ids:
            self.stdout.write("Нет заявок в ожидании FinalizedWithdrawal.")
            return

        total = 0
        for wid in wallet_ids:
            w = TraderWallet.objects.filter(pk=wid).first()
            if not w:
                continue
            try:
                n = try_finalize_usdc_withdrawals_for_wallet(w)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"wallet_id={wid}: {e}"))
                continue
            total += n
            if n:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"wallet {w.label} ({w.address[:10]}…): закрыто заявок: {n}"
                    )
                )
        if total == 0:
            self.stdout.write(
                "Заявки в ожидании есть, по цепочке пока не найдено событий (или RPC недоступен)."
            )
