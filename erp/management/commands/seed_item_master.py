from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from erp.models import ItemCategory, Product


class Command(BaseCommand):
    help = 'Create an idempotent 100-SKU sample jewellery item master.'

    @transaction.atomic
    def handle(self, *args, **options):
        catalog = [
            ('Gold', 'gold', '22K', '7113', Decimal('6200'), [
                ('Rings', ['Band', 'Solitaire', 'Antique', 'Kids', 'Cocktail']),
                ('Necklaces', ['Daily Wear', 'Temple', 'Choker', 'Bridal', 'Pendant Set']),
                ('Earrings', ['Stud', 'Hoop', 'Jhumka', 'Drop', 'Kids']),
                ('Bangles', ['Plain', 'Kada', 'Antique', 'Engraved', 'Pair']),
                ('Chains', ['Rope', 'Singapore', 'Box', 'Curb', 'Beads']),
            ]),
            ('Silver', 'silver', '925', '7113', Decimal('90'), [
                ('Anklets', ['Classic', 'Ghungroo', 'Kids', 'Pair', 'Designer']),
                ('Bracelets', ['Chain', 'Cuff', 'Charm', 'Mens', 'Kids']),
                ('Pooja Articles', ['Diya', 'Plate', 'Bowl', 'Bell', 'Kalash']),
                ('Payal', ['Lightweight', 'Traditional', 'Designer', 'Kids', 'Pair']),
                ('Utensils', ['Glass', 'Spoon Set', 'Bowl Set', 'Thali', 'Tray']),
            ]),
            ('Diamond', 'diamond', '18K', '7113', Decimal('7200'), [
                ('Rings', ['Solitaire', 'Halo', 'Cluster', 'Wedding', 'Mens']),
                ('Earrings', ['Stud', 'Drop', 'Hoop', 'Huggie', 'Jacket']),
                ('Pendants', ['Solitaire', 'Heart', 'Floral', 'Initial', 'Religious']),
                ('Bracelets', ['Tennis', 'Bangle', 'Chain', 'Cuff', 'Charm']),
                ('Necklaces', ['Line', 'Floral', 'Bridal', 'Station', 'Layered']),
            ]),
            ('Platinum', 'platinum', 'PT950', '7113', Decimal('3100'), [
                ('Rings', ['Classic', 'Wedding Band', 'Solitaire', 'Couple', 'Mens']),
                ('Bands', ['Plain', 'Grooved', 'Diamond', 'Couple', 'Comfort Fit']),
                ('Pendants', ['Classic', 'Heart', 'Cross', 'Initial', 'Diamond']),
                ('Chains', ['Cable', 'Rope', 'Box', 'Curb', 'Singapore']),
                ('Earrings', ['Stud', 'Hoop', 'Drop', 'Diamond', 'Huggie']),
            ]),
            ('Gemstone', 'gold', '22K', '7113', Decimal('6000'), [
                ('Rings', ['Ruby', 'Emerald', 'Sapphire', 'Pearl', 'Navratna']),
                ('Pendants', ['Ruby', 'Emerald', 'Sapphire', 'Pearl', 'Navratna']),
                ('Earrings', ['Ruby', 'Emerald', 'Sapphire', 'Pearl', 'Navratna']),
                ('Bracelets', ['Ruby', 'Emerald', 'Sapphire', 'Pearl', 'Navratna']),
                ('Necklaces', ['Ruby', 'Emerald', 'Sapphire', 'Pearl', 'Navratna']),
            ]),
        ]
        created = 0
        index = 0
        for category_name, metal, purity, hsn, rate, subcategories in catalog:
            category, _ = ItemCategory.objects.get_or_create(name=category_name)
            for subcategory, variants in subcategories:
                for variant_number, variant in enumerate(variants, 1):
                    index += 1
                    sku = f'IM-{index:04d}'
                    weight = Decimal(('{:.3f}'.format(2 + ((index * 7) % 180) / 10)))
                    making = Decimal(500 + (index % 9) * 250)
                    sale = (rate * weight + making).quantize(Decimal('1'))
                    _, was_created = Product.objects.get_or_create(
                        sku=sku,
                        defaults={
                            'item_category': category,
                            'subcategory': subcategory,
                            'variant': f'{variant} / {variant_number:02d}',
                            'name': f'{purity} {subcategory} - {variant}',
                            'metal_type': metal,
                            'purity': purity,
                            'weight_grams': weight,
                            'making_charge': making,
                            'purchase_price': (sale * Decimal('0.82')).quantize(Decimal('0.01')),
                            'sale_price': sale,
                            'mrp': (sale * Decimal('1.03')).quantize(Decimal('1')),
                            'barcode': f'8907000{index:06d}',
                            'hsn_code': hsn,
                            'stock_quantity': Decimal(index % 18 + 1),
                            'is_active': True,
                        },
                    )
                    created += int(was_created)
        self.stdout.write(self.style.SUCCESS(
            f'Item master ready: {Product.objects.count()} total products; {created} added in this run.'
        ))
