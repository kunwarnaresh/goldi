from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from inventory.models import TenantMembership
from inventory.tenancy import create_tenant_for_user


class Command(BaseCommand):
    help = 'Create an inventory workspace (tenant) owned by an existing user - for accounts created before multi-location inventory.'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('business_name')

    def handle(self, username, business_name, **options):
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f'No user {username}.')
        if TenantMembership.objects.filter(user=user, active=True).exists():
            raise CommandError(f'{username} already belongs to a workspace.')
        tenant = create_tenant_for_user(user, business_name)
        self.stdout.write(self.style.SUCCESS(f'Workspace "{tenant.name}" ({tenant.code}) created; {username} is its owner.'))
