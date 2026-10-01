from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from inventory.engine import InventoryError, InventoryPostingEngine, availability_by_location, compute_available
from inventory.models import (
    ApprovalRule, Bin, InventoryBalance, InventoryLedgerEntry, Item, JewelleryUnit, Location, LocationPermission,
    SKU, TenantMembership, UnitOfMeasure,
)
from inventory.services import (
    Actor, approve_adjustment, approve_transfer, approve_transfer_request, create_count, create_transfer_order,
    create_transfer_request, generate_replenishment, post_adjustment, post_reclassification, post_receipt, post_sale,
    receive_transfer, record_count, register_unit, scan_count, ship_transfer, short_close_transfer, snapshot_count,
    submit_count,
)
from inventory.tenancy import allowed_locations, create_tenant_for_user, require_location


def balance_sum(tenant, location, field='on_hand_qty', item=None):
    qs = InventoryBalance.objects.filter(tenant=tenant, location=location)
    if item is not None:
        qs = qs.filter(item=item)
    return qs.aggregate(total=Sum(field))['total'] or Decimal('0')


class InventoryFixture(TestCase):
    """Tenant with Central Warehouse, Delhi and Mumbai stores, a gold ring SKU and a serialized item."""

    def setUp(self):
        self.owner = User.objects.create_user('owner@goldio.test', password='pw-12345678')
        self.tenant = create_tenant_for_user(self.owner, 'Goldio Jewellers')
        self.actor = Actor(self.tenant, self.owner)
        self.transit = Location.objects.get(tenant=self.tenant, location_type='TRANSIT')
        # Test 1 - locations
        self.wh = Location.objects.create(tenant=self.tenant, code='WH', name='Central Warehouse', location_type='WAREHOUSE',
                                          is_warehouse=True)
        self.delhi = Location.objects.create(tenant=self.tenant, code='DEL', name='Delhi Store', location_type='STORE', allow_pos=True)
        self.mumbai = Location.objects.create(tenant=self.tenant, code='MUM', name='Mumbai Store', location_type='STORE')
        pcs = UnitOfMeasure.objects.get(tenant=self.tenant, code='PCS')
        # Test 2 - Gold Ring SKU
        self.ring = Item.objects.create(tenant=self.tenant, item_no='GOLD-RING-001', description='Gold Ring 22K', metal='GOLD',
                                        purity='22K', base_uom=pcs)
        self.ring_wh = SKU.objects.create(tenant=self.tenant, code='GOLD-RING-001-WH', item=self.ring, location=self.wh,
                                          unit_cost=Decimal('50000'), gross_weight=Decimal('5.000'), stone_weight=Decimal('0.500'))
        self.ring_del = SKU.objects.create(tenant=self.tenant, code='GOLD-RING-001-DEL', item=self.ring, location=self.delhi)
        self.necklace = Item.objects.create(tenant=self.tenant, item_no='GN001', description='Gold Necklace', metal='GOLD',
                                            purity='22K', base_uom=pcs, serial_tracking=True)
        self.necklace_wh = SKU.objects.create(tenant=self.tenant, code='GN001-WH', item=self.necklace, location=self.wh,
                                              gross_weight=Decimal('12.450'), stone_weight=Decimal('0.850'), other_weight=Decimal('0.100'))

    def add_rings(self, qty=100, location=None, sku=None):
        return post_receipt(self.actor, location=location or self.wh, sku=sku or self.ring_wh, quantity=qty)

    def add_unit(self, barcode='JWL-0001', huid=None, sku=None, location=None):
        unit = register_unit(self.actor, sku=sku or self.necklace_wh, barcode=barcode, serial_no=f'SN-{barcode}', huid=huid,
                             metal_cost=Decimal('130000'), making_cost=Decimal('12000'))
        post_receipt(self.actor, location=location or self.wh, sku=sku or self.necklace_wh, unit=unit)
        unit.refresh_from_db()
        return unit

    def transfer(self, lines, from_location=None, to_location=None):
        order = create_transfer_order(self.actor, from_location=from_location or self.wh, to_location=to_location or self.delhi,
                                      lines=lines)
        return approve_transfer(self.actor, order)


class AcceptanceTests(InventoryFixture):
    def test_03_opening_stock_lands_at_the_warehouse(self):
        self.add_rings(100)
        self.assertEqual(balance_sum(self.tenant, self.wh), 100)
        self.assertEqual(balance_sum(self.tenant, self.wh, 'available_qty'), 100)
        self.assertEqual(balance_sum(self.tenant, self.wh, 'gross_weight'), Decimal('500.000'))
        self.assertEqual(balance_sum(self.tenant, self.wh, 'cost_value'), Decimal('5000000.00'))

    def test_04_to_07_transfer_ship_partial_and_full_receipt(self):
        self.add_rings(100)
        order = self.transfer([{'sku': self.ring_wh, 'quantity': 20}])
        self.assertEqual(order.status, 'APPROVED')
        # Approval reserves at the source: available down, on hand unchanged.
        self.assertEqual(balance_sum(self.tenant, self.wh), 100)
        self.assertEqual(balance_sum(self.tenant, self.wh, 'available_qty'), 80)

        ship_transfer(self.actor, order)
        self.assertEqual(balance_sum(self.tenant, self.wh), 80)
        self.assertEqual(balance_sum(self.tenant, self.wh, 'available_qty'), 80)
        self.assertEqual(balance_sum(self.tenant, self.wh, 'reserved_qty'), 0)
        self.assertEqual(balance_sum(self.tenant, self.transit, 'in_transit_qty'), 20)
        order.refresh_from_db()
        self.assertEqual(order.status, 'SHIPPED')

        line = order.lines.get()
        receive_transfer(self.actor, order, lines=[{'line': line, 'quantity': 18}])
        order.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(balance_sum(self.tenant, self.delhi), 18)
        self.assertEqual(balance_sum(self.tenant, self.transit, 'in_transit_qty'), 2)
        self.assertEqual(line.qty_outstanding, 2)
        self.assertEqual(order.status, 'PARTIALLY_RECEIVED')

        receive_transfer(self.actor, order)  # receive remaining
        order.refresh_from_db()
        self.assertEqual(balance_sum(self.tenant, self.delhi), 20)
        self.assertEqual(balance_sum(self.tenant, self.transit, 'in_transit_qty'), 0)
        self.assertEqual(order.status, 'CLOSED')
        # Value and weight travel with the stock; a transfer is never a sale.
        self.assertEqual(balance_sum(self.tenant, self.delhi, 'cost_value'), Decimal('1000000.00'))
        self.assertEqual(balance_sum(self.tenant, self.delhi, 'gross_weight'), Decimal('100.000'))
        self.assertFalse(InventoryLedgerEntry.objects.filter(tenant=self.tenant, transaction_type='SALE').exists())

    def test_08_09_serialized_unit_moves_through_transit_and_cannot_be_sold_from_source(self):
        unit = self.add_unit('JWL-0001')
        self.assertEqual(unit.current_location, self.wh)
        order = self.transfer([{'unit': unit}])
        ship_transfer(self.actor, order, barcodes=['JWL-0001'])
        unit.refresh_from_db()
        self.assertEqual(unit.current_location, self.transit)
        self.assertEqual(unit.status, 'IN_TRANSIT')

        with self.assertRaises(InventoryError):
            post_sale(self.actor, location=self.wh, barcode='JWL-0001', document_no='POS-1')

        receive_transfer(self.actor, order, barcodes=['JWL-0001'])
        unit.refresh_from_db()
        self.assertEqual(unit.current_location, self.delhi)
        self.assertEqual(unit.status, 'AVAILABLE')
        self.assertEqual(unit.pk, JewelleryUnit.objects.get(tenant=self.tenant, barcode='JWL-0001').pk)
        # Specific cost followed the piece.
        self.assertEqual(balance_sum(self.tenant, self.delhi, 'cost_value'), Decimal('142000.00'))
        post_sale(self.actor, location=self.delhi, barcode='JWL-0001', document_no='POS-2')
        unit.refresh_from_db()
        self.assertEqual(unit.status, 'SOLD')

    def test_10_delhi_only_user_cannot_touch_mumbai(self):
        clerk = User.objects.create_user('clerk@goldio.test', password='pw-12345678')
        TenantMembership.objects.create(tenant=self.tenant, user=clerk, role='member')
        LocationPermission.objects.create(tenant=self.tenant, user=clerk, location=self.delhi, can_view=True, can_sell=True)
        self.assertEqual(list(allowed_locations(self.tenant, clerk)), [self.delhi])
        with self.assertRaises(PermissionDenied):
            require_location(self.tenant, clerk, self.mumbai, 'view')
        with self.assertRaises(PermissionDenied):
            create_transfer_order(Actor(self.tenant, clerk), from_location=self.mumbai, to_location=self.delhi,
                                  lines=[{'sku': self.ring_wh, 'quantity': 1}])
        self.client.force_login(clerk)
        response = self.client.get(reverse('api_inventory_availability'), {'item': self.ring.pk, 'location': self.mumbai.pk})
        self.assertEqual(response.status_code, 403)
        response = self.client.get(reverse('api_inventory_balances'))
        self.assertTrue(all(row['location_code'] == 'DEL' for row in response.json()['results']))

    def test_11_other_tenant_sees_no_data(self):
        self.add_rings(100)
        outsider = User.objects.create_user('b@other.test', password='pw-12345678')
        create_tenant_for_user(outsider, 'Other Jewellers')
        self.client.force_login(outsider)
        response = self.client.get(reverse('api_inventory_balances'), {'item': self.ring.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [])
        self.assertEqual(self.client.get(reverse('api_location_detail', args=[self.wh.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('api_item_availability', args=[self.ring.pk])).status_code, 404)

    def test_12_duplicate_huid_rejected(self):
        self.add_unit('JWL-0001', huid='AB12CD')
        with self.assertRaises(InventoryError):
            register_unit(self.actor, sku=self.necklace_wh, barcode='JWL-0002', serial_no='SN-2', huid='ab12cd')
        with self.assertRaises(InventoryError):
            register_unit(self.actor, sku=self.necklace_wh, barcode='JWL-0001', serial_no='SN-3')

    def test_13_physical_count_variance_needs_approved_adjustment(self):
        self.add_rings(100)
        count = create_count(self.actor, location=self.wh)
        snapshot_count(self.actor, count)
        line = count.lines.get()
        self.assertEqual(line.system_qty, 100)
        record_count(self.actor, count, line=line, counted_qty=98)
        adjustment = submit_count(self.actor, count)
        self.assertEqual(adjustment.status, 'SUBMITTED')
        self.assertEqual(adjustment.lines.get().quantity, 2)
        with self.assertRaises(InventoryError):
            post_adjustment(self.actor, adjustment)  # not approved yet
        self.assertEqual(balance_sum(self.tenant, self.wh), 100)
        approve_adjustment(self.actor, adjustment)
        post_adjustment(self.actor, adjustment)
        self.assertEqual(balance_sum(self.tenant, self.wh), 98)
        count.refresh_from_db()
        self.assertEqual(count.status, 'POSTED')
        self.assertTrue(InventoryLedgerEntry.objects.filter(tenant=self.tenant, transaction_type='COUNT', quantity=-2).exists())


class EngineInvariantTests(InventoryFixture):
    def test_ledger_is_immutable(self):
        entry = self.add_rings(5)
        entry.quantity = 999
        with self.assertRaises(ValidationError):
            entry.save()
        with self.assertRaises(ValidationError):
            entry.delete()
        with self.assertRaises(ValidationError):
            InventoryLedgerEntry.objects.filter(pk=entry.pk).update(quantity=1)
        with self.assertRaises(ValidationError):
            InventoryLedgerEntry.objects.filter(pk=entry.pk).delete()

    def test_balance_always_equals_ledger(self):
        self.add_rings(100)
        order = self.transfer([{'sku': self.ring_wh, 'quantity': 30}])
        ship_transfer(self.actor, order, quantities={order.lines.get().pk: 10})
        post_sale(self.actor, location=self.wh, sku=self.ring_wh, quantity=5, document_no='S-1')
        for location in (self.wh, self.transit):
            ledger = InventoryLedgerEntry.objects.filter(tenant=self.tenant, location=location).aggregate(q=Sum('quantity'))['q']
            field = 'in_transit_qty' if location.is_transit else 'on_hand_qty'
            self.assertEqual(ledger, balance_sum(self.tenant, location, field))
        self.assertEqual(balance_sum(self.tenant, self.wh), 85)
        self.assertEqual(balance_sum(self.tenant, self.wh, 'reserved_qty'), 20)
        self.assertEqual(balance_sum(self.tenant, self.wh, 'available_qty'), 65)

    def test_negative_inventory_blocked_by_default(self):
        self.add_rings(3)
        with self.assertRaises(InventoryError):
            post_sale(self.actor, location=self.wh, sku=self.ring_wh, quantity=4, document_no='S-1')
        self.assertEqual(balance_sum(self.tenant, self.wh), 3)

    def test_reserved_unit_cannot_be_sold_twice(self):
        unit = self.add_unit('JWL-0009')
        post_sale(self.actor, location=self.wh, barcode='JWL-0009', document_no='POS-A')
        with self.assertRaises(InventoryError):
            post_sale(self.actor, location=self.wh, barcode='JWL-0009', document_no='POS-B')
        self.assertEqual(InventoryLedgerEntry.objects.filter(tenant=self.tenant, jewellery_unit=unit, transaction_type='SALE').count(), 1)

    def test_stale_unit_version_is_rejected(self):
        unit = self.add_unit('JWL-0010')
        stale = JewelleryUnit.objects.get(pk=unit.pk)
        post_sale(self.actor, location=self.wh, barcode='JWL-0010', document_no='POS-A')
        engine = InventoryPostingEngine(self.tenant, self.owner, document_type='SALE', document_no='POS-B')
        with self.assertRaises(InventoryError):
            engine.issue(transaction_type='SALE', item=self.necklace, location=self.wh, unit=stale)

    def test_unshipped_serial_cannot_be_received(self):
        first, second = self.add_unit('JWL-0001'), self.add_unit('JWL-0002')
        order = self.transfer([{'unit': first}])
        ship_transfer(self.actor, order)
        with self.assertRaises(InventoryError):
            receive_transfer(self.actor, order, barcodes=['JWL-0002'])
        second.refresh_from_db()
        self.assertEqual(second.current_location, self.wh)

    def test_damaged_on_arrival_is_not_available(self):
        self.add_rings(10)
        Bin.objects.create(tenant=self.tenant, location=self.delhi, code='DEL-DAMAGE', bin_type='DAMAGED')
        order = self.transfer([{'sku': self.ring_wh, 'quantity': 8}])
        ship_transfer(self.actor, order)
        receive_transfer(self.actor, order, lines=[{'line': order.lines.get(), 'quantity': 8, 'damaged_qty': 1, 'reason_code': 'DAMAGED'}])
        self.assertEqual(balance_sum(self.tenant, self.delhi), 8)
        self.assertEqual(balance_sum(self.tenant, self.delhi, 'available_qty'), 7)
        self.assertEqual(balance_sum(self.tenant, self.delhi, 'damaged_qty'), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, 'RECEIVED')  # stays open until the exception is resolved

    def test_short_close_writes_off_transit(self):
        self.add_rings(10)
        order = self.transfer([{'sku': self.ring_wh, 'quantity': 10}])
        ship_transfer(self.actor, order)
        receive_transfer(self.actor, order, lines=[{'line': order.lines.get(), 'quantity': 9, 'reason_code': 'MISSING'}])
        short_close_transfer(self.actor, order, 'MISSING')
        order.refresh_from_db()
        self.assertEqual(order.status, 'CLOSED')
        self.assertEqual(balance_sum(self.tenant, self.transit, 'in_transit_qty'), 0)
        self.assertTrue(InventoryLedgerEntry.objects.filter(tenant=self.tenant, transaction_type='TRANSFER_LOSS', quantity=-1).exists())

    def test_approval_threshold_by_weight(self):
        self.add_rings(200)
        ApprovalRule.objects.create(tenant=self.tenant, name='Gold > 500 gm', min_weight=Decimal('500'), metal='GOLD', approver_level=3)
        manager = User.objects.create_user('mgr@goldio.test', password='pw-12345678')
        TenantMembership.objects.create(tenant=self.tenant, user=manager)
        LocationPermission.objects.create(tenant=self.tenant, user=manager, location=self.wh, can_create_transfer=True,
                                          can_approve_transfer=True, approval_level=2)
        order = create_transfer_order(self.actor, from_location=self.wh, to_location=self.delhi, lines=[{'sku': self.ring_wh, 'quantity': 120}])
        with self.assertRaises(PermissionDenied):
            approve_transfer(Actor(self.tenant, manager), order)  # 600 gm needs level 3
        approve_transfer(self.actor, order)

    def test_request_approved_partially_becomes_transfer(self):
        self.add_rings(50)
        request = create_transfer_request(self.actor, to_location=self.delhi, lines=[{'item': self.ring, 'quantity': 10}])
        line = request.lines.get()
        approve_transfer_request(self.actor, request, approved_quantities={line.pk: 7}, from_location=self.wh)
        request.refresh_from_db()
        self.assertEqual(request.status, 'CONVERTED')
        self.assertEqual(request.transfer_order.lines.get().quantity, 7)
        self.assertEqual(request.transfer_order.source_type, 'REQUEST')

    def test_bin_reclassification_and_direct_transfer_permission(self):
        safe = Bin.objects.create(tenant=self.tenant, location=self.wh, code='WH-SAFE', bin_type='SAFE')
        unit = self.add_unit('JWL-0020')
        post_reclassification(self.actor, lines=[{'unit': unit, 'to_bin': safe}], reason='Move to safe')
        unit.refresh_from_db()
        self.assertEqual(unit.current_bin, safe)
        self.assertEqual(unit.current_location, self.wh)
        with self.assertRaises(InventoryError):  # direct transfer not enabled at WH
            post_reclassification(self.actor, lines=[{'unit': unit, 'to_location': self.delhi}])

    def test_projected_availability_counts_open_transfers(self):
        self.add_rings(100)
        self.transfer([{'sku': self.ring_wh, 'quantity': 20}])
        rows = {row['location'].code: row for row in availability_by_location(self.tenant, item=self.ring)}
        self.assertEqual(rows['WH']['on_hand'], 100)
        self.assertEqual(rows['WH']['available'], 80)
        self.assertEqual(rows['WH']['transfer_out'], 20)
        self.assertEqual(rows['WH']['projected'], 80)
        self.assertEqual(rows['DEL']['transfer_in'], 20)
        self.assertEqual(rows['DEL']['projected'], 20)

    def test_replenishment_suggests_transfer_up_to_maximum(self):
        self.add_rings(100)
        self.add_rings(3, location=self.delhi, sku=self.ring_del)
        SKU.objects.filter(pk=self.ring_del.pk).update(reorder_point=5, minimum_stock=5, maximum_stock=20, replenishment_source=self.wh)
        [line] = generate_replenishment(self.actor)
        self.assertEqual(line.suggested_qty, 17)
        self.assertEqual(line.source_location, self.wh)

    def test_scan_count_flags_missing_serialized_piece(self):
        self.add_unit('JWL-0001')
        self.add_unit('JWL-0002')
        count = create_count(self.actor, location=self.wh, blind=True)
        snapshot_count(self.actor, count)
        scan_count(self.actor, count, 'JWL-0001')
        adjustment = submit_count(self.actor, count)
        self.assertEqual(adjustment.lines.get().jewellery_unit.barcode, 'JWL-0002')
        self.assertEqual(compute_available(InventoryBalance.objects.get(tenant=self.tenant, jewellery_unit__barcode='JWL-0001')), 1)


class PageTests(InventoryFixture):
    def test_inventory_pages_render(self):
        self.add_rings(10)
        unit = self.add_unit('JWL-0001')
        order = self.transfer([{'sku': self.ring_wh, 'quantity': 2}])
        self.client.force_login(self.owner)
        urls = [
            reverse('inventory_dashboard'), reverse('inventory_locations'), reverse('inventory_location_card', args=[self.wh.pk]),
            reverse('inventory_skus'), reverse('inventory_sku_card', args=[self.ring_wh.pk]),
            reverse('inventory_availability') + f'?q={self.ring.item_no}', reverse('inventory_transfers'),
            reverse('inventory_transfer_detail', args=[order.pk]), reverse('inventory_transit'), reverse('inventory_ledger'),
            reverse('inventory_trace') + '?q=JWL-0001', reverse('inventory_units'), reverse('inventory_valuation'),
            reverse('inventory_stock_card', args=[self.ring_wh.pk]), reverse('inventory_requests'),
            reverse('inventory_adjustments'), reverse('inventory_counts'), reverse('inventory_replenishment'),
            reverse('inventory_bins'), reverse('inventory_transfer_register'), reverse('inventory_imports'),
            reverse('inventory_reservations'), reverse('inventory_transfer_new'),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        self.assertContains(self.client.get(reverse('inventory_trace') + '?q=JWL-0001'), unit.unit_no)

    def test_registration_creates_isolated_workspace(self):
        response = self.client.post(reverse('register'), {
            'first_name': 'Asha', 'email': 'asha@shop.test', 'business_name': 'Asha Gold', 'password': 'strong-pass-1',
            'confirm_password': 'strong-pass-1',
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='asha@shop.test')
        membership = TenantMembership.objects.get(user=user)
        self.assertEqual(membership.role, 'owner')
        self.assertNotEqual(membership.tenant, self.tenant)
        self.assertTrue(Location.objects.filter(tenant=membership.tenant, location_type='TRANSIT').exists())
