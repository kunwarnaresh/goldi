"""Manufacturing acceptance tests - the spec's end-to-end Gold Ring scenario (Tests 1-30) plus engine guarantees."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from django.test import TestCase

from erp.models import Company, FinanceVoucher, GLAccount, GeneralLedger
from inventory.models import Bin, InventoryBalance, InventoryLedgerEntry, Item, JewelleryUnit, Location, SKU, TenantMembership, UnitOfMeasure
from inventory.services import Actor, post_receipt, register_unit
from inventory.tenancy import create_tenant_for_user

from manufacturing import engine as eng
from manufacturing import services as svc
from manufacturing.calc import component_requirement, evaluate_formula, purity_factor, FormulaError
from manufacturing.models import (
    ConsumptionEntry, CostEntry, ManufacturingAuditLog, ManufacturingRole, OutputUnit, PostingBatch, ProductionOrder, QualityParameter,
    RuntimeEntry, ScrapEntry, ShopCalendar, VarianceEntry, WorkCenter,
)
from manufacturing.reports import trace_unit, trace_lot
from manufacturing.security import ConfirmationRequired, ManufacturingError, get_setup

D = Decimal


def wip(order):
    return CostEntry.objects.filter(order=order).aggregate(v=Sum('amount'))['v'] or D('0')


def on_hand(tenant, item, location, bin=None):
    qs = InventoryBalance.objects.filter(tenant=tenant, item=item, location=location)
    if bin is not None:
        qs = qs.filter(bin=bin)
    return qs.aggregate(q=Sum('on_hand_qty'))['q'] or D('0')


class ManufacturingFixture(TestCase):
    """Goldio tenant with a warehouse and a factory, gold/diamond/stone/solder stock, six work centres and G/L accounts."""

    def setUp(self):
        self.owner = User.objects.create_user('owner@goldio.test', password='pw-12345678')
        self.tenant = create_tenant_for_user(self.owner, 'Goldio Manufacturing')
        self.actor = Actor(self.tenant, self.owner)
        self.reviewer = self._member('reviewer', 'BOM_REVIEWER')
        self.approver = self._member('approver', 'BOM_APPROVER')
        self.manager = self._member('manager', 'MANAGER')
        self.operator = self._member('operator', 'OPERATOR')
        self.inspector = self._member('inspector', 'QUALITY')
        self.auditor = self._member('auditor', 'AUDITOR')

        self.wh = Location.objects.create(tenant=self.tenant, code='WH', name='Central Warehouse', location_type='WAREHOUSE', is_warehouse=True)
        self.fact = Location.objects.create(tenant=self.tenant, code='FACT', name='Karkhana', location_type='MANUFACTURING', allow_sales=False)
        mk = lambda code, kind: Bin.objects.create(tenant=self.tenant, location=self.fact, code=code, bin_type=kind)
        self.rm_bin, self.prod_bin, self.qc_bin = mk('FACT-RM', 'STORAGE'), mk('FACT-PROD', 'STAGING'), mk('FACT-QC', 'QC')
        self.fg_bin, self.scrap_bin = mk('FACT-FG', 'STORAGE'), mk('FACT-SCRAP', 'DAMAGED')

        uom = {u.code: u for u in UnitOfMeasure.objects.filter(tenant=self.tenant)}
        item = lambda no, desc, metal, purity, u, **kw: Item.objects.create(tenant=self.tenant, item_no=no, description=desc, metal=metal,
                                                                           purity=purity, base_uom=uom[u], **kw)
        self.gold = item('GOLD-22K', '22K Gold', 'GOLD', '22K', 'GM', item_type='BULLION')
        self.diamond = item('DIA-VS', 'Diamond VS', 'DIAMOND', '', 'CT', item_type='DIAMOND')
        self.stone = item('STONE-RUBY', 'Ruby', 'NONE', '', 'CT', item_type='STONE')
        self.solder = item('SOLDER-22K', '22K Solder', 'GOLD', '22K', 'GM', item_type='BULLION')
        self.recovery = item('GOLD-RECOVERY', 'Recovered gold', 'GOLD', '22K', 'GM', item_type='BULLION')
        self.ring = item('RING-22K-001', 'Gold Ring 22K', 'GOLD', '22K', 'PCS', serial_tracking=True, category='Rings')
        self.plain = item('BAND-22K', 'Plain band (non-serialized)', 'GOLD', '22K', 'PCS')
        sku = lambda it, loc, cost, gross=0: SKU.objects.create(tenant=self.tenant, code=f'{it.item_no}-{loc.code}', item=it, location=loc,
                                                                unit_cost=D(cost), gross_weight=D(gross), default_bin=self.rm_bin if loc == self.fact else None)
        self.gold_fact, self.gold_wh = sku(self.gold, self.fact, 6000, 1), sku(self.gold, self.wh, 6000, 1)
        self.dia_fact, self.stone_fact = sku(self.diamond, self.fact, 50000), sku(self.stone, self.fact, 2000)
        self.solder_fact = sku(self.solder, self.fact, 5000, 1)
        post_receipt(self.actor, location=self.fact, sku=self.gold_fact, quantity=1000, bin=self.rm_bin)
        post_receipt(self.actor, location=self.wh, sku=self.gold_wh, quantity=500)
        post_receipt(self.actor, location=self.fact, sku=self.dia_fact, quantity=25, bin=self.rm_bin)
        post_receipt(self.actor, location=self.fact, sku=self.stone_fact, quantity=12, bin=self.rm_bin)
        post_receipt(self.actor, location=self.fact, sku=self.solder_fact, quantity=10, bin=self.rm_bin)

        company = Company.objects.create(company_code='GLD', company_name='Goldio Jewellers', status='active')
        acct = lambda code, name, kind: GLAccount.objects.create(company=company, account_code=code, account_name=name, account_type=kind)
        setup = get_setup(self.tenant)
        setup.company = company
        setup.default_production_location = setup.default_finished_goods_location = self.fact
        setup.default_production_bin, setup.default_material_bin = self.prod_bin, self.rm_bin
        setup.default_qc_bin, setup.default_finished_goods_bin, setup.default_scrap_bin = self.qc_bin, self.fg_bin, self.scrap_bin
        setup.material_overhead_percent = D('2')
        setup.scrap_requires_approval = False
        setup.gl_posting_enabled = True
        setup.raw_material_account = acct('1310', 'Raw material', 'asset')
        setup.finished_goods_account = acct('1320', 'Finished goods', 'asset')
        setup.wip_account = acct('1330', 'Production WIP', 'asset')
        setup.labour_applied_account = acct('5110', 'Labour applied', 'expense')
        setup.machine_applied_account = acct('5120', 'Machine applied', 'expense')
        setup.overhead_applied_account = acct('5130', 'Overhead applied', 'expense')
        setup.subcontract_applied_account = acct('2150', 'Job work accrual', 'liability')
        setup.variance_account = acct('5190', 'Production variance', 'expense')
        setup.scrap_inventory_account = acct('1340', 'Scrap gold', 'asset')
        setup.save()
        self.setup = setup

        calendar = ShopCalendar.objects.create(tenant=self.tenant, code='KARKHANA', name='Mon-Sat 10-19')
        wc = lambda code, name, rate: WorkCenter.objects.create(tenant=self.tenant, code=code, name=name, location=self.fact, calendar=calendar,
                                                                direct_labour_rate=D(rate), overhead_rate=D('60'), capacity=2)
        self.centers = [wc('MELT', 'Melting', 300), wc('CAST', 'Casting', 300), wc('FILE', 'Filing', 240), wc('SET', 'Stone setting', 480),
                        wc('POLISH', 'Polishing', 240), wc('QC', 'Quality', 360)]

    def _member(self, username, role):
        user = User.objects.create_user(username, password='pw-12345678')
        TenantMembership.objects.create(tenant=self.tenant, user=user, role='member', all_locations=True)
        ManufacturingRole.objects.create(tenant=self.tenant, user=user, role=role)
        return Actor(self.tenant, user)

    # -- builders ---------------------------------------------------------

    def certified_bom(self, item=None):
        item = item or self.ring
        bom, version = svc.create_bom(self.actor, item=item, bom_name='Gold ring 22K', expected_net_weight=D('8.50'),
                                      expected_gross_weight=D('8.56'), expected_stone_weight=D('0.06'))
        svc.save_bom_line(self.actor, version, component_item=self.gold, quantity=D('8.50'), consumption_basis='WEIGHT', scrap_percent=D('7'),
                          flushing_method='MANUAL', routing_link_code='MELT')
        svc.save_bom_line(self.actor, version, component_item=self.diamond, quantity=D('0.20'), consumption_basis='WEIGHT',
                          component_type='DIAMOND', flushing_method='BACKWARD', routing_link_code='SET')
        svc.save_bom_line(self.actor, version, component_item=self.stone, quantity=D('0.10'), consumption_basis='WEIGHT',
                          component_type='STONE', flushing_method='BACKWARD', routing_link_code='SET')
        svc.save_bom_line(self.actor, version, component_item=self.solder, quantity=D('0.05'), consumption_basis='WEIGHT',
                          component_type='CONSUMABLE', flushing_method='BACKWARD')
        svc.save_bom_line(self.actor, version, component_item=self.recovery, quantity=D('0.10'), consumption_basis='WEIGHT',
                          component_type='BY_PRODUCT')
        svc.transition_version(self.actor, version, 'submit')
        svc.transition_version(self.reviewer, version, 'review')
        svc.transition_version(self.approver, version, 'approve')
        svc.transition_version(self.manager, version, 'certify')
        version.refresh_from_db()
        return bom, version

    def certified_routing(self, item=None):
        routing, version = svc.create_routing(self.actor, description='Ring routing', item=item or self.ring)
        for (no, desc, run), wc, link in zip([('10', 'Melting', '0.5'), ('20', 'Casting', '1'), ('30', 'Filing', '3'), ('40', 'Stone setting', '6'),
                                              ('50', 'Polishing', '2'), ('60', 'QC', '1')], self.centers, ['MELT', '', '', 'SET', '', '']):
            svc.save_routing_line(self.actor, version, operation_no=no, description=desc, work_center=wc, setup_time=D('15'), run_time=D(run),
                                  routing_link_code=link)
        svc.transition_version(self.actor, version, 'submit')
        svc.transition_version(self.reviewer, version, 'review')
        svc.transition_version(self.approver, version, 'approve')
        svc.transition_version(self.manager, version, 'certify')
        version.refresh_from_db()
        return routing, version

    def released_order(self, qty=100, item=None):
        self.certified_bom(item)
        self.certified_routing(item)
        order = svc.create_order(self.manager, item=item or self.ring, quantity=qty)
        svc.approve_order(self.manager, order)
        svc.release_order(self.manager, order)
        order.refresh_from_db()
        return order

    def op(self, order, no):
        return order.operations.get(operation_no=no)

    def comp(self, order, item):
        return order.components.get(item=item)

    def issue_all(self, order):
        svc.reserve_material(self.manager, order)
        pick = svc.create_pick_list(self.operator, order)
        for line in pick.lines.select_related('item'):
            svc.scan_pick(self.operator, pick, line.item.item_no)
        return svc.post_pick_list(self.operator, pick)

    def run_operation(self, order, no, qty, *, units=None):
        operation = self.op(order, no)
        svc.start_operation(self.operator, operation)
        return svc.complete_operation(self.operator, operation, output_qty=qty, setup_minutes=15, run_minutes=30, units=units)


class CalculationTests(TestCase):
    def test_requirement_matches_the_spec_example(self):
        req = component_requirement(per_piece=D('8.50'), production_qty=100, scrap_percent=7)
        self.assertEqual((req['gross'], req['scrap'], req['expected']), (D('850.000'), D('59.500'), D('909.500')))

    def test_formula_is_evaluated_safely(self):
        self.assertEqual(evaluate_formula('net_weight * (1 + wastage_percent / 100) + net_weight * loss_percent / 100',
                                          {'net_weight': 10, 'wastage_percent': 7, 'loss_percent': D('1.5')}), D('10.85'))
        with self.assertRaises(FormulaError):
            evaluate_formula('__import__("os").system("x")', {})
        with self.assertRaises(FormulaError):
            evaluate_formula('unknown * 2', {'net_weight': 1})

    def test_purity_factor(self):
        self.assertEqual(purity_factor('22K'), D('0.9167'))
        self.assertEqual(purity_factor('916'), D('0.916'))
        self.assertEqual(purity_factor('91.6'), D('0.916'))
        self.assertEqual(purity_factor(''), D('1'))


class AcceptanceTests(ManufacturingFixture):
    def test_01_bom_maker_checker_and_certification(self):
        bom, version = svc.create_bom(self.actor, item=self.ring, bom_name='Ring')
        with self.assertRaises(ManufacturingError):  # no lines
            svc.transition_version(self.actor, version, 'submit')
        svc.save_bom_line(self.actor, version, component_item=self.gold, quantity=D('8.5'))
        svc.transition_version(self.actor, version, 'submit')
        with self.assertRaises(ManufacturingError):  # maker cannot review own BOM
            svc.transition_version(self.actor, version, 'review')
        with self.assertRaises(PermissionDenied):  # operator has no review right
            svc.transition_version(self.operator, version, 'review')
        svc.transition_version(self.reviewer, version, 'review')
        with self.assertRaises(ManufacturingError):  # cannot certify before approval
            svc.transition_version(self.manager, version, 'certify')
        svc.transition_version(self.approver, version, 'approve')
        svc.transition_version(self.manager, version, 'certify')
        version.refresh_from_db()
        self.assertEqual(version.status, 'CERTIFIED')
        self.assertEqual(bom.active_version(), version)
        with self.assertRaises(ManufacturingError):  # certified versions are never edited in place
            svc.save_bom_line(self.actor, version, component_item=self.diamond, quantity=1)
        v2 = svc.new_bom_version(self.actor, bom, change_note='More gold')
        self.assertEqual((v2.version_no, v2.status, v2.lines.count()), (2, 'DRAFT', 1))

    def test_02_routing_certification(self):
        routing, version = self.certified_routing()
        self.assertEqual(version.status, 'CERTIFIED')
        self.assertEqual(version.lines.count(), 6)
        self.assertEqual(routing.active_version(), version)

    def test_03_to_07_order_refresh_availability_reserve_release_pick(self):
        self.certified_bom()
        self.certified_routing()
        with self.assertRaises(PermissionDenied):
            svc.create_order(self.operator, item=self.ring, quantity=100)
        order = svc.create_order(self.manager, item=self.ring, quantity=100)
        # Test 3 - refresh generated the snapshot
        gold = self.comp(order, self.gold)
        self.assertEqual((gold.gross_requirement, gold.scrap_requirement, gold.expected_qty), (D('850.000'), D('59.500'), D('909.500')))
        self.assertEqual(self.comp(order, self.diamond).expected_qty, D('20.000'))
        self.assertEqual(order.operations.count(), 6)
        self.assertEqual(order.lines.filter(line_type='BY_PRODUCT').get().item, self.recovery)
        order.refresh_from_db()
        self.assertEqual(order.planned_material_cost, D('909.5') * 6000 + 20 * 50000 + 10 * 2000 + 5 * 5000)
        self.assertGreater(order.planned_labour_cost, 0)
        self.assertIsNotNone(order.planned_end)
        # Test 4 - availability
        rows = {r['component'].item.item_no: r for r in svc.material_availability(order)}
        self.assertEqual(rows['GOLD-22K']['status'], 'AVAILABLE')
        self.assertEqual(rows['GOLD-22K']['available'], D('1000'))
        # Release needs manager approval first (maker-checker on orders)
        with self.assertRaises(ManufacturingError):
            svc.release_order(self.manager, order)
        svc.approve_order(self.manager, order)
        # Test 5 - reservation lowers available, not on-hand
        svc.reserve_material(self.manager, order)
        gold.refresh_from_db()
        self.assertEqual(gold.reserved_qty, D('909.500'))
        self.assertEqual(on_hand(self.tenant, self.gold, self.fact), D('1000'))
        # Test 6 - release
        svc.release_order(self.manager, order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'MATERIAL_RESERVED')
        # Test 7 - pick with scan validation
        pick = svc.create_pick_list(self.operator, order)
        with self.assertRaises(ManufacturingError):
            svc.scan_pick(self.operator, pick, 'NOT-A-CODE')
        gold_line = pick.lines.get(item=self.gold)
        self.assertEqual(gold_line.qty_to_pick, D('909.500'))
        svc.scan_pick(self.operator, pick, 'FACT-RM', quantity=D('909.5'))
        svc.post_pick_list(self.operator, pick)
        gold.refresh_from_db()
        self.assertEqual((gold.picked_qty, gold.reserved_qty, gold.issued_open_qty), (D('909.500'), D('0'), D('909.500')))
        self.assertEqual(on_hand(self.tenant, self.gold, self.fact, self.prod_bin), D('909.500'))
        picked = InventoryBalance.objects.get(tenant=self.tenant, item=self.gold, bin=self.prod_bin)
        self.assertEqual(picked.picked_qty, D('909.500'))
        self.assertEqual(picked.available_qty, D('0'))  # issued material is not available to anyone else
        order.refresh_from_db()
        self.assertEqual(order.status, 'MATERIAL_ISSUED')

    def test_08_to_30_end_to_end_gold_ring(self):
        order = self.released_order()
        self.issue_all(order)
        gold = self.comp(order, self.gold)
        # Test 8 - partial consumption (warehouse controlled: only issued material is consumed)
        batch = eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': D('500')}])
        gold.refresh_from_db()
        self.assertEqual((gold.consumed_qty, gold.remaining_qty), (D('500.000'), D('409.500')))
        self.assertEqual(on_hand(self.tenant, self.gold, self.fact), D('500.000'))
        self.assertEqual(wip(order), D('3000000') + D('60000'))  # 500 g x 6000 + 2% material overhead
        self.assertEqual(batch.gl_status, 'POSTED')
        # Tests 9-12 - start, setup, runtime, downtime
        op10 = self.op(order, '10')
        svc.start_operation(self.operator, op10)
        op10.refresh_from_db()
        self.assertEqual(op10.status, 'STARTED')
        eng.quick_post(self.operator, order, [{'entry_type': 'RUNTIME', 'operation': op10, 'setup_minutes': 20, 'run_minutes': 40,
                                               'downtime_minutes': 10}])
        op10.refresh_from_db()
        self.assertEqual((op10.actual_setup_minutes, op10.actual_run_minutes, op10.actual_downtime_minutes), (D('20'), D('40'), D('10')))
        self.assertEqual(op10.planned_setup_minutes, D('15'))  # planned is never overwritten
        runtime = RuntimeEntry.objects.get(operation=op10)
        self.assertEqual(runtime.total_minutes, D('70'))
        self.assertEqual(runtime.labour_cost, D('300.00'))  # 1 h x 300
        self.assertEqual(runtime.overhead_cost, D('60.00'))
        # Test 13 - partial output; Test 14 - scrap; Test 15 - completion
        eng.quick_post(self.operator, order, [{'entry_type': 'OUTPUT', 'operation': op10, 'quantity': 40}])
        eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': D('409.5')}])
        eng.quick_post(self.operator, order, [{'entry_type': 'SCRAP', 'operation': op10, 'scrap_weight': D('3'), 'recoverable': True,
                                               'scrap_type': 'METAL_LOSS'}])
        svc.complete_operation(self.operator, op10, output_qty=60, run_minutes=20)
        op10.refresh_from_db()
        self.assertEqual((op10.output_qty, op10.status), (D('100.000'), 'COMPLETED'))
        self.assertEqual(ScrapEntry.objects.get(order=order).recovery_entry.quantity, D('3.000'))
        self.assertEqual(on_hand(self.tenant, self.recovery, self.fact, self.scrap_bin), D('3.000'))
        # Operation sequence is enforced
        with self.assertRaises(ManufacturingError):
            eng.quick_post(self.operator, order, [{'entry_type': 'OUTPUT', 'operation': self.op(order, '30'), 'quantity': 1}])
        self.run_operation(order, '20', 100)
        self.run_operation(order, '30', 100)
        self.run_operation(order, '40', 100)  # backflushes diamonds + stones (routing link SET)
        dia = self.comp(order, self.diamond)
        self.assertEqual(dia.consumed_qty, D('20.000'))
        self.assertEqual(on_hand(self.tenant, self.diamond, self.fact), D('5'))
        self.run_operation(order, '50', 100)
        # Test 16/18/19 - final output creates 100 serialized units in QC, with HUIDs
        units = [{'huid': f'H{n:05d}', 'gross_weight': '8.60', 'stone_weight': '0.06'} for n in range(1, 101)]
        self.run_operation(order, '60', 100, units=units)
        order.refresh_from_db()
        self.assertEqual((order.produced_qty, order.qc_pending_qty, order.status), (D('100.000'), D('100.000'), 'QC_PENDING'))
        self.assertEqual(OutputUnit.objects.filter(order=order).count(), 100)
        self.assertEqual(JewelleryUnit.objects.filter(tenant=self.tenant, item=self.ring, status='QC').count(), 100)
        self.assertEqual(self.comp(order, self.solder).consumed_qty, D('5.000'))  # unlinked backflush at final operation
        unit = OutputUnit.objects.filter(order=order).first().jewellery_unit
        self.assertEqual((unit.huid, unit.gross_weight, unit.net_metal_weight), ('H00001', D('8.600'), D('8.540')))
        self.assertEqual(unit.purchase_cost, order.planned_unit_cost.quantize(D('0.01')))
        svc.assign_huid(self.operator, order, unit, huid='HX0001', hallmark_status='HALLMARKED', assay_centre='BIS Assay Delhi')
        with self.assertRaises(ManufacturingError):  # HUID is unique
            svc.assign_huid(self.operator, order, OutputUnit.objects.filter(order=order)[1].jewellery_unit, huid='HX0001')
        # Test 17 - QC: 98 pass, 1 fail (scrapped), 1 rework
        pieces = list(svc.pending_qc_units(order))
        results = [(u, 'PASS') for u in pieces[:98]] + [(pieces[98], 'FAIL'), (pieces[99], 'REWORK')]
        weight = QualityParameter.objects.create(tenant=self.tenant, code='WT', name='Weight', parameter_type='WEIGHT', stage='FINAL',
                                                 min_value=D('8.4'), max_value=D('8.8'))
        with self.assertRaises(PermissionDenied):
            eng.post_qc(self.operator, svc.create_qc(self.inspector, order, parameters=[(weight, '8.6', 'PASS', None)]), unit_results=results)
        qc = svc.create_qc(self.inspector, order, parameters=[(weight, '8.6', 'PASS', None)])
        eng.post_qc(self.inspector, qc, unit_results=results)
        order.refresh_from_db()
        self.assertEqual((order.accepted_qty, order.rejected_qty, order.qc_pending_qty), (D('98'), D('1'), D('1')))
        # Test 20 - put-away: accepted pieces are available in finished goods
        self.assertEqual(JewelleryUnit.objects.filter(tenant=self.tenant, item=self.ring, status='AVAILABLE', current_bin=self.fg_bin).count(), 98)
        self.assertEqual(pieces[98].__class__.objects.get(pk=pieces[98].pk).status, 'SCRAPPED')
        # Rework does not alter history: it adds a rework operation, costed separately
        rework = order.reworks.get()
        rework_op = rework.operations.get()
        eng.quick_post(self.operator, order, [{'entry_type': 'RUNTIME', 'operation': rework_op, 'run_minutes': 30},
                                              {'entry_type': 'OUTPUT', 'operation': rework_op, 'quantity': 1}], journal_type='REWORK', rework=rework)
        svc.complete_rework(self.operator, rework)
        self.assertTrue(CostEntry.objects.filter(order=order, is_rework=True, cost_type='LABOUR').exists())
        qc2 = svc.create_qc(self.inspector, order, parameters=[(weight, '8.5', 'PASS', None)])
        eng.post_qc(self.inspector, qc2, unit_results=[(pieces[99], 'PASS')])
        order.refresh_from_db()
        self.assertEqual((order.accepted_qty, order.qc_pending_qty, order.status), (D('99'), D('0'), 'QC_APPROVED'))
        # Test 21/22 - cost and WIP
        breakdown = eng.cost_breakdown(order)
        self.assertEqual(breakdown['rows'][0]['actual'], D('909.5') * 6000 + 20 * 50000 + 10 * 2000 + 5 * 5000)
        self.assertGreater(breakdown['rework'], 0)
        self.assertEqual(breakdown['wip'], wip(order))
        self.assertNotEqual(wip(order), 0)
        # Test 23 - finish settles WIP to zero
        eng.finish_order(self.manager, order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'FINISHED')
        self.assertEqual(wip(order), 0)
        self.assertTrue(VarianceEntry.objects.filter(order=order, variance_type='TOTAL').exists())
        # finished orders are immutable
        with self.assertRaises(ManufacturingError):
            eng.quick_post(self.operator, order, [{'entry_type': 'RUNTIME', 'operation': self.op(order, '60'), 'run_minutes': 5}])
        # Test 25 - reopen under authorized control restores WIP through reversal entries
        with self.assertRaises(PermissionDenied):
            eng.reopen_order(self.operator, order, reason='wrong')
        with self.assertRaises(ManufacturingError):
            eng.reopen_order(self.manager, order, reason='')
        eng.reopen_order(self.manager, order, reason='Late invoice for stone setting')
        order.refresh_from_db()
        self.assertEqual(order.status, 'QC_APPROVED')
        self.assertNotEqual(wip(order), 0)
        eng.finish_order(self.manager, order)
        order.refresh_from_db()
        self.assertEqual(wip(order), 0)
        # Test 26 - audit trail
        actions = set(ManufacturingAuditLog.objects.filter(order=order).values_list('action', flat=True))
        for action in ('create', 'refresh', 'approve', 'release', 'start_operation', 'post_journal', 'post_qc', 'assign_huid', 'finish', 'reopen'):
            self.assertIn(action, actions)
        # Test 27 - inventory ledger: consumption negative, output positive, all under the order's document number
        ledger = InventoryLedgerEntry.objects.filter(tenant=self.tenant, document_no=order.order_no)
        self.assertEqual(ledger.filter(transaction_type='CONSUMPTION', item=self.gold).aggregate(q=Sum('quantity'))['q'], D('-909.500'))
        self.assertEqual(ledger.filter(transaction_type='OUTPUT', item=self.ring).count(), 100)
        self.assertEqual(on_hand(self.tenant, self.gold, self.fact), D('90.500'))
        # Test 28 - G/L: every production voucher balances; WIP account nets to zero after finish
        vouchers = FinanceVoucher.objects.filter(document_no__in=order.posting_batches.values_list('batch_no', flat=True))
        self.assertTrue(vouchers.exists())
        for voucher in vouchers:
            self.assertEqual(voucher.total_debit, voucher.total_credit)
            self.assertEqual(voucher.status, 'posted')
        wip_gl = GeneralLedger.objects.filter(account=self.setup.wip_account).aggregate(d=Sum('debit_amount'), c=Sum('credit_amount'))
        self.assertEqual(wip_gl['d'], wip_gl['c'])
        # Test 30 - serialized traceability both ways
        trace = trace_unit(self.tenant, unit)
        self.assertEqual(trace['order'], order)
        self.assertIn('GOLD-22K', {row['item'] for row in trace['materials']})
        self.assertTrue(any(row['receipt'] for row in trace['materials']))
        forward = trace_lot(self.tenant, item=self.gold)
        self.assertIn(order, forward['orders'])
        self.assertEqual(len(forward['units']), 100)
        # Test 26 (read-only auditor)
        with self.assertRaises(PermissionDenied):
            eng.quick_post(self.auditor, order, [])

    def test_24_reverse_incorrect_consumption(self):
        order = self.released_order(qty=10)
        self.issue_all(order)
        gold = self.comp(order, self.gold)
        eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': D('50')}])
        entry = ConsumptionEntry.objects.get(order=order)
        with self.assertRaises(PermissionDenied):
            eng.reverse_entry(self.operator, 'consumption', entry, reason='typo')
        with self.assertRaises(ManufacturingError):
            eng.reverse_entry(self.manager, 'consumption', entry, reason='')
        eng.reverse_entry(self.manager, 'consumption', entry, reason='Keyed 50 instead of 5')
        entry.refresh_from_db()
        gold.refresh_from_db()
        self.assertTrue(entry.reversed)
        reversal = ConsumptionEntry.objects.get(reversal_of=entry)
        self.assertEqual((reversal.quantity, reversal.cost_amount), (D('-50.000'), -entry.cost_amount))
        self.assertEqual((gold.consumed_qty, gold.issued_open_qty), (D('0'), gold.expected_qty))
        self.assertEqual(wip(order), 0)
        self.assertEqual(on_hand(self.tenant, self.gold, self.fact, self.prod_bin), gold.expected_qty)  # back in the production bin
        with self.assertRaises(ManufacturingError):
            eng.reverse_entry(self.manager, 'consumption', entry, reason='again')
        with self.assertRaises(ValidationError):  # posted entries are immutable
            entry.delete()

    def test_29_multi_location_material_moves_through_a_transfer(self):
        order = self.released_order(qty=150)  # needs 1364.25 g gold; only 1000 g at the factory
        rows = {r['component'].item.item_no: r for r in svc.material_availability(order)}
        self.assertEqual(rows['GOLD-22K']['shortage'], D('364.250'))
        self.assertEqual(rows['GOLD-22K']['elsewhere'][0]['location__code'], 'WH')
        transfer = svc.create_material_transfer(self.manager, order, from_location=self.wh)
        line = transfer.lines.get()
        self.assertEqual((line.item, line.quantity, transfer.to_location), (self.gold, D('364.250'), self.fact))
        # issuing straight from another location is refused - stock only moves through the transfer
        order.components.filter(item=self.gold).update(location=self.wh)
        with self.assertRaises(ManufacturingError):
            eng.issue_material(self.operator, order, [{'component': self.comp(order, self.gold), 'quantity': 1}])

    def test_release_blocks_shortage_unless_manager_overrides(self):
        self.certified_bom()
        self.certified_routing()
        order = svc.create_order(self.manager, item=self.ring, quantity=200)
        svc.approve_order(self.manager, order)
        with self.assertRaises(ConfirmationRequired):
            svc.release_order(self.manager, order)
        supervisor = self._member('supervisor', 'SUPERVISOR')
        with self.assertRaises(ManufacturingError):
            svc.release_order(supervisor, order, override_shortage=True)
        svc.release_order(self.manager, order, override_shortage=True)
        order.refresh_from_db()
        self.assertEqual(order.shortage_override_by, self.manager.user)

    def test_posting_is_atomic_and_preview_writes_nothing(self):
        order = self.released_order(qty=10)
        self.issue_all(order)
        gold = self.comp(order, self.gold)
        counts = lambda: (InventoryLedgerEntry.objects.count(), CostEntry.objects.count(), PostingBatch.objects.count(), FinanceVoucher.objects.count())
        before = counts()
        preview = eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': D('10')}], preview=True)
        self.assertTrue(preview['ok'])
        self.assertEqual(preview['inventory'][0]['qty'], '-10.000')
        self.assertTrue(preview['gl'])
        self.assertEqual(counts(), before)
        # a journal whose second line fails rolls the first one back too
        with self.assertRaises(ManufacturingError):
            eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': D('10')},
                                                  {'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': D('99999')}])
        self.assertEqual(counts()[:3], before[:3])
        gold.refresh_from_db()
        self.assertEqual(gold.consumed_qty, 0)
        # G/L setup gaps block the whole posting
        self.setup.wip_account = None
        self.setup.save()
        with self.assertRaises(ManufacturingError):
            eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': D('1')}])
        self.assertEqual(counts()[:3], before[:3])

    def test_idempotent_posting(self):
        order = self.released_order(qty=10)
        self.issue_all(order)
        gold = self.comp(order, self.gold)
        first = eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': 5}], idempotency_key='k-1')
        second = eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': gold, 'quantity': 5}], idempotency_key='k-1')
        self.assertEqual(first.pk, second.pk)
        gold.refresh_from_db()
        self.assertEqual(gold.consumed_qty, D('5'))

    def test_over_output_and_over_consumption_are_blocked(self):
        order = self.released_order(qty=10, item=self.plain)
        self.issue_all(order)
        with self.assertRaises(ManufacturingError):
            eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': self.comp(order, self.gold), 'quantity': 200}])
        for no in ('10', '20', '30', '40', '50'):
            self.run_operation(order, no, 10)
        with self.assertRaises(ManufacturingError):
            eng.quick_post(self.operator, order, [{'entry_type': 'OUTPUT', 'operation': self.op(order, '60'), 'quantity': 11}])

    def test_scrap_needs_supervisor_approval_when_configured(self):
        self.setup.scrap_requires_approval = True
        self.setup.save()
        order = self.released_order(qty=10)
        journal_batch = eng.quick_post(self.operator, order, [{'entry_type': 'SCRAP', 'operation': self.op(order, '10'), 'scrap_weight': 1}])
        self.assertIsNone(journal_batch)
        journal = order.journals.get(status='PENDING_APPROVAL')
        self.assertFalse(ScrapEntry.objects.filter(order=order).exists())
        eng.post_journal(self.manager, journal)
        self.assertEqual(ScrapEntry.objects.get(order=order).approved_by, self.manager.user)

    def test_non_serialized_output_reversal_and_actual_costing(self):
        self.setup.costing_method = None
        order = self.released_order(qty=10, item=self.plain)
        order.costing_method = 'ACTUAL'
        order.save()
        self.setup.require_quality_check = False
        self.setup.save()
        self.issue_all(order)
        eng.quick_post(self.operator, order, [{'entry_type': 'CONSUMPTION', 'component': self.comp(order, self.gold),
                                               'quantity': self.comp(order, self.gold).expected_qty}])
        for no in ('10', '20', '30', '40', '50', '60'):
            self.run_operation(order, no, 10)
        order.refresh_from_db()
        self.assertEqual((order.produced_qty, order.accepted_qty, order.status), (D('10'), D('10'), 'COMPLETED'))
        output = order.output_entries.get(is_final=True)
        eng.reverse_entry(self.manager, 'output', output, reason='Counted wrong')
        order.refresh_from_db()
        self.assertEqual(order.produced_qty, 0)
        self.assertEqual(on_hand(self.tenant, self.plain, self.fact), 0)
        eng.quick_post(self.operator, order, [{'entry_type': 'OUTPUT', 'operation': self.op(order, '60'), 'quantity': 10}])
        eng.finish_order(self.manager, order)
        order.refresh_from_db()
        self.assertEqual(wip(order), 0)
        # actual costing: the residual was revalued onto the band stock still on hand
        value = InventoryBalance.objects.filter(tenant=self.tenant, item=self.plain).aggregate(v=Sum('cost_value'))['v']
        inputs = CostEntry.objects.filter(order=order, cost_type__in=('MATERIAL', 'LABOUR', 'MACHINE', 'OVERHEAD')).aggregate(v=Sum('amount'))['v']
        credits = -(CostEntry.objects.filter(order=order, cost_type='BYPRODUCT').aggregate(v=Sum('amount'))['v'] or 0)
        self.assertEqual(value, inputs - credits)

    def test_finish_needs_confirmation_for_remaining_work(self):
        order = self.released_order(qty=10)
        with self.assertRaises(ConfirmationRequired):
            eng.finish_order(self.manager, order)
        self.issue_all(order)
        with self.assertRaises(ManufacturingError):  # issued material must be returned first
            eng.finish_order(self.manager, order, force=True)
        for comp in order.components.all():
            if comp.issued_open_qty:
                eng.return_material(self.operator, order, comp)
        eng.finish_order(self.manager, order, force=True, reason='Order cancelled by customer')
        order.refresh_from_db()
        self.assertEqual(order.status, 'FINISHED')
        self.assertEqual(on_hand(self.tenant, self.gold, self.fact, self.prod_bin), 0)

    def test_tenant_isolation(self):
        order = self.released_order(qty=10)
        other_user = User.objects.create_user('intruder', password='pw-12345678')
        other = Actor(create_tenant_for_user(other_user, 'Other Jewellers'), other_user)
        with self.assertRaises(ManufacturingError):
            eng.quick_post(other, order, [])
        with self.assertRaises(ManufacturingError):
            svc.start_operation(other, self.op(order, '10'))

    def test_planning_suggests_production_and_components(self):
        self.certified_bom()
        self.certified_routing()
        ring_sku = SKU.objects.get(tenant=self.tenant, item=self.ring, location=self.fact)
        ring_sku.reorder_point, ring_sku.maximum_stock = D('50'), D('200')
        ring_sku.save()
        run = svc.run_planning(self.manager, horizon_days=30)
        production = run.suggestions.get(suggestion_type='PRODUCTION', item=self.ring)
        self.assertEqual(production.quantity, D('200'))
        gold = run.suggestions.filter(item=self.gold)
        self.assertTrue(gold.filter(suggestion_type='TRANSFER').exists())   # 500 g sit in the warehouse
        self.assertTrue(gold.filter(suggestion_type='PURCHASE').exists())   # the rest must be bought
        created = svc.carry_out(self.manager, [production], firm=True)
        order = ProductionOrder.objects.get(order_no=created[0])
        self.assertEqual((order.status, order.source_type, order.planned_qty), ('FIRM_PLANNED', 'PLANNING', D('200')))
        load = svc.capacity_load(self.tenant, days=30)
        self.assertTrue(any(row['planned'] > 0 for row in load))


class SubcontractTests(ManufacturingFixture):
    def test_subcontracted_casting_tracks_material_loss_and_service_cost(self):
        from manufacturing.models import Subcontractor
        vendor_loc = Location.objects.create(tenant=self.tenant, code='CASTER', name='Caster premises', location_type='VIRTUAL', allow_sales=False)
        caster = Subcontractor.objects.create(tenant=self.tenant, code='CAST01', name='Shree Casting', location=vendor_loc,
                                              allowed_loss_percent=D('1'), rate_per_unit=D('150'))
        self.certified_bom()
        routing, version = svc.create_routing(self.actor, description='Subcontracted casting', item=self.ring)
        svc.save_routing_line(self.actor, version, operation_no='10', description='Casting (job work)', work_center=self.centers[1],
                              subcontracting=True, subcontractor=caster)
        svc.save_routing_line(self.actor, version, operation_no='20', description='Finishing', work_center=self.centers[4], run_time=D('2'))
        for actor, action in ((self.actor, 'submit'), (self.reviewer, 'review'), (self.approver, 'approve'), (self.manager, 'certify')):
            svc.transition_version(actor, version, action)
        order = svc.create_order(self.manager, item=self.ring, quantity=10, routing=routing)
        svc.approve_order(self.manager, order)
        svc.release_order(self.manager, order)
        self.issue_all(order)
        gold = self.comp(order, self.gold)
        sub = svc.create_subcontract(self.manager, order, self.op(order, '10'))
        svc.send_to_subcontractor(self.operator, sub, [{'component': gold, 'quantity': gold.expected_qty}])
        self.assertEqual(on_hand(self.tenant, self.gold, vendor_loc), gold.expected_qty)
        sub_line = sub.lines.get()
        svc.receive_from_subcontractor(self.operator, sub, returns=[{'line': sub_line, 'quantity': gold.expected_qty - D('0.9')}],
                                       service_cost=D('1500'), invoice_reference='CAST/77', output_qty=10, close=True)
        sub.refresh_from_db()
        gold.refresh_from_db()
        self.assertEqual((sub.status, sub.loss_weight, sub.service_cost), ('RECEIVED', D('0.900'), D('1500.00')))
        self.assertEqual(gold.consumed_qty, D('0.900'))
        self.assertEqual(ConsumptionEntry.objects.get(order=order).source, 'SUBCONTRACT_LOSS')
        self.assertEqual(on_hand(self.tenant, self.gold, vendor_loc), 0)
        self.assertEqual(CostEntry.objects.get(order=order, cost_type='SUBCONTRACT').amount, D('1500.00'))
        self.assertEqual(self.op(order, '10').output_qty, D('10'))
