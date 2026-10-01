"""Daily Jewellery Savings job: due / grace / overdue marking, advance application, reminders, maturity candidates.

Idempotent - reminders are de-duplicated and statuses are recomputed from dates, so it can run any number of times.
Schedule once a day (e.g. cron: `python manage.py savings_daily`).
"""
from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date

from inventory.models import Tenant, TenantMembership
from inventory.services import Actor
from savings.services import run_daily


class Command(BaseCommand):
    help = 'Run the daily jewellery savings scheme job for every tenant.'

    def add_arguments(self, parser):
        parser.add_argument('--date', help='Process as of this date (YYYY-MM-DD); default today.')

    def handle(self, *args, **options):
        as_of = parse_date(options['date']) if options.get('date') else None
        for tenant in Tenant.objects.filter(active=True):
            owner = TenantMembership.objects.filter(tenant=tenant, role='owner', active=True).select_related('user').first()
            if owner is None:
                continue
            stats = run_daily(Actor(tenant=tenant, user=owner.user), as_of)
            self.stdout.write(f'{tenant.code}: {stats}')
