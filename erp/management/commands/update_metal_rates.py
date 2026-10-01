from django.core.management.base import BaseCommand, CommandError

from erp.models import Store
from erp.services import apply_live_metal_rates


class Command(BaseCommand):
    help = (
        'Fetch live gold/silver spot rates (gold-api.com + open.er-api.com), convert to INR/gram per purity, '
        'and apply them to every active store. Intended to be run on a daily schedule '
        '(Windows Task Scheduler / cron) via: python manage.py update_metal_rates'
    )

    def handle(self, *args, **options):
        stores = list(Store.objects.filter(is_active=True))
        if not stores:
            raise CommandError('No active store found to apply rates to.')

        try:
            data, created = apply_live_metal_rates(stores)
        except ValueError as exc:
            raise CommandError(f'Could not fetch live metal rates: {exc}') from exc

        self.stdout.write(self.style.SUCCESS(
            f"Fetched at {data['fetched_at']:%Y-%m-%d %H:%M:%S} (USD/INR {data['usd_inr_rate']:.4f})"
        ))
        for metal_type, purities in (('gold', data['gold']), ('silver', data['silver'])):
            for purity, rate in purities.items():
                self.stdout.write(f'  {metal_type.title():7} {purity:>5}: Rs.{rate}/g')
        self.stdout.write(self.style.SUCCESS(
            f'Applied {len(created)} rate row(s) across {len(stores)} active store(s).'
        ))
