"""
Закрывает заявки USDC→Arbitrum по событию FinalizedWithdrawal (Bridge2).

По умолчанию — Arbiscan API (лёгкие HTTP-запросы). Нужен ARBITRUM_ARBISCAN_API_KEY в env.

Опция --rpc: дополнительно попробовать тяжёлый eth_getLogs по RPC (для отладки, не для веб-воркера).

Пример cron на Render (каждые 5 минут):
  python manage.py finalize_arbitrum_withdrawals
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from trading.arbitrum_withdrawal import (
    try_finalize_usdc_withdrawals_for_wallet,
    try_finalize_usdc_withdrawals_for_wallet_arbiscan,
)
from trading.models import FundsOperationRequest, TraderWallet


class Command(BaseCommand):
    help = (
        "Проверить Bridge2 на Arbitrum и проставить executed_at для завершённых выводов USDC."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--rpc",
            action="store_true",
            help="После Arbiscan попробовать тяжёлый RPC-скан (eth_getLogs).",
        )

    def handle(self, *args, **options):
        use_rpc = bool(options.get("rpc"))
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
                n = try_finalize_usdc_withdrawals_for_wallet_arbiscan(
                    w, max_http_calls=40
                )
                if use_rpc and n == 0:
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
                "Заявки в ожидании есть, событий пока не найдено "
                "(проверьте ARBITRUM_ARBISCAN_API_KEY и лимиты Arbiscan)."
            )
