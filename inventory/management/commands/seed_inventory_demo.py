from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import ApprovalRule, Bin, Item, Location, SKU, TenantMembership, TransferRoute, UnitOfMeasure, Zone
from inventory.services import (
    Actor, approve_transfer, create_transfer_order, post_receipt, receive_transfer, register_unit, ship_transfer,
)
from inventory.tenancy import create_tenant_for_user


class Command(BaseCommand):
    help = "Populate a user's inventory workspace with demo locations, bins, SKUs, jewellery units, stock and transfers."

    def add_arguments(self, parser):
        parser.add_argument('username')

    @transaction.atomic
    def handle(self, username, **options):
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f'No user {username}.')
        membership = TenantMembership.objects.filter(user=user, active=True).select_related('tenant').first()
        tenant = membership.tenant if membership else create_tenant_for_user(user, 'Goldio Demo Jewellers')
        if Location.objects.filter(tenant=tenant, code='WH').exists():
            raise CommandError('Demo data already exists in this workspace.')
        actor = Actor(tenant, user)
        mk = lambda **kw: Location.objects.create(tenant=tenant, created_by=user, **kw)
        wh = mk(code='WH', name='Central Warehouse', location_type='WAREHOUSE', city='Delhi', is_warehouse=True, allow_sales=False)
        delhi = mk(code='DEL', name='Delhi Store', location_type='STORE', city='Delhi', allow_pos=True, metal_rate_premium_per_gram=Decimal('50'))
        mumbai = mk(code='MUM', name='Mumbai Store', location_type='STORE', city='Mumbai', allow_pos=True, metal_rate_premium_per_gram=Decimal('70'))
        mk(code='ECOM', name='E-commerce Warehouse', location_type='E_COMMERCE', city='Gurugram')
        mk(code='REPAIR', name='Repair Centre', location_type='REPAIR', city='Delhi', allow_sales=False)
        transit = Location.objects.get(tenant=tenant, location_type='TRANSIT')
        for source, target, days in ((wh, delhi, 1), (wh, mumbai, 3), (delhi, mumbai, 3), (delhi, wh, 1), (mumbai, wh, 3)):
            TransferRoute.objects.create(tenant=tenant, from_location=source, to_location=target, transit_location=transit, transfer_days=days)
        receiving = Zone.objects.create(tenant=tenant, location=wh, code='RCV', name='Receiving', zone_type='RECEIVING', sequence=10)
        storage = Zone.objects.create(tenant=tenant, location=wh, code='STO', name='Storage', zone_type='STORAGE', sequence=20)
        Bin.objects.create(tenant=tenant, location=wh, zone=receiving, code='WH-RCV-01', bin_type='RECEIVING')
        wh_gold = Bin.objects.create(tenant=tenant, location=wh, zone=storage, code='WH-GOLD-01', bin_type='STORAGE', pick_sequence=10)
        Bin.objects.create(tenant=tenant, location=wh, code='WH-QC', bin_type='QC')
        for code, kind in (('DEL-01-DISPLAY', 'DISPLAY'), ('DEL-01-SAFE', 'SAFE'), ('DEL-01-DAMAGE', 'DAMAGED')):
            Bin.objects.create(tenant=tenant, location=delhi, code=code, bin_type=kind)
        ApprovalRule.objects.create(tenant=tenant, name='Transfers over ₹5 lakh', min_value=Decimal('500000'), approver_level=2)
        ApprovalRule.objects.create(tenant=tenant, name='Gold over 500 g', metal='GOLD', min_weight=Decimal('500'), approver_level=3)

        pcs = UnitOfMeasure.objects.get(tenant=tenant, code='PCS')
        ring = Item.objects.create(tenant=tenant, item_no='GOLD-RING-001', description='22K Gold Band Ring', metal='GOLD', purity='22K',
                                   base_uom=pcs, category='Rings', created_by=user)
        chain = Item.objects.create(tenant=tenant, item_no='SLV-CHAIN-925', description='925 Silver Chain', metal='SILVER', purity='925',
                                    base_uom=pcs, category='Chains', created_by=user)
        necklace = Item.objects.create(tenant=tenant, item_no='GN001', description='Gold Temple Necklace', metal='GOLD', purity='22K',
                                       base_uom=pcs, category='Necklaces', serial_tracking=True, created_by=user)
        common = dict(tenant=tenant, created_by=user)
        ring_wh = SKU.objects.create(code='GOLD-RING-001-WH', item=ring, location=wh, default_bin=wh_gold, unit_cost=Decimal('62000'),
                                     gross_weight=Decimal('5.200'), stone_weight=Decimal('0.200'), retail_price=Decimal('78000'), barcode='8901000000011', **common)
        SKU.objects.create(code='GOLD-RING-001-DEL', item=ring, location=delhi, reorder_point=5, minimum_stock=5, maximum_stock=20,
                           replenishment_source=wh, retail_price=Decimal('78000'), **common)
        SKU.objects.create(code='GOLD-RING-001-MUM', item=ring, location=mumbai, reorder_point=5, maximum_stock=15,
                           replenishment_source=wh, retail_price=Decimal('78500'), **common)
        chain_wh = SKU.objects.create(code='SLV-CHAIN-925-WH', item=chain, location=wh, unit_cost=Decimal('2400'),
                                      gross_weight=Decimal('25.800'), stone_weight=Decimal('0'), retail_price=Decimal('3600'), **common)
        necklace_wh = SKU.objects.create(code='GN001-WH', item=necklace, location=wh, gross_weight=Decimal('38.450'),
                                         stone_weight=Decimal('2.850'), other_weight=Decimal('0.100'), **common)
        SKU.objects.create(code='GN001-DEL', item=necklace, location=delhi, **common)

        post_receipt(actor, location=wh, sku=ring_wh, quantity=100, bin=wh_gold)
        post_receipt(actor, location=wh, sku=chain_wh, quantity=210)
        units = []
        for number in range(1, 6):
            unit = register_unit(actor, sku=necklace_wh, barcode=f'JWL-{number:04d}', serial_no=f'JR-2026-{450 + number:06d}',
                                 huid=f'HU{number:04d}', metal_cost=Decimal('380000'), making_cost=Decimal('42000'),
                                 stone_cost=Decimal('18000'), stone_value=Decimal('21000'), making_charge=Decimal('45000'),
                                 retail_price=Decimal('560000'), certificate_no=f'IGI-{9000 + number}', certificate_type='IGI')
            post_receipt(actor, location=wh, sku=necklace_wh, unit=unit, bin=wh_gold)
            units.append(unit)

        received = create_transfer_order(actor, from_location=wh, to_location=delhi, lines=[{'sku': ring_wh, 'quantity': 20}, {'unit': units[0]}])
        approve_transfer(actor, received)
        ship_transfer(actor, received)
        receive_transfer(actor, received, lines=[{'line': received.lines.get(jewellery_unit__isnull=True), 'quantity': 18}])
        in_transit = create_transfer_order(actor, from_location=wh, to_location=mumbai, lines=[{'sku': ring_wh, 'quantity': 8}, {'unit': units[1]}])
        approve_transfer(actor, in_transit)
        ship_transfer(actor, in_transit)
        create_transfer_order(actor, from_location=wh, to_location=delhi, lines=[{'sku': chain_wh, 'quantity': 30}])
        self.stdout.write(self.style.SUCCESS(f'Demo inventory created in workspace "{tenant.name}". Open /inventory/.'))
