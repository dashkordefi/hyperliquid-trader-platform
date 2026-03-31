"""
Проверка заявок на депозит/вывод и сопоставление с текущим состоянием HL (Info API).

Примеры:
  python manage.py check_funds_operations
  python manage.py check_funds_operations --wallet 0xabc...
  python manage.py check_funds_operations --wallet 0xabc... --limit 20
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from trading.hl_read import fetch_withdraw_limits
from trading.models import FundsOperationRequest, TraderWallet


class Command(BaseCommand):
    help = "Показать заявки на депозит/вывод и (опционально) текущие лимиты HL по адресу."

    def add_arguments(self, parser):
        parser.add_argument(
            "--wallet",
            type=str,
            default="",
            help="Адрес кошелька 0x… — запросить withdrawable USDC и spot ETH из Info API.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=15,
            help="Сколько последних заявок показать (по всем кошелькам).",
        )

    def handle(self, *args, **options):
        limit = max(1, min(int(options["limit"] or 15), 200))
        addr = (options.get("wallet") or "").strip()

        if addr:
            if not TraderWallet.objects.filter(address__iexact=addr).exists():
                self.stdout.write(
                    self.style.WARNING(
                        f"Кошелёк {addr} не найден в БД — покажу только заявки и HL по адресу."
                    )
                )

        qs = (
            FundsOperationRequest.objects.select_related(
                "wallet",
                "compliance_approved_by",
                "middleoffice_approved_by",
            )
            .order_by("-created_at")[:limit]
        )

        self.stdout.write(self.style.MIGRATE_HEADING("Заявки (последние %d)" % limit))
        for op in qs:
            self.stdout.write(
                f"  id={op.pk} {op.get_kind_display()} {op.amount} {op.route} "
                f"| wallet={op.wallet.label} ({op.wallet.address[:10]}…)"
            )
            self.stdout.write(
                f"    compliance={bool(op.compliance_approved_at)} "
                f"middleoffice={bool(op.middleoffice_approved_at)} "
                f"executed={bool(op.executed_at)} rejected={bool(op.rejected_at)}"
            )
            if op.blockchain_tx_hash:
                self.stdout.write(f"    tx={op.blockchain_tx_hash}")
            if (
                op.compliance_approved_at
                and op.middleoffice_approved_at
                and not op.executed_at
                and not op.rejected_at
            ):
                self.stdout.write(
                    self.style.WARNING(
                        "    → В БД нет отметки «исполнено». После двух аппрувов вывод USDC/ETH "
                        "должен уйти автоматически; при сбое — см. логи и админку."
                    )
                )

        if addr:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("Hyperliquid Info API (сейчас)"))
            try:
                lim = fetch_withdraw_limits(addr)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Ошибка: {e}"))
                return
            if lim.get("error"):
                self.stdout.write(self.style.ERROR(f"  {lim['error']}"))
                return
            self.stdout.write(
                f"  Perp withdrawable (USDC bridge): {lim.get('usdc_arbitrum')}"
            )
            self.stdout.write(
                f"  Spot ETH (доступно к spotSend): {lim.get('eth_ethereum')}"
            )
            self.stdout.write("")
            self.stdout.write(
                "  Если после «успешного» вывода USDC это число почти не изменилось — "
                "транзакция может не пройти или ещё в очереди HL. Проверьте Arbitrum: "
                "arbiscan.io по адресу кошелька и по tx из заявки."
            )
