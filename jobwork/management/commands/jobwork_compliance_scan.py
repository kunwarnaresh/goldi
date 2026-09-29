"""Daily job work compliance scan: overdue goods (Section 143 due dates), operational SLA breaches and in-transit movements
without a generated e-way bill become (deduplicated) exceptions. Run from cron / a scheduler once a day."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from inventory.models import Tenant, TenantMembership
from inventory.services import Actor
from jobwork.finance import scan_compliance


class Command(BaseCommand):
    help = 'Raise job work compliance alerts (overdue goods at job workers, missing e-way bills) for every tenant.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', help='Tenant code (default: all active tenants).')

    def handle(self, *args, tenant=None, **options):
        tenants = Tenant.objects.filter(active=True)
        if tenant:
            tenants = tenants.filter(code=tenant)
        for t in tenants:
            owner = TenantMembership.objects.filter(tenant=t, active=True, role='owner').select_related('user').first()
            user = owner.user if owner else get_user_model().objects.filter(is_superuser=True).first()
            if user is None:
                self.stderr.write(f'{t.code}: no owner to act as - skipped.')
                continue
            actor = Actor(t, user, ip=None, device='jobwork_compliance_scan')
            raised = scan_compliance(actor)
            self.stdout.write(f'{t.code}: {len(raised)} open compliance alert(s).')
