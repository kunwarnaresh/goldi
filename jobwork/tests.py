"""Job work & subcontracting acceptance tests (spec sections 110-119) plus engine guarantees.

Tax rates used here are test fixtures created and approved through the tax master like any tenant's data - the module
itself contains no rate."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone

from erp.models import Company
from inventory.engine import InventoryError
from inventory.models import InventoryBalance, Item, JewelleryUnit, Location, SKU, TenantMembership, UnitOfMeasure
from inventory.services import Actor, post_receipt
from inventory.tenancy import create_tenant_for_user

from jobwork import compliance, finance, pricing, reports
from jobwork import services as svc
from jobwork.models import (
    ApprovalRule, ComplianceRule, DeliveryChallan, JobWorkAuditLog, JobWorkerPrice, JobWorkerStockEntry, JobWorkException, JobWorkOrder,
    JobWorkRole, TaxRate,
)
from jobwork.security import JobWorkError, get_setup

D = Decimal
TODAY = timezone.localdate()


def on_hand(tenant, item, location):
    return InventoryBalance.objects.filter(tenant=tenant, item=item, location=location).aggregate(q=Sum('on_hand_qty'))['q'] or D('0')


class JobWorkFixture(TestCase):
    """Goldio (Maharashtra, 27) with a warehouse, 22K gold stock, an intra-state and an inter-state job worker, approved rules."""

    def setUp(self):
        self.owner = User.objects.create_user('owner@goldio.test', password='pw-12345678')
        self.tenant = create_tenant_for_user(self.owner, 'Goldio Jewellers')
        self.actor = Actor(self.tenant, self.owner)
        self.manager = self._member('manager', 'MANAGER')
        self.supervisor = self._member('supervisor', 'SUPERVISOR')
        self.operator = self._member('operator', 'OPERATOR')
        self.inspector = self._member('inspector', 'QUALITY')
        self.accounts = self._member('accounts', 'FINANCE')
        self.tax1 = self._member('tax1', 'TAX_ADMIN')
        self.tax2 = self._member('tax2', 'TAX_ADMIN')
        self.company = Company.objects.create(company_code='GLD', company_name='Goldio Jewellers', gstin='27AAAAA0000A1Z5', status='active')
        setup = get_setup(self.tenant)
        setup.company = self.company
        setup.save()

        self.wh = Location.objects.create(tenant=self.tenant, code='WH', name='Central Warehouse', location_type='WAREHOUSE', is_warehouse=True,
                                          gstin='27AAAAA0000A1Z5', state='Maharashtra', company=self.company)
        uom = {u.code: u for u in UnitOfMeasure.objects.filter(tenant=self.tenant)}
        item = lambda no, desc, metal, purity, u, **kw: Item.objects.create(tenant=self.tenant, item_no=no, description=desc, metal=metal,
                                                                           purity=purity, base_uom=uom[u], hsn_code=kw.pop('hsn', '7108'), **kw)
        self.gold = item('GOLD-22K', '22K Gold', 'GOLD', '22K', 'GM', item_type='BULLION')
        self.scrap = item('GOLD-SCRAP', 'Gold scrap 22K', 'GOLD', '22K', 'GM', item_type='BULLION')
        self.ring = item('RING-22K', 'Gold ring 22K', 'GOLD', '22K', 'PCS', serial_tracking=True, hsn='7113')
        self.casting = item('CAST-22K', 'Cast rings (semi-finished)', 'GOLD', '22K', 'GM', hsn='7113')
        self.polished = item('POLISHED-22K', 'Polished rings (semi-finished)', 'GOLD', '22K', 'GM', hsn='7113')
        self.gold_wh = SKU.objects.create(tenant=self.tenant, code='GOLD-22K-WH', item=self.gold, location=self.wh, unit_cost=D('6000'),
                                          gross_weight=D('1'))
        post_receipt(self.actor, location=self.wh, sku=self.gold_wh, quantity=500)

        self.jw_a = svc.create_job_worker(self.actor, code='CAST01', legal_name='ABC Casting Works', gstin='27BBBBB1111B1Z5', state='Maharashtra',
                                          default_sac='9988', lead_time_days=5, default_loss_percent=D('1'), max_loss_percent=D('1.6'),
                                          specializations=['CASTING', 'GOLD'], city='Mumbai')
        self.jw_b = svc.create_job_worker(self.actor, code='POL01', legal_name='Surat Polishers', gstin='24CCCCC2222C1Z5', state='Gujarat',
                                          default_sac='9988', lead_time_days=4, default_loss_percent=D('0.5'), max_loss_percent=D('1'),
                                          specializations=['POLISHING'], city='Surat')
        JobWorkerPrice.objects.create(tenant=self.tenant, job_worker=self.jw_a, operation_code='CASTING', effective_from=date(2020, 1, 1),
                                      rate_basis='PER_PIECE', rate=D('100'), minimum_amount=D('0'))
        JobWorkerPrice.objects.create(tenant=self.tenant, job_worker=self.jw_b, operation_code='POLISHING', effective_from=date(2020, 1, 1),
                                      rate_basis='PER_GRAM', rate=D('20'))
        self.approve_rules()

    def _member(self, username, role):
        user = User.objects.create_user(username, password='pw-12345678')
        TenantMembership.objects.create(tenant=self.tenant, user=user, role='member', all_locations=True)
        JobWorkRole.objects.create(tenant=self.tenant, user=user, role=role)
        return Actor(self.tenant, user)

    def approve_rules(self):
        compliance.seed_statutory_rules(self.tax1)
        for rule in ComplianceRule.objects.filter(tenant=self.tenant):
            compliance.approve_master(self.tax2, rule)
        self.sac = TaxRate.objects.create(tenant=self.tenant, code='9988', description='Job work - Chapter 71 goods (test fixture)',
                                          cgst_rate=D('0.75'), sgst_rate=D('0.75'), igst_rate=D('1.5'), effective_from=date(2020, 1, 1),
                                          created_by=self.tax1.user)
        compliance.approve_master(self.tax2, self.sac)

    # -- builders ---------------------------------------------------------

    def ring_order(self, gold=100, rings=10, job_worker=None, **kw):
        order = svc.create_order(self.operator, job_worker=job_worker or self.jw_a, source_location=self.wh, operation_code='CASTING',
                                 transaction_type='CASTING', lines=[
                                     {'line_type': 'INPUT', 'item': self.gold, 'quantity': gold},
                                     {'line_type': 'OUTPUT', 'item': self.ring, 'quantity': rings, 'net_weight': D('9.5') * rings},
                                     {'line_type': 'SCRAP', 'item': self.scrap, 'quantity': 0},
                                 ], **kw)
        svc.submit_order(self.operator, order)
        svc.approve_order(self.manager, order)
        return JobWorkOrder.objects.get(pk=order.pk)

    def send(self, order, **transport):
        svc.reserve_material(self.operator, order)
        dispatch = svc.post_dispatch(self.operator, svc.create_dispatch(self.operator, order, vehicle_no='MH01AB1234', **transport))
        for eway in dispatch.eway_bills.filter(status='PENDING'):
            compliance.generate_eway_bill(self.operator, eway, ewb_no=f'EWB{dispatch.pk:010d}', valid_until=timezone.now() + timedelta(days=1))
        svc.deliver_dispatch(self.operator, dispatch)
        return dispatch

    def lines(self, order):
        return {l.line_type: l for l in order.lines.all()}

    def report_rings(self, order, *, rings=10, per_ring=D('9.5'), scrap=D('3.5'), loss=D('1.5'), loss_class='PROCESS', start=1):
        ln = self.lines(order)
        specs = [{'kind': 'OUTPUT', 'input_line': ln['INPUT'], 'result_line': ln['OUTPUT'], 'quantity': 1, 'gross_weight': per_ring,
                  'net_weight': per_ring, 'barcode': f'{order.order_no}-R{i:02d}', 'huid': f'HU{order.pk:02d}{i:02d}X'}
                 for i in range(start, start + rings)]
        if scrap:
            specs.append({'kind': 'SCRAP', 'input_line': ln['INPUT'], 'result_line': ln['SCRAP'], 'input_qty': scrap, 'quantity': scrap,
                          'scrap_disposition': 'RETURNED'})
        if loss:
            specs.append({'kind': 'LOSS', 'input_line': ln['INPUT'], 'input_qty': loss, 'loss_class': loss_class})
        return svc.post_process_report(self.operator, svc.create_process_report(self.operator, order, lines=specs, reference='JW-DOCKET-1'))

    def bring_back(self, order):
        ret = svc.post_dispatch(self.operator, svc.create_return(self.operator, order, vehicle_no='MH01AB1234'))
        for eway in ret.eway_bills.filter(status='PENDING'):
            compliance.generate_eway_bill(self.operator, eway, ewb_no=f'EWB{ret.pk:010d}R')
        receipt = svc.receive_return(self.operator, ret)
        return ret, receipt

    def qc_all(self, receipt, rejected=0, rework=0):
        results = []
        for rl in receipt.lines.filter(qc_pending_qty__gt=0):
            if rejected:
                results.append({'receipt_line': rl, 'rejected': 1})
                rejected -= 1
            elif rework:
                results.append({'receipt_line': rl, 'rework': 1})
                rework -= 1
            else:
                results.append({'receipt_line': rl, 'accepted': rl.qc_pending_qty})
        return svc.record_qc(self.inspector, receipt, results=results, checks={'weight': 'ok', 'huid': 'ok'})


class SimpleJobWorkTests(JobWorkFixture):
    def test_110_111_simple_job_work_end_to_end_and_metal_reconciliation(self):
        order = self.ring_order()
        self.assertEqual((order.rate_basis, order.rate, order.expected_charge), ('PER_PIECE', D('100'), D('1000.00')))
        # Reservation blocks the same gold from going to a second job worker
        svc.reserve_material(self.operator, order)
        self.assertEqual(self.lines(order)['INPUT'].reserved_qty, D('100'))
        other = self.ring_order(gold=450)
        other, short = svc.reserve_material(self.operator, other)
        self.assertEqual((other.status, self.lines(other)['INPUT'].reserved_qty), ('MATERIAL_PENDING', D('400')))
        self.assertTrue(short)
        with self.assertRaises(InventoryError):
            svc.post_dispatch(self.operator, svc.create_dispatch(self.operator, other, lines=[
                {'order_line': self.lines(other)['INPUT'], 'quantity': 450}]))
        svc.cancel_order(self.manager, other, reason='Duplicate requirement')
        dispatch = svc.post_dispatch(self.operator, svc.create_dispatch(self.operator, order, vehicle_no='MH01AB1234'))
        # Delivery challan, not an invoice; e-way bill applicability from the approved rule (value above threshold)
        challan = dispatch.challan
        self.assertEqual((challan.document_kind, challan.reason, challan.consignor_gstin, challan.consignee_gstin),
                         ('DELIVERY_CHALLAN', 'JOB_WORK', '27AAAAA0000A1Z5', '27BBBBB1111B1Z5'))
        self.assertEqual(challan.lines.get().quantity, D('100'))
        self.assertTrue(dispatch.eway_bill_required)
        # Section 143 due date from the approved RETURN_PERIOD rule (inputs: 12 months)
        self.assertEqual(dispatch.compliance_due_date, compliance.add_months(TODAY, 12))
        # In transit: not at the job worker yet, not in the warehouse either
        self.assertEqual(on_hand(self.tenant, self.gold, self.wh), D('400'))
        self.assertEqual(svc.jw_balance(order)['qty'], D('0'))
        with self.assertRaises(JobWorkError):  # no movement without the e-way bill
            svc.deliver_dispatch(self.operator, dispatch)
        compliance.generate_eway_bill(self.operator, dispatch.eway_bills.get(), ewb_no='331000000001')
        svc.deliver_dispatch(self.operator, dispatch)
        jw_loc = self.jw_a.default_location
        self.assertEqual(on_hand(self.tenant, self.gold, jw_loc), D('100'))
        # Ownership never changed: the stock at the job worker is still the principal's, at the same historical cost
        held = svc.jw_balance(order)
        self.assertEqual((held['qty'], held['value']), (D('100'), D('600000.00')))
        self.assertEqual(set(JobWorkerStockEntry.objects.filter(order=order).values_list('owner', flat=True)), {'PRINCIPAL'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'RECEIVED_BY_JOB_WORKER')

        self.report_rings(order)
        rec = reports.reconcile(order)
        metal = rec['metal']
        # 95 + 3.5 + 1.5 = 100 g: reconciled
        self.assertEqual((metal['sent'], metal['consumed'], metal['scrap'], metal['loss'], metal['difference']),
                         (D('100'), D('95'), D('3.5'), D('1.5'), D('0')))
        self.assertEqual(metal['loss_percent'], D('1.500'))
        self.assertFalse(JobWorkException.objects.filter(order=order, exception_type='EXCESS_LOSS').exists())
        self.assertEqual(on_hand(self.tenant, self.gold, jw_loc), D('0'))
        self.assertEqual(on_hand(self.tenant, self.ring, jw_loc), D('10'))
        # Material value flows into the output: 95 g + 1.5 g process loss absorbed; the 3.5 g scrap keeps its own value
        ring_value = InventoryBalance.objects.filter(tenant=self.tenant, item=self.ring, location=jw_loc).aggregate(v=Sum('cost_value'))['v']
        self.assertEqual(ring_value, D('579000.00'))

        ret, receipt = self.bring_back(order)
        self.assertEqual(ret.challan.reason, 'JOB_WORK_RETURN')
        order.refresh_from_db()
        self.assertEqual(order.status, 'QC_PENDING')
        unit = JewelleryUnit.objects.get(tenant=self.tenant, barcode=f'{order.order_no}-R01')
        self.assertEqual((unit.status, unit.current_location), ('QC', self.wh))  # never straight to available stock
        self.qc_all(receipt)
        unit.refresh_from_db()
        self.assertEqual(unit.status, 'AVAILABLE')
        self.assertEqual(on_hand(self.tenant, self.scrap, self.wh), D('3.5'))

        # Test 114: invoice 10 x 100, GST from the tax master (intra-state -> CGST + SGST)
        invoice = finance.create_vendor_invoice(self.accounts, order, vendor_invoice_no='ABC/101', invoice_date=TODAY, billed_quantity=10,
                                                cgst=D('7.50'), sgst=D('7.50'))
        self.assertEqual(invoice.status, 'MATCHED', invoice.match_result)
        with self.assertRaises(JobWorkError):  # maker-checker
            finance.approve_invoice(self.accounts, invoice)
        finance.approve_invoice(self.manager, invoice)
        invoice = finance.post_invoice(self.accounts, invoice)
        snap = invoice.tax_snapshot
        self.assertEqual((snap.supply_type, snap.cgst, snap.sgst, snap.igst), ('INTRA', D('7.50'), D('7.50'), D('0')))
        self.assertEqual(invoice.gl_status, 'DISABLED')
        unit.refresh_from_db()
        self.assertEqual(unit.total_cost, D('57900.00') + D('100.00'))  # job charge capitalized into the piece
        with self.assertRaises(JobWorkError):  # duplicate vendor invoice number
            finance.create_vendor_invoice(self.accounts, order, vendor_invoice_no='abc/101', invoice_date=TODAY, billed_quantity=1)

        finance.complete_order(self.manager, order)
        finance.close_order(self.manager, order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'CLOSED')
        self.assertTrue(JobWorkAuditLog.objects.filter(order=order, action='post', document_type='JOB_WORK_DISPATCH').exists())

        # Test 118: ITC-04 generated from the ledgers and reconciled before it can be marked prepared
        fy = TODAY.year if TODAY.month >= 4 else TODAY.year - 1
        start, end, code = compliance.itc04_period('ANNUAL', fy)
        itc = compliance.generate_itc04(self.tax1, period_start=start, period_end=end, period_code=code)
        tables = {t: itc.lines.filter(table=t) for t in ('4', '5A', '5B')}
        self.assertEqual(itc.frequency, 'ANNUAL')
        self.assertEqual(tables['4'].get().quantity, D('100'))
        self.assertEqual(tables['5A'].filter(item_no='RING-22K').aggregate(q=Sum('quantity'))['q'], D('10'))
        self.assertEqual(tables['5A'].get(item_no='GOLD-22K').loss_quantity, D('1.5'))
        self.assertTrue(all(l.original_challan_no == challan.challan_no for l in tables['5A']))
        self.assertEqual(itc.status, 'RECONCILED', itc.summary)
        compliance.mark_itc04_prepared(self.tax1, itc)

    def test_112_117_multi_level_job_work_with_wip_transfer(self):
        order_a = svc.create_order(self.operator, job_worker=self.jw_a, source_location=self.wh, operation_code='CASTING',
                                   transaction_type='CASTING', lines=[
                                       {'line_type': 'INPUT', 'item': self.gold, 'quantity': 50},
                                       {'line_type': 'OUTPUT', 'item': self.casting, 'quantity': D('49.5')}])
        svc.submit_order(self.operator, order_a)
        svc.approve_order(self.manager, order_a)
        self.send(order_a)
        a = self.lines(order_a)
        svc.post_process_report(self.operator, svc.create_process_report(self.operator, order_a, lines=[
            {'kind': 'OUTPUT', 'input_line': a['INPUT'], 'input_qty': D('49.5'), 'result_line': a['OUTPUT'], 'quantity': D('49.5'),
             'gross_weight': D('49.5')},
            {'kind': 'LOSS', 'input_line': a['INPUT'], 'input_qty': D('0.5'), 'loss_class': 'PROCESS'}]))       # 1% <= 1.6%
        # Next stage at an inter-state job worker, fed straight from job worker A
        order_b = svc.create_order(self.operator, job_worker=self.jw_b, source_location=self.wh, parent=order_a, operation_code='POLISHING',
                                   transaction_type='POLISHING', lines=[
                                       {'line_type': 'WIP', 'item': self.casting, 'quantity': D('49.5')},
                                       {'line_type': 'OUTPUT', 'item': self.polished, 'quantity': D('49.2')}])
        svc.submit_order(self.operator, order_b)
        svc.approve_order(self.manager, order_b)
        self.assertEqual(order_b.rate_basis, 'PER_GRAM')
        transfer = svc.post_dispatch(self.operator, svc.create_wip_transfer(self.operator, order_a, order_b, vehicle_no='GJ05XY0001'))
        self.assertEqual(transfer.challan.reason, 'JW_TO_JW')
        self.assertEqual((transfer.challan.consignor_gstin, transfer.challan.consignee_gstin), ('27BBBBB1111B1Z5', '24CCCCC2222C1Z5'))
        self.assertTrue(transfer.eway_bill_required)  # inter-state job work movement, any value
        self.assertIn('Inter-state', transfer.eway_bill_reason)
        compliance.generate_eway_bill(self.operator, transfer.eway_bills.get(), ewb_no='241000000009')
        svc.deliver_dispatch(self.operator, transfer)
        order_a.refresh_from_db()
        order_b.refresh_from_db()
        self.assertEqual((order_a.status, order_b.status), ('TRANSFERRED', 'RECEIVED_BY_JOB_WORKER'))
        self.assertEqual(order_b.compliance_due_date, order_a.compliance_due_date)  # the statutory clock keeps running
        self.assertEqual(on_hand(self.tenant, self.casting, self.jw_b.default_location), D('49.5'))
        self.assertEqual(on_hand(self.tenant, self.casting, self.jw_a.default_location), D('0'))
        # Value continuity across the transfer: 50 g x 6,000 (49.5 g output + 0.5 g process loss absorbed) arrives at B intact
        self.assertEqual(svc.jw_balance(order_b)['value'], D('300000.00'))
        self.assertEqual(svc.jw_balance(order_a)['value'], D('0'))
        b = self.lines(order_b)
        svc.post_process_report(self.operator, svc.create_process_report(self.operator, order_b, lines=[
            {'kind': 'OUTPUT', 'input_line': b['WIP'], 'input_qty': D('49.2'), 'result_line': b['OUTPUT'], 'quantity': D('49.2'),
             'gross_weight': D('49.2')},
            {'kind': 'LOSS', 'input_line': b['WIP'], 'input_qty': D('0.3'), 'loss_class': 'PROCESS'}]))         # 0.61% <= 1%
        _, receipt = self.bring_back(order_b)
        self.qc_all(receipt)
        self.assertEqual(on_hand(self.tenant, self.polished, self.wh), D('49.2'))
        finance.complete_order(self.manager, order_a)
        finance.complete_order(self.manager, order_b)
        # ITC-04: goods sent A -> B appear in table 5B; B's receipt links back to the principal's original challan
        fy = TODAY.year if TODAY.month >= 4 else TODAY.year - 1
        start, end, code = compliance.itc04_period('ANNUAL', fy)
        itc = compliance.generate_itc04(self.tax1, period_start=start, period_end=end, period_code=code)
        original = order_a.dispatches.get(movement_type='PRINCIPAL_TO_JW').challan.challan_no
        self.assertEqual(itc.lines.get(table='5B').original_challan_no, original)
        self.assertEqual(itc.lines.get(table='5A', item_no='POLISHED-22K').original_challan_no, original)
        self.assertEqual(itc.status, 'RECONCILED', itc.summary)
        # Reverse trace: gold batch -> dispatch -> job worker -> order -> output
        trail = reports.trace_lot(self.tenant, self.gold)
        self.assertEqual(trail[0]['order'], order_a)

    def test_113_inter_state_invoice_needs_igst(self):
        order = svc.create_order(self.operator, job_worker=self.jw_b, source_location=self.wh, operation_code='POLISHING',
                                 transaction_type='POLISHING', lines=[
                                     {'line_type': 'INPUT', 'item': self.gold, 'quantity': 10},
                                     {'line_type': 'OUTPUT', 'item': self.polished, 'quantity': D('9.9')}])
        svc.submit_order(self.operator, order)
        svc.approve_order(self.manager, order)
        self.send(order)
        ln = self.lines(order)
        svc.post_process_report(self.operator, svc.create_process_report(self.operator, order, lines=[
            {'kind': 'OUTPUT', 'input_line': ln['INPUT'], 'input_qty': D('9.9'), 'result_line': ln['OUTPUT'], 'quantity': D('9.9'),
             'gross_weight': D('9.9')},
            {'kind': 'LOSS', 'input_line': ln['INPUT'], 'input_qty': D('0.1'), 'loss_class': 'PROCESS'}]))
        _, receipt = self.bring_back(order)
        self.qc_all(receipt)
        wrong = finance.create_vendor_invoice(self.accounts, order, vendor_invoice_no='SP/1', invoice_date=TODAY, billed_quantity=D('9.9'),
                                              cgst=D('1.49'), sgst=D('1.49'))
        self.assertEqual(wrong.status, 'EXCEPTION')
        self.assertTrue(JobWorkException.objects.filter(document_no=wrong.document_no, exception_type='GST_MISMATCH').exists())
        with self.assertRaises(JobWorkError):
            finance.approve_invoice(self.manager, wrong)
        right = finance.create_vendor_invoice(self.accounts, order, vendor_invoice_no='SP/2', invoice_date=TODAY, billed_quantity=D('9.9'),
                                              igst=D('2.97'))
        self.assertEqual(right.status, 'MATCHED', right.match_result)
        self.assertEqual(right.match_result['tax']['supply_type'], 'INTER')


class ExceptionTests(JobWorkFixture):
    def test_111_unexplained_difference_blocks_closure_until_resolved(self):
        order = self.ring_order()
        self.send(order)
        self.report_rings(order, loss=D('0.5'))           # 95 + 3.5 + 0.5 = 99 g reported
        ln = self.lines(order)
        # The job worker returns rings and scrap and says nothing is left - 1 g is unaccounted for
        ret = svc.post_dispatch(self.operator, svc.create_return(self.operator, order, lines=[{'order_line': ln['SCRAP'], 'quantity': D('3.5')}],
                                                                 barcodes=[f'{order.order_no}-R{i:02d}' for i in range(1, 11)]))
        compliance.generate_eway_bill(self.operator, ret.eway_bills.get(), ewb_no='331000000055')
        receipt = svc.receive_return(self.operator, ret)
        self.qc_all(receipt)
        self.assertEqual(reports.reconcile(order)['metal']['difference'], D('1'))
        with self.assertRaises(JobWorkError) as ctx:
            finance.complete_order(self.manager, order)
        self.assertIn('unexplained difference', str(ctx.exception))
        exc = JobWorkException.objects.get(order=order, exception_type='WEIGHT_DIFFERENCE')
        self.assertEqual((exc.weight, exc.blocking, exc.status), (D('1.000'), True, 'OPEN'))
        with self.assertRaises(JobWorkError):  # the manager raised it - someone else approves
            finance.approve_exception(self.manager, exc, resolution='DEBIT_NOTE', reason='Karigar shortage', recovery_amount=6000,
                                      loss_class='JOB_WORKER_LIABILITY')
        finance.approve_exception(self.actor, exc, resolution='DEBIT_NOTE', reason='Karigar shortage', recovery_amount=6000,
                                  loss_class='JOB_WORKER_LIABILITY')
        finance.write_off_difference(self.actor, exc)
        self.assertEqual(reports.reconcile(order)['metal']['difference'], D('0'))
        note = finance.create_note(self.accounts, note_type='DEBIT', order=order, amount=None, reason='1 g gold shortage', exception=exc)
        self.assertEqual(note.amount, D('6000.00'))
        finance.approve_note(self.manager, note)
        finance.post_note(self.accounts, note)
        exc.refresh_from_db()
        self.assertEqual(exc.status, 'RESOLVED')
        finance.complete_order(self.manager, order)

    def test_116_excess_loss_needs_manager_approval(self):
        ApprovalRule.objects.create(tenant=self.tenant, metric='LOSS_PERCENT', min_value=D('0'), max_value=D('1'), required_role='SUPERVISOR')
        ApprovalRule.objects.create(tenant=self.tenant, metric='LOSS_PERCENT', min_value=D('1'), required_role='MANAGER')
        order = self.ring_order(expected_loss_percent=D('1'), max_loss_percent=D('1.25'))
        self.send(order)
        self.report_rings(order, scrap=D('2.8'), loss=D('2.2'))      # 2.2% > 1.25%
        exc = JobWorkException.objects.get(order=order, exception_type='EXCESS_LOSS')
        self.assertEqual((exc.actual_value, exc.required_role, exc.blocking), (D('2.200'), 'MANAGER', True))
        _, receipt = self.bring_back(order)
        self.qc_all(receipt)
        with self.assertRaises(JobWorkError):
            finance.complete_order(self.supervisor, order)
        with self.assertRaises(JobWorkError):  # supervisor is below the required role
            finance.approve_exception(self.supervisor, exc, resolution='MANAGEMENT_WAIVER', reason='Complex design')
        with self.assertRaises(JobWorkError):  # reason is mandatory
            finance.approve_exception(self.manager, exc, resolution='MANAGEMENT_WAIVER', reason='')
        finance.approve_exception(self.manager, exc, resolution='MANAGEMENT_WAIVER', reason='Complex filigree design')
        finance.complete_order(self.supervisor, order)

    def test_115_short_return_is_never_assumed_consumed(self):
        order = self.ring_order()
        self.send(order)
        self.report_rings(order)
        ret = svc.post_dispatch(self.operator, svc.create_return(self.operator, order))
        compliance.generate_eway_bill(self.operator, ret.eway_bills.get(), ewb_no='331000000077')
        ring_lines = [dl for dl in ret.lines.filter(item=self.ring)]
        others = [dl for dl in ret.lines.exclude(item=self.ring)]
        receipt = svc.receive_return(self.operator, ret, lines=[{'dispatch_line': dl} for dl in ring_lines[:8] + others])
        ret.refresh_from_db()
        self.assertEqual(ret.status, 'PARTIALLY_RECEIVED')
        self.qc_all(receipt)
        with self.assertRaises(JobWorkError):  # 2 rings still in transit
            finance.complete_order(self.manager, order)
        with self.assertRaises(JobWorkError):  # the shortage must be classified
            svc.short_close_return(self.operator, ret, reason='Not in parcel', loss_class='')
        exc = svc.short_close_return(self.operator, ret, reason='Not in parcel', loss_class='JOB_WORKER_LIABILITY')
        self.assertEqual((exc.exception_type, exc.blocking), ('SHORT_RETURN', True))
        missing = JewelleryUnit.objects.filter(tenant=self.tenant, barcode__in=[dl.barcode for dl in ring_lines[8:]])
        self.assertEqual(set(missing.values_list('status', flat=True)), {'MISSING'})
        with self.assertRaises(JobWorkError):
            finance.complete_order(self.manager, order)
        finance.approve_exception(self.actor, exc, resolution='VENDOR_RECOVERY', reason='Courier claim', recovery_amount=D('115800'))
        finance.complete_order(self.manager, order)

    def test_57_three_way_match_rejects_billing_for_rejected_pieces(self):
        order = self.ring_order()
        self.send(order)
        self.report_rings(order)
        _, receipt = self.bring_back(order)
        self.qc_all(receipt, rejected=1)                    # 9 accepted, 1 rejected
        invoice = finance.create_vendor_invoice(self.accounts, order, vendor_invoice_no='ABC/9', invoice_date=TODAY, billed_quantity=10,
                                                cgst=D('7.50'), sgst=D('7.50'))
        self.assertEqual(invoice.status, 'EXCEPTION')
        self.assertIn('Billed 10', ' '.join(invoice.match_result['issues']))
        ok = finance.create_vendor_invoice(self.accounts, order, vendor_invoice_no='ABC/10', invoice_date=TODAY, billed_quantity=9,
                                           cgst=D('6.75'), sgst=D('6.75'))
        self.assertEqual(ok.status, 'MATCHED', ok.match_result)

    def test_47_rework_creates_a_new_order(self):
        order = self.ring_order()
        self.send(order)
        self.report_rings(order)
        _, receipt = self.bring_back(order)
        qc = self.qc_all(receipt, rework=2)
        self.assertIsNotNone(qc.rework_order)
        rework = qc.rework_order
        self.assertEqual((rework.rework_of, rework.transaction_type, rework.expected_charge), (order, 'REPAIR_JOB_WORK', D('0')))
        self.assertEqual(rework.lines.filter(line_type='INPUT').count(), 2)
        order.refresh_from_db()
        self.assertEqual(order.status, 'REWORK')
        with self.assertRaises(JobWorkError):
            finance.complete_order(self.manager, order)


class ControlTests(JobWorkFixture):
    def test_119_dispatch_reversal_not_deletion(self):
        order = self.ring_order()
        svc.reserve_material(self.operator, order)
        dispatch = svc.post_dispatch(self.operator, svc.create_dispatch(self.operator, order))
        with self.assertRaises(PermissionDenied):
            svc.reverse_dispatch(self.operator, dispatch, reason='Wrong job worker')
        svc.reverse_dispatch(self.manager, dispatch, reason='Wrong job worker')
        dispatch.refresh_from_db()
        self.assertEqual((dispatch.status, dispatch.challan.status), ('REVERSED', 'CANCELLED'))
        self.assertEqual(dispatch.eway_bills.get().status, 'CANCELLED')
        self.assertEqual(on_hand(self.tenant, self.gold, self.wh), D('500'))
        self.assertEqual(self.lines(order)['INPUT'].dispatched_qty, D('0'))
        entry = JobWorkerStockEntry.objects.first()
        self.assertIsNone(entry)  # nothing reached the job worker, so nothing was ever posted there

    def test_process_report_reversal_and_immutable_ledger(self):
        order = self.ring_order()
        self.send(order)
        report = self.report_rings(order)
        entry = JobWorkerStockEntry.objects.filter(order=order).first()
        with self.assertRaises(ValidationError):
            entry.delete()
        with self.assertRaises(ValidationError):
            JobWorkerStockEntry.objects.filter(order=order).delete()
        svc.reverse_process_report(self.manager, report, reason='Wrong weights keyed')
        jw_loc = self.jw_a.default_location
        self.assertEqual(on_hand(self.tenant, self.gold, jw_loc), D('100'))
        self.assertEqual(on_hand(self.tenant, self.ring, jw_loc), D('0'))
        self.assertEqual(svc.material_wip(order), D('0'))
        self.assertEqual(svc.jw_balance(order)['value'], D('600000.00'))

    def test_output_must_account_for_fine_metal(self):
        order = self.ring_order()
        self.send(order)
        ln = self.lines(order)
        svc.post_process_report(self.operator, svc.create_process_report(self.operator, order, lines=[
            {'kind': 'OUTPUT', 'input_line': ln['INPUT'], 'input_qty': 12, 'result_line': ln['OUTPUT'], 'quantity': 1, 'gross_weight': D('9.5'),
             'barcode': 'X-1'}]))
        exc = JobWorkException.objects.get(order=order, exception_type='FINE_WEIGHT_VARIANCE')
        self.assertTrue(exc.blocking)
        with self.assertRaises(JobWorkError):  # loss must always be classified
            svc.create_process_report(self.operator, order, lines=[{'kind': 'LOSS', 'input_line': ln['INPUT'], 'input_qty': 1}])

    def test_missing_rules_are_flagged_not_assumed(self):
        ComplianceRule.objects.filter(tenant=self.tenant).update(status='RETIRED')
        order = self.ring_order()
        svc.reserve_material(self.operator, order)
        dispatch = svc.post_dispatch(self.operator, svc.create_dispatch(self.operator, order))
        self.assertIsNone(dispatch.compliance_due_date)
        self.assertFalse(dispatch.eway_bill_required)
        types = set(JobWorkException.objects.filter(order=order).values_list('exception_type', flat=True))
        self.assertEqual(types, {'COMPLIANCE_RULE_MISSING'})

    def test_tax_master_versioning_and_approval(self):
        with self.assertRaises(PermissionDenied):
            compliance.approve_master(self.manager, TaxRate.objects.create(tenant=self.tenant, code='9988', description='x', igst_rate=5,
                                                                         effective_from=TODAY, created_by=self.tax1.user))
        draft = TaxRate.objects.filter(tenant=self.tenant, status='DRAFT').get()
        self.assertEqual(compliance.resolve_tax_rate(self.tenant, '9988', TODAY).pk, self.sac.pk)  # drafts are never used
        with self.assertRaises(JobWorkError):  # maker-checker
            compliance.approve_master(self.tax1, draft)
        compliance.approve_master(self.tax2, draft)
        self.sac.refresh_from_db()
        self.assertEqual(self.sac.effective_to, TODAY - timedelta(days=1))
        self.assertEqual(compliance.resolve_tax_rate(self.tenant, '9988', TODAY).igst_rate, D('5'))
        self.assertEqual(compliance.resolve_tax_rate(self.tenant, '9988', TODAY - timedelta(days=1)).pk, self.sac.pk)
        with self.assertRaises(JobWorkError):
            compliance.resolve_tax_rate(self.tenant, '9999', TODAY)

    def test_price_resolution_and_minimum_charge(self):
        JobWorkerPrice.objects.create(tenant=self.tenant, job_worker=self.jw_a, operation_code='CASTING', item=self.ring,
                                      effective_from=date(2021, 1, 1), rate_basis='PER_PIECE', rate=D('120'), minimum_amount=D('1000'))
        JobWorkerPrice.objects.create(tenant=self.tenant, job_worker=self.jw_a, operation_code='CASTING', item=self.ring,
                                      effective_from=date(2021, 1, 1), minimum_quantity=D('50'), rate_basis='PER_PIECE', rate=D('90'))
        pick = lambda qty: pricing.resolve_price(self.tenant, self.jw_a, operation_code='CASTING', item=self.ring, quantity=qty)
        self.assertEqual(pick(5).rate, D('120'))                     # item-specific beats the generic CASTING price
        self.assertEqual(pick(60).rate, D('90'))                     # volume break
        self.assertEqual(pricing.resolve_price(self.tenant, self.jw_a, operation_code='CASTING', item=self.gold).rate, D('100'))
        self.assertIsNone(pricing.resolve_price(self.tenant, self.jw_a, operation_code='PLATING'))
        self.assertEqual(pricing.charge('PER_PIECE', D('120'), D('1000'), pieces=5), D('1000.00'))   # 5 x 120 = 600 -> minimum 1,000
        self.assertEqual(pricing.charge('PER_GRAM', D('20'), grams=D('48.5')), D('970.00'))
        order = self.ring_order(rings=5)
        self.assertEqual((order.rate, order.expected_charge), (D('120'), D('1000.00')))

    def test_tenant_isolation(self):
        other_user = User.objects.create_user('intruder', password='pw-12345678')
        other = Actor(create_tenant_for_user(other_user, 'Other Jewellers'), other_user)
        order = self.ring_order()
        with self.assertRaises(JobWorkError):
            svc.reserve_material(other, order)
        with self.assertRaises(JobWorkError):
            svc.create_order(other, job_worker=self.jw_a, source_location=self.wh, lines=[{'item': self.gold, 'quantity': 1}])
        self.assertEqual(reports.dashboard(other.tenant)['operations']['open'], 0)

    def test_order_approval_matrix_and_maker_checker(self):
        ApprovalRule.objects.create(tenant=self.tenant, metric='MATERIAL_VALUE', min_value=D('500000'), required_role='HEAD_OF_MANUFACTURING')
        order = svc.create_order(self.operator, job_worker=self.jw_a, source_location=self.wh, lines=[
            {'item': self.gold, 'quantity': 100}, {'line_type': 'OUTPUT', 'item': self.ring, 'quantity': 10}])
        svc.submit_order(self.operator, order)
        order.refresh_from_db()
        self.assertEqual(order.required_approval_role, 'HEAD_OF_MANUFACTURING')
        with self.assertRaises(JobWorkError):
            svc.approve_order(self.manager, order)
        with self.assertRaises(PermissionDenied):
            svc.approve_order(self.operator, order)
        head = self._member('head', 'HEAD_OF_MANUFACTURING')
        svc.approve_order(head, order)

    def test_compliance_bands_and_scan(self):
        order = self.ring_order()
        self.send(order)
        band = compliance.alert_band(self.tenant, TODAY - timedelta(days=200), compliance.add_months(TODAY - timedelta(days=200), 12))
        self.assertEqual((band['level'], band['band']), ('WARNING', 'AMBER'))
        band = compliance.alert_band(self.tenant, TODAY - timedelta(days=400), compliance.add_months(TODAY - timedelta(days=400), 12))
        self.assertEqual((band['level'], band['band']), ('OVERDUE', 'RED'))
        JobWorkOrder.objects.filter(pk=order.pk).update(compliance_due_date=TODAY - timedelta(days=1), first_dispatch_date=TODAY - timedelta(days=366))
        raised = finance.scan_compliance(self.actor)
        self.assertTrue(any(e.exception_type == 'MATERIAL_OVERDUE' and e.severity == 'CRITICAL' for e in raised))
        self.assertEqual(len(finance.scan_compliance(self.actor)), len(raised))   # idempotent
        self.assertEqual(JobWorkException.objects.filter(order=order, exception_type='MATERIAL_OVERDUE').count(), 1)
        rows = reports.material_at_job_workers(self.tenant)
        self.assertEqual(rows[0]['q'], D('100'))
        self.assertEqual(rows[0]['band']['level'], 'OVERDUE')


class ProductionSubcontractTests(JobWorkFixture):
    """Business Central-style subcontracting: a routing operation of a released production order performed by a job worker."""

    def setUp(self):
        super().setUp()
        from inventory.models import Bin
        from manufacturing import services as msvc
        from manufacturing.models import ManufacturingRole, Subcontractor, WorkCenter
        from manufacturing.security import get_setup as mfg_setup
        self.msvc = msvc
        self.fact = Location.objects.create(tenant=self.tenant, code='FACT', name='Karkhana', location_type='MANUFACTURING', allow_sales=False,
                                            gstin='27AAAAA0000A1Z5', company=self.company)
        mk = lambda code, kind: Bin.objects.create(tenant=self.tenant, location=self.fact, code=code, bin_type=kind)
        rm, prod, qc, fg, scrap = mk('RM', 'STORAGE'), mk('PROD', 'STAGING'), mk('QC', 'QC'), mk('FG', 'STORAGE'), mk('SCRAP', 'DAMAGED')
        self.prod_bin = prod
        gold_fact = SKU.objects.create(tenant=self.tenant, code='GOLD-22K-FACT', item=self.gold, location=self.fact, unit_cost=D('6000'),
                                       gross_weight=D('1'), default_bin=rm)
        post_receipt(self.actor, location=self.fact, sku=gold_fact, quantity=300, bin=rm)
        setup = mfg_setup(self.tenant)
        setup.company = self.company
        setup.default_production_location = setup.default_finished_goods_location = self.fact
        setup.default_production_bin, setup.default_material_bin, setup.default_qc_bin = prod, rm, qc
        setup.default_finished_goods_bin, setup.default_scrap_bin = fg, scrap
        setup.scrap_requires_approval = False
        setup.save()
        for name, role in (('bom_reviewer', 'BOM_REVIEWER'), ('bom_approver', 'BOM_APPROVER'), ('mfg_manager', 'MANAGER')):
            user = User.objects.create_user(name, password='pw-12345678')
            TenantMembership.objects.create(tenant=self.tenant, user=user, role='member', all_locations=True)
            ManufacturingRole.objects.create(tenant=self.tenant, user=user, role=role)
            setattr(self, name, Actor(self.tenant, user))
        self.sub = Subcontractor.objects.create(tenant=self.tenant, code='SUB-CAST', name='ABC Casting Works')
        self.jw_a.subcontractor = self.sub
        self.jw_a.save()
        JobWorkerPrice.objects.create(tenant=self.tenant, job_worker=self.jw_a, operation_code='CAST', effective_from=date(2020, 1, 1),
                                      rate_basis='PER_PIECE', rate=D('150'))
        wc = lambda code: WorkCenter.objects.create(tenant=self.tenant, code=code, name=code.title(), location=self.fact,
                                                    direct_labour_rate=D('300'), overhead_rate=D('60'))
        self.centers = {code: wc(code) for code in ('MELT', 'CASTWC', 'POLISH')}

    def released_order(self, *, final_subcontract=False):
        msvc = self.msvc
        bom, version = msvc.create_bom(self.actor, item=self.ring, bom_name='Ring', expected_net_weight=D('8.5'))
        msvc.save_bom_line(self.actor, version, component_item=self.gold, quantity=D('8.5'), consumption_basis='WEIGHT', flushing_method='MANUAL',
                           routing_link_code='CAST')
        for step in ('submit', 'review', 'approve', 'certify'):
            msvc.transition_version({'review': self.bom_reviewer, 'approve': self.bom_approver, 'certify': self.mfg_manager}.get(step, self.actor), version, step)
        routing, rversion = msvc.create_routing(self.actor, description='Ring', item=self.ring)
        ops = [('10', 'Melting', 'MELT', False), ('20', 'Casting', 'CASTWC', True)]
        if not final_subcontract:
            ops.append(('30', 'Polishing', 'POLISH', False))
        for no, desc, center, sub in ops:
            msvc.save_routing_line(self.actor, rversion, operation_no=no, description=desc, work_center=self.centers[center], run_time=D('1'),
                                   routing_link_code='CAST' if sub else '', subcontracting=sub, subcontractor=self.sub if sub else None)
        for step in ('submit', 'review', 'approve', 'certify'):
            msvc.transition_version({'review': self.bom_reviewer, 'approve': self.bom_approver, 'certify': self.mfg_manager}.get(step, self.actor), rversion, step)
        order = msvc.create_order(self.actor, item=self.ring, quantity=10)
        msvc.approve_order(self.actor, order)
        msvc.release_order(self.actor, order)
        from manufacturing import engine as meng
        gold = order.components.get(item=self.gold)
        meng.issue_material(self.actor, order, [{'component': gold, 'quantity': gold.expected_qty}])
        op10 = order.operations.get(operation_no='10')
        msvc.start_operation(self.actor, op10)
        msvc.complete_operation(self.actor, op10, output_qty=10, run_minutes=30)
        order.refresh_from_db()
        return order

    def subcontract_cycle(self, order):
        op20 = order.operations.get(operation_no='20')
        jwo = svc.create_from_production_operation(self.operator, op20)
        self.assertEqual((jwo.order_kind, jwo.selection_mode, jwo.job_worker, jwo.rate), ('SUBCONTRACT', 'FIXED', self.jw_a, D('150')))
        kinds = {l.line_type: l for l in jwo.lines.all()}
        self.assertEqual((kinds['COMPONENT'].item, kinds['COMPONENT'].quantity), (self.gold, D('85')))
        self.assertTrue(svc.line_is_memo(kinds['WIP']) and svc.line_is_memo(kinds['OUTPUT']))
        with self.assertRaises(JobWorkError):  # one open subcontract order per operation
            svc.create_from_production_operation(self.operator, op20)
        svc.submit_order(self.operator, jwo)
        svc.approve_order(self.manager, jwo)
        self.send(JobWorkOrder.objects.get(pk=jwo.pk))
        gold_line = kinds['COMPONENT']
        comp = gold_line.production_component
        svc.post_process_report(self.operator, svc.create_process_report(self.operator, jwo, lines=[
            {'kind': 'CONSUMPTION', 'input_line': gold_line, 'input_qty': 84},
            {'kind': 'LOSS', 'input_line': gold_line, 'input_qty': 1, 'loss_class': 'PROCESS'},
            {'kind': 'OUTPUT', 'input_line': kinds['WIP'], 'input_qty': 10, 'result_line': kinds['OUTPUT'], 'quantity': 10,
             'gross_weight': 84, 'net_weight': 84}]))
        comp.refresh_from_db()
        self.assertEqual(comp.consumed_qty, D('85'))   # consumption and loss went through the manufacturing engine into WIP
        _, receipt = self.bring_back(JobWorkOrder.objects.get(pk=jwo.pk))
        self.qc_all(receipt)
        return JobWorkOrder.objects.get(pk=jwo.pk), op20

    def test_87_subcontract_operation_output_and_cost_flow_to_production(self):
        from manufacturing.models import CostEntry
        order = self.released_order()
        jwo, op20 = self.subcontract_cycle(order)
        op20.refresh_from_db()
        self.assertEqual(op20.output_qty, D('10'))        # intermediate operation: accepted output reported on the operation
        self.assertEqual(jwo.output_posted_qty, D('10'))
        invoice = finance.create_vendor_invoice(self.accounts, jwo, vendor_invoice_no='ABC/P1', invoice_date=TODAY, billed_quantity=10,
                                                cgst=D('11.25'), sgst=D('11.25'))
        self.assertEqual(invoice.status, 'MATCHED', invoice.match_result)
        finance.approve_invoice(self.manager, invoice)
        finance.post_invoice(self.accounts, invoice)
        self.assertEqual(CostEntry.objects.filter(order=order, cost_type='SUBCONTRACT').aggregate(v=Sum('amount'))['v'], D('1500.00'))
        op20.refresh_from_db()
        self.assertEqual(op20.actual_cost, D('1500.00'))
        finance.complete_order(self.manager, jwo)
        finance.close_order(self.manager, jwo)
        self.assertEqual(reports.subcontracting_worksheet(self.tenant), [])

    def test_87_final_subcontract_operation_needs_output_confirmation(self):
        order = self.released_order(final_subcontract=True)
        rows = reports.subcontracting_worksheet(self.tenant)
        self.assertEqual((len(rows), rows[0]['job_worker'], rows[0]['status']), (1, self.jw_a, 'READY'))
        jwo, op20 = self.subcontract_cycle(order)
        order.refresh_from_db()
        self.assertEqual((order.produced_qty, jwo.output_posted_qty), (D('0'), D('0')))   # never posted blindly
        with self.assertRaises(JobWorkError):
            finance.complete_order(self.manager, jwo)
        svc.confirm_final_output(self.operator, jwo, quantity=10)
        order.refresh_from_db()
        self.assertEqual(order.produced_qty, D('10'))
        self.assertEqual(JewelleryUnit.objects.filter(tenant=self.tenant, item=self.ring).count(), 10)
        finance.complete_order(self.manager, jwo)
