from decimal import Decimal

from django.core.management.base import BaseCommand

from erp.models import Customer, Product, StockLedger, Supplier


class Command(BaseCommand):
    help = 'Seed demo jewelry ERP data for local development.'

    def handle(self, *args, **options):
        Customer.objects.get_or_create(
            name='Asha Jewels',
            defaults={
                'phone': '9876543210',
                'email': 'asha@example.com',
                'gstin': '27ABCDE1234F1Z5',
                'customer_type': 'retail',
                'address': 'Andheri East, Mumbai',
            },
        )
        Customer.objects.get_or_create(
            name='Rajat Gold House',
            defaults={
                'phone': '9123456780',
                'email': 'rajat@example.com',
                'gstin': '27FGHIJ5678K1Z7',
                'customer_type': 'wholesale',
                'address': 'Banjara Hills, Hyderabad',
            },
        )

        Supplier.objects.get_or_create(
            name='Mumbai Bullion Traders',
            defaults={
                'phone': '9988776655',
                'email': 'traders@example.com',
                'gstin': '27LMNOP1234Q1Z2',
                'address': 'Kalbadevi, Mumbai',
            },
        )

        product, _ = Product.objects.get_or_create(
            sku='GOLD-22K-001',
            defaults={
                'name': '22K Gold Necklace',
                'metal_type': 'gold',
                'purity': '22K',
                'weight_grams': Decimal('12.500'),
                'making_charge': Decimal('2500.00'),
                'purchase_price': Decimal('9000.00'),
                'sale_price': Decimal('12000.00'),
                'stock_quantity': 10,
                'is_active': True,
            },
        )

        Product.objects.get_or_create(
            sku='SILVER-925-001',
            defaults={
                'name': '925 Silver Bracelet',
                'metal_type': 'silver',
                'purity': '925',
                'weight_grams': Decimal('30.000'),
                'making_charge': Decimal('600.00'),
                'purchase_price': Decimal('1500.00'),
                'sale_price': Decimal('2600.00'),
                'stock_quantity': 28,
                'is_active': True,
            },
        )

        StockLedger.objects.get_or_create(
            product=product,
            movement_type='inward',
            quantity=Decimal('10.000'),
            rate=Decimal('9000.00'),
            balance=Decimal('10.000'),
            reference='Initial stock',
        )

        self.stdout.write(self.style.SUCCESS('Demo jewelry ERP seed data created successfully.'))
