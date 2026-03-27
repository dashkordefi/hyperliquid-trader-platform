from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Creates default role groups: admins and users."

    def handle(self, *args, **options):
        for group_name in ("admins", "users"):
            _, created = Group.objects.get_or_create(name=group_name)
            status = "created" if created else "already exists"
            self.stdout.write(self.style.SUCCESS(f"{group_name}: {status}"))
