"""Acceptance tests for the Location -> Store -> Staff / POS terminal / Tender architecture (spec section 45),
plus POS tender posting, snapshots, shifts, Excel import, API and audit."""
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from erp.models import (
    AuditLog, Company, ItemCategory, Location, POSPayment, POSRole, POSSession, POSShift, POSStaff, POSStaffAssignment,
    POSTerminal, Product, RetailImportBatch, SalesInvoice, Store, StoreTender, Tender,
)
from erp.retail import imports, services


class RetailFixture(TestCase):
    password = 'secret-123'

    def setUp(self):
        self.admin = User.objects.create_user('retail-admin', password='pw', is_staff=True)
        self.client.force_login(self.admin)
        self.company = Company.objects.create(company_code='GOLDIO', company_name='Goldio Jewellers')
        self.mumbai = Location.objects.create(company=self.company, location_code='L001', location_name='Mumbai', status='active')
        self.delhi = Location.objects.create(company=self.company, location_code='L002', location_name='Delhi', status='active')
        self.spare = Location.objects.create(company=self.company, location_code='L003', location_name='Pune', status='active')
        self.st1 = Store.objects.create(company=self.company, code='ST001', name='Mumbai Flagship', location=self.mumbai)
        self.st2 = Store.objects.create(company=self.company, code='ST002', name='Delhi Store', location=self.delhi)
        self.cashier = POSRole.objects.get(code='CASHIER')
        self.manager_role = POSRole.objects.get(code='MANAGER')
        self.cash = Tender.objects.get(code='CASH')
        self.upi = Tender.objects.get(code='UPI')
        self.pos1 = POSTerminal.objects.create(store=self.st1, code='POS-01', name='Counter 1')
        self.pos2 = POSTerminal.objects.create(store=self.st1, code='POS-02', name='Counter 2')
        self.delhi_pos = POSTerminal.objects.create(store=self.st2, code='POS-01', name='Delhi Counter')
        self.rahul = self.make_staff('EMP001', 'Rahul Sharma', 'rahul.pos', self.st1)

    def make_staff(self, code, name, login_id, store, role=None, password=None):
        staff = POSStaff(employee_code=code, name=name, login_id=login_id, store=store, staff_role=role or self.cashier)
        services.save_staff(staff, password=password or self.password)
        return staff

    def pos_login(self, terminal, login_id='rahul.pos', password=None):
        return self.client.post(reverse('pos_login'), {'staff_id': login_id, 'pin': password or self.password,
                                                       'terminal_id': terminal.pk})


class HierarchyTests(RetailFixture):
    def test_01_location_can_back_a_store(self):
        response = self.client.post(reverse('retail_store_new'), {
            'company': self.company.pk, 'code': 'ST003', 'name': 'Pune Store', 'store_type': 'showroom', 'status': 'active',
            'location': self.spare.pk, 'country': 'India'})
        store = Store.objects.get(code='ST003')
        self.assertRedirects(response, reverse('retail_store_card', args=[store.pk]))
        self.assertEqual(store.location, self.spare)

    def test_02_location_cannot_back_two_stores(self):
        response = self.client.post(reverse('retail_store_new'), {
            'company': self.company.pk, 'code': 'ST009', 'name': 'Second Mumbai', 'store_type': 'showroom',
            'status': 'active', 'location': self.mumbai.pk, 'country': 'India'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Store.objects.filter(code='ST009').exists())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Store.objects.create(company=self.company, code='ST010', name='Dup', location=self.mumbai)

    def test_03_store_can_have_many_staff(self):
        for n in range(2, 7):
            self.make_staff(f'EMP00{n}', f'Staff {n}', f'staff{n}.pos', self.st1)
        self.assertEqual(self.st1.pos_staff.count(), 6)

    def test_04_staff_belongs_to_one_store_and_moving_is_validated(self):
        # A single store FK: "assigning" EMP001 to ST002 is a move, not a second assignment.
        POSStaffAssignment.objects.create(staff=self.rahul, terminal=self.pos1, role='cashier')
        self.rahul.default_terminal = self.pos1
        self.rahul.save()
        self.rahul.store = self.st2
        services.save_staff(self.rahul)
        self.rahul.refresh_from_db()
        self.assertEqual(self.rahul.store, self.st2)
        self.assertIsNone(self.rahul.default_terminal)
        self.assertFalse(POSStaffAssignment.objects.filter(staff=self.rahul, active=True).exists())

    def test_05_store_can_have_many_terminals(self):
        for n in range(3, 8):
            POSTerminal.objects.create(store=self.st1, code=f'POS-0{n}', name=f'Counter {n}')
        self.assertEqual(self.st1.pos_terminals.count(), 7)

    def test_06_terminal_requires_store(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            POSTerminal.objects.create(code='ORPHAN', name='No store')
        response = self.client.post(reverse('retail_terminal_new'), {'code': 'ORPHAN', 'name': 'x', 'terminal_type': 'counter', 'status': 'active'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(POSTerminal.objects.filter(code='ORPHAN').exists())

    def test_terminal_code_unique_per_store(self):
        response = self.client.post(reverse('retail_store_add_terminal', args=[self.st1.pk]), {
            'code': 'pos-01', 'name': 'Duplicate', 'terminal_type': 'counter', 'status': 'active'})
        self.assertContains(response, 'already exists in store ST001')
        # Same code in another store is fine.
        self.assertEqual(self.delhi_pos.code, 'POS-01')

    def test_07_and_08_assign_tender_once(self):
        response = self.client.post(reverse('retail_store_add_tender', args=[self.st1.pk]), {
            'tender': self.upi.pk, 'active': 'on', 'sequence': 10, 'allow_refund': 'on', 'allow_split_payment': 'on'})
        self.assertRedirects(response, reverse('retail_store_card', args=[self.st1.pk]))
        self.assertTrue(StoreTender.objects.filter(store=self.st1, tender=self.upi).exists())
        again = self.client.post(reverse('retail_store_add_tender', args=[self.st1.pk]), {'tender': self.upi.pk, 'active': 'on', 'sequence': 20})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(StoreTender.objects.filter(store=self.st1, tender=self.upi).count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StoreTender.objects.create(store=self.st1, tender=self.upi)

    def test_inactive_tender_cannot_be_assigned(self):
        self.upi.status = 'inactive'
        self.upi.save()
        with self.assertRaises(ValidationError):
            StoreTender.objects.create(store=self.st1, tender=self.upi)

    def test_one_default_tender_per_store(self):
        services.assign_store_tender(StoreTender(store=self.st1, tender=self.cash, is_default=True))
        services.assign_store_tender(StoreTender(store=self.st1, tender=self.upi, is_default=True))
        self.assertEqual(list(StoreTender.objects.filter(store=self.st1, is_default=True).values_list('tender__code', flat=True)), ['UPI'])

    def test_09_add_staff_from_store_card_prefills_store_and_location(self):
        page = self.client.get(reverse('retail_store_add_staff', args=[self.st1.pk]))
        self.assertEqual(page.context['form'].initial['store'], self.st1.pk)
        self.assertEqual(page.context['form'].initial['location'], self.mumbai.pk)
        self.assertTrue(page.context['form'].fields['store'].disabled)
        response = self.client.post(reverse('retail_store_add_staff', args=[self.st1.pk]), {
            'employee_code': 'EMP050', 'name': 'Neha', 'staff_role': self.cashier.pk, 'pos_access': 'on', 'is_active': 'on',
            'login_id': 'Neha.POS', 'password': 'neha-pass', 'confirm_password': 'neha-pass',
            # A tampered store value is ignored: the store comes from the card.
            'store': self.st2.pk})
        neha = POSStaff.objects.get(employee_code='EMP050')
        self.assertRedirects(response, reverse('retail_staff_card', args=[neha.pk]))
        self.assertEqual(neha.store, self.st1)
        self.assertEqual(neha.location, self.mumbai)
        self.assertEqual(neha.login_id, 'neha.pos')
        self.assertTrue(check_password('neha-pass', neha.pin_hash))
        self.assertNotIn('neha-pass', neha.pin_hash)

    def test_10_add_terminal_from_store_card(self):
        page = self.client.get(reverse('retail_store_add_terminal', args=[self.st1.pk]))
        self.assertContains(page, 'Mumbai')
        self.client.post(reverse('retail_store_add_terminal', args=[self.st1.pk]), {
            'code': 'pos-05', 'name': 'Counter 5', 'terminal_type': 'counter', 'status': 'active', 'store': self.st2.pk})
        terminal = POSTerminal.objects.get(code='POS-05')
        self.assertEqual(terminal.store, self.st1)
        self.assertEqual(terminal.location, self.mumbai)

    def test_11_add_tender_from_store_card_uses_that_store(self):
        page = self.client.get(reverse('retail_store_add_tender', args=[self.st1.pk]))
        self.assertEqual(page.context['form'].store, self.st1)

    def test_staff_location_and_store_must_agree(self):
        response = self.client.post(reverse('retail_staff_new'), {
            'employee_code': 'EMP070', 'name': 'Amit', 'staff_role': self.cashier.pk, 'location': self.delhi.pk,
            'store': self.st1.pk, 'is_active': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(POSStaff.objects.filter(employee_code='EMP070').exists())
        # Location alone resolves its store.
        self.client.post(reverse('retail_staff_new'), {
            'employee_code': 'EMP071', 'name': 'Amit', 'staff_role': self.cashier.pk, 'location': self.delhi.pk, 'is_active': 'on'})
        self.assertEqual(POSStaff.objects.get(employee_code='EMP071').store, self.st2)

    def test_login_id_unique_case_insensitive(self):
        with self.assertRaises(ValidationError):
            self.make_staff('EMP080', 'Clone', 'RAHUL.POS', self.st2)

    def test_assignment_must_be_same_store(self):
        with self.assertRaises(ValidationError):
            POSStaffAssignment.objects.create(staff=self.rahul, terminal=self.delhi_pos, role='cashier')

    def test_16_store_change_blocked_by_open_session_and_pending_bills(self):
        self.assertRedirects(self.pos_login(self.pos1), reverse('pos'))
        self.rahul.store = self.st2
        with self.assertRaisesMessage(ValidationError, 'open POS session'):
            services.save_staff(self.rahul)
        self.client.get(reverse('pos_logout'))
        self.rahul.refresh_from_db()
        category = ItemCategory.objects.create(name='Rings')
        product = Product.objects.create(item_category=category, sku='R1', name='Ring', sale_price=Decimal('100'), mrp=Decimal('100'))
        from erp.models import Customer
        SalesInvoice.objects.create(invoice_no='PB-1', customer=Customer.objects.create(name='C'), cashier_staff=self.rahul,
                                    status='pending_approval')
        self.rahul.store = self.st2
        with self.assertRaisesMessage(ValidationError, 'pending POS bill'):
            services.save_staff(self.rahul)
        self.assertTrue(product.pk)

    def test_17_store_location_change_must_be_free(self):
        response = self.client.post(reverse('retail_store_edit', args=[self.st2.pk]), {
            'company': self.company.pk, 'code': 'ST002', 'name': 'Delhi Store', 'store_type': 'showroom', 'status': 'active',
            'location': self.mumbai.pk, 'country': 'India'})
        self.assertEqual(response.status_code, 200)
        self.st2.refresh_from_db()
        self.assertEqual(self.st2.location, self.delhi)
        self.client.post(reverse('retail_store_edit', args=[self.st2.pk]), {
            'company': self.company.pk, 'code': 'ST002', 'name': 'Delhi Store', 'store_type': 'showroom', 'status': 'active',
            'location': self.spare.pk, 'country': 'India'})
        self.st2.refresh_from_db()
        self.assertEqual(self.st2.location, self.spare)

    def test_non_admin_cannot_change_setup(self):
        self.client.force_login(User.objects.create_user('clerk', password='pw'))
        self.assertEqual(self.client.get(reverse('retail_stores')).status_code, 200)
        response = self.client.post(reverse('retail_store_add_tender', args=[self.st1.pk]), {'tender': self.upi.pk, 'active': 'on'})
        self.assertEqual(response.status_code, 403)


class POSLoginTests(RetailFixture):
    def test_12_login_opens_session_with_snapshots(self):
        response = self.pos_login(self.pos1)
        self.assertRedirects(response, reverse('pos'))
        session = POSSession.objects.get(staff=self.rahul, status='active')
        self.assertEqual((session.store_code_snapshot, session.location_code_snapshot, session.terminal_code_snapshot),
                         ('ST001', 'L001', 'POS-01'))
        self.assertEqual(session.staff_name_snapshot, 'Rahul Sharma')
        self.assertTrue(session.session_no.startswith('POSSES-'))
        self.assertTrue(POSShift.objects.filter(terminal=self.pos1, status='open').exists())
        page = self.client.get(reverse('pos'))
        self.assertContains(page, 'Mumbai Flagship')
        self.assertContains(page, 'POS-01')

    def test_13_login_rejected_on_another_stores_terminal(self):
        self.pos_login(self.delhi_pos)
        self.assertFalse(POSSession.objects.exists())
        response = self.client.post(reverse('api_retail_staff_login'), {'login_id': 'rahul.pos', 'password': self.password,
                                                                        'terminal_id': self.delhi_pos.pk}, content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['detail'], 'Staff is not authorized for this Store.')

    def test_14_deactivated_staff_loses_access_immediately(self):
        self.pos_login(self.pos1)
        services.set_staff_active(self.rahul, False)
        self.assertEqual(POSSession.objects.get(staff=self.rahul).status, 'force_closed')
        response = self.client.get(reverse('pos'))
        self.assertTemplateUsed(response, 'erp/pos_login.html')
        self.pos_login(self.pos1)
        self.assertFalse(POSSession.objects.filter(status='active').exists())

    def test_15_deactivated_terminal_blocks_everyone(self):
        manager = self.make_staff('EMP090', 'Priya', 'priya.pos', self.st1, role=self.manager_role)
        services.set_terminal_status(self.pos1, 'inactive')
        self.pos_login(self.pos1)
        self.pos_login(self.pos1, 'priya.pos')
        self.assertFalse(POSSession.objects.filter(status='active').exists())
        self.assertTrue(manager.pk)

    def test_session_is_revalidated_each_request(self):
        self.pos_login(self.pos1)
        self.st1.status = 'inactive'
        self.st1.save()
        response = self.client.get(reverse('pos'))
        self.assertRedirects(response, reverse('pos_login'))
        self.assertEqual(POSSession.objects.get().status, 'force_closed')

    def test_account_locks_after_five_failures_and_admin_unlocks(self):
        for _ in range(5):
            self.pos_login(self.pos1, password='wrong')
        self.rahul.refresh_from_db()
        self.assertTrue(self.rahul.is_blocked)
        response = self.pos_login(self.pos1)  # correct password, still locked
        self.assertContains(response, 'Account locked. Please contact Store Manager/Admin.')
        self.client.post(reverse('retail_staff_card', args=[self.rahul.pk]), {'action': 'unlock'})
        self.assertRedirects(self.pos_login(self.pos1), reverse('pos'))
        self.assertTrue(AuditLog.objects.filter(action='lock', record_id=str(self.rahul.pk)).exists())
        self.assertTrue(AuditLog.objects.filter(action='unlock', record_id=str(self.rahul.pk)).exists())

    def test_pos_disabled_and_role_without_pos_access_rejected(self):
        auditor = self.make_staff('EMP091', 'Audit', 'audit.pos', self.st1, role=POSRole.objects.get(code='AUDITOR'))
        self.pos_login(self.pos1, 'audit.pos')
        self.rahul.pos_access = False
        self.rahul.save()
        self.pos_login(self.pos1)
        self.assertFalse(POSSession.objects.exists())
        self.assertTrue(auditor.pk)

    def test_terminal_assignment_restricts_login(self):
        POSStaffAssignment.objects.create(staff=self.rahul, terminal=self.pos2, role='cashier')
        self.pos_login(self.pos1)
        self.assertFalse(POSSession.objects.exists())
        self.assertRedirects(self.pos_login(self.pos2), reverse('pos'))

    def test_store_without_location_blocks_login(self):
        store = Store.objects.create(company=self.company, code='ST050', name='Legacy')
        terminal = POSTerminal.objects.create(store=store, code='L-1', name='Legacy till')
        POSStaff.objects.create(employee_code='LEG', name='Legacy', store=store, pin_hash='x')
        response = self.pos_login(terminal, 'LEG')
        self.assertContains(response, 'has no Location assigned')

    def test_busy_terminal_rejects_second_staff(self):
        self.make_staff('EMP092', 'Amit', 'amit.pos', self.st1)
        self.pos_login(self.pos1)
        other = self.client_class()
        other.force_login(self.admin)
        response = other.post(reverse('pos_login'), {'staff_id': 'amit.pos', 'pin': self.password, 'terminal_id': self.pos1.pk})
        self.assertContains(response, 'is in use by Rahul Sharma')

    def test_default_terminal_used_when_device_not_bound(self):
        self.rahul.default_terminal = self.pos2
        self.rahul.save()
        self.client.post(reverse('pos_login'), {'staff_id': 'rahul.pos', 'pin': self.password})
        self.assertEqual(POSSession.objects.get().terminal, self.pos2)

    def test_logout_closes_session_with_summary(self):
        self.pos_login(self.pos1)
        self.client.get(reverse('pos_logout'))
        session = POSSession.objects.get()
        self.assertEqual(session.status, 'logged_out')
        self.assertIsNotNone(session.logout_time)


class POSSaleTenderTests(RetailFixture):
    def setUp(self):
        super().setUp()
        self.st_cash = services.assign_store_tender(StoreTender(store=self.st1, tender=self.cash, is_default=True, allow_change=True))
        self.st_upi = services.assign_store_tender(StoreTender(store=self.st1, tender=self.upi))
        category = ItemCategory.objects.create(name='Chains')
        self.product = Product.objects.create(item_category=category, sku='CH-1', name='Chain', sale_price=Decimal('100.00'),
                                              mrp=Decimal('100.00'), barcode='CH-1')
        self.pos_login(self.pos1)

    def sell(self, **extra):
        data = {'item_id': self.product.pk, 'barcode': self.product.barcode, 'quantity': '1', 'gst_rate': '3', 'place_of_supply': '27'}
        data.update(extra)
        return self.client.post(reverse('pos'), data)

    def test_split_tender_with_cash_change_and_snapshots(self):
        response = self.sell(tender_store_tender=[self.st_upi.pk, self.st_cash.pk], tender_amount=['50', '60'],
                             tender_reference=['UPI-123', ''])
        invoice = SalesInvoice.objects.get()
        self.assertRedirects(response, f'{reverse("pos")}?completed={invoice.pk}')
        payments = {p.tender_code_snapshot: p for p in POSPayment.objects.filter(invoice=invoice)}
        self.assertEqual(payments['UPI'].reference, 'UPI-123')
        self.assertEqual(payments['CASH'].change_amount, Decimal('7.00'))  # 110 tendered for 103.00
        self.assertEqual((invoice.store_name_snapshot, invoice.location_code_snapshot, invoice.terminal_name_snapshot,
                          invoice.cashier_code_snapshot), ('Mumbai Flagship', 'L001', 'Counter 1', 'EMP001'))
        # 38: history survives Rahul moving store.
        self.client.get(reverse('pos_logout'))
        SalesInvoice.objects.filter(pk=invoice.pk).update(status='completed')
        self.rahul.refresh_from_db()
        self.rahul.store = self.st2
        services.save_staff(self.rahul)
        invoice.refresh_from_db()
        self.assertEqual(invoice.store_name_snapshot, 'Mumbai Flagship')
        session = POSSession.objects.get()
        self.assertEqual(session.sales_count, 1)
        self.assertEqual(session.cash_collected, Decimal('53.00'))

    def test_reference_required_and_unassigned_tender_rejected(self):
        response = self.sell(tender_store_tender=[self.st_upi.pk], tender_amount=['103'], tender_reference=[''])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SalesInvoice.objects.exists())
        delhi_cash = services.assign_store_tender(StoreTender(store=self.st2, tender=self.cash))
        response = self.sell(tender_store_tender=[delhi_cash.pk], tender_amount=['103'], tender_reference=[''])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_overpayment_needs_change_giving_tender(self):
        response = self.sell(tender_store_tender=[self.st_upi.pk], tender_amount=['200'], tender_reference=['x'])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_cashier_discount_needs_manager_approval(self):
        response = self.sell(discount_amount='5')
        self.assertEqual(response.status_code, 400)
        self.make_staff('EMP095', 'Manager', 'mgr.pos', self.st1, role=self.manager_role)
        response = self.sell(discount_amount='5', approver_login='mgr.pos', approver_password=self.password)
        self.assertEqual(response.status_code, 302)
        self.assertIn('Discount approved by EMP095', SalesInvoice.objects.get().notes)

    def test_shift_close_computes_cash_difference(self):
        self.sell(tender_store_tender=[self.st_cash.pk], tender_amount=['103'], tender_reference=[''])
        shift = POSShift.objects.get()
        shift.opening_cash = Decimal('500')
        shift.save()
        services.close_shift(shift, self.rahul, Decimal('600'))
        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash, Decimal('603.00'))
        self.assertEqual(shift.cash_difference, Decimal('-3.00'))
        self.assertEqual(shift.status, 'closed')
        self.assertFalse(POSSession.objects.filter(status='active').exists())


class ImportExportTests(RetailFixture):
    def workbook(self, kind, rows):
        wb = Workbook()
        ws = wb.active
        ws.append([c for c, _ in imports.COLUMNS[kind]])
        for row in rows:
            ws.append(row)
        out = BytesIO()
        wb.save(out)
        out.seek(0)
        out.name = f'{kind}.xlsx'
        return out

    def test_staff_import_validates_previews_then_imports(self):
        good = ['EMP200', 'Kiran', '', '', '', '', '', 'CASHIER', 'GOLDIO', 'ST001', 'L001', 'kiran.pos', 'kiran-pass', 'Yes', 'Yes', '']
        bad_location = ['EMP201', 'Mismatch', '', '', '', '', '', 'CASHIER', 'GOLDIO', 'ST001', 'L002', 'mis.pos', 'mis-pass1', 'Yes', 'Yes', '']
        short_pw = ['EMP202', 'Short', '', '', '', '', '', 'CASHIER', 'GOLDIO', 'ST001', '', 'short.pos', '123', 'Yes', 'Yes', '']
        response = self.client.post(reverse('retail_imports'), {'import_type': 'staff', 'file': self.workbook('staff', [good, bad_location, short_pw])})
        batch = RetailImportBatch.objects.get()
        self.assertRedirects(response, reverse('retail_import_detail', args=[batch.pk]))
        self.assertEqual(batch.status, 'failed')
        self.assertEqual({e['row'] for e in batch.errors}, {3, 4})
        self.assertNotIn('kiran-pass', str(batch.rows))  # never staged in plain text
        self.assertFalse(POSStaff.objects.filter(employee_code='EMP200').exists())  # nothing written on validation
        self.client.post(reverse('retail_import_detail', args=[batch.pk]), {'action': 'import'})
        self.assertFalse(POSStaff.objects.filter(employee_code='EMP200').exists())  # refused while errors remain

        self.client.post(reverse('retail_imports'), {'import_type': 'staff', 'file': self.workbook('staff', [good])})
        fixed = RetailImportBatch.objects.order_by('-pk').first()
        self.assertEqual(fixed.status, 'validated')
        self.client.post(reverse('retail_import_detail', args=[fixed.pk]), {'action': 'import'})
        fixed.refresh_from_db()
        self.assertEqual(fixed.status, 'imported')
        kiran = POSStaff.objects.get(employee_code='EMP200')
        self.assertEqual(kiran.store, self.st1)
        self.assertTrue(check_password('kiran-pass', kiran.pin_hash))
        self.assertNotIn('hashed$', str(fixed.rows))

    def test_store_tender_import_rejects_duplicate_rows_in_file(self):
        rows = [['GOLDIO', 'ST001', 'UPI', 'Yes', 'No', 10, 'Yes', 'Yes', 'No', 'Yes'],
                ['GOLDIO', 'ST001', 'CASH', 'Yes', 'Yes', 20, 'No', 'Yes', 'Yes', 'Yes']]
        self.client.post(reverse('retail_imports'), {'import_type': 'store-tenders', 'file': self.workbook('store-tenders', rows)})
        batch = RetailImportBatch.objects.get()
        self.assertEqual(batch.status, 'validated', batch.errors)
        self.client.post(reverse('retail_import_detail', args=[batch.pk]), {'action': 'import'})
        self.assertEqual(StoreTender.objects.filter(store=self.st1).count(), 2)
        self.assertTrue(StoreTender.objects.get(store=self.st1, tender=self.cash).is_default)

    def test_exports_never_contain_password_hash(self):
        response = self.client.get(reverse('retail_staff') + '?export=1')
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(response.content))
        values = [str(c.value) for row in wb.active.iter_rows() for c in row]
        self.assertIn('rahul.pos', values)
        self.assertFalse(any(v.startswith('pbkdf2') or 'argon' in v for v in values))
        self.assertTrue(AuditLog.objects.filter(action='export').exists())


class APITests(RetailFixture):
    def test_read_endpoints_hide_passwords(self):
        for name, args in [('api_retail_locations', []), ('api_retail_stores', []), ('api_retail_store', [self.st1.pk]),
                           ('api_retail_store_staff', [self.st1.pk]), ('api_retail_store_terminals', [self.st1.pk]),
                           ('api_retail_store_tenders', [self.st1.pk]), ('api_retail_staff', []),
                           ('api_retail_staff_detail', [self.rahul.pk]), ('api_retail_terminals', []),
                           ('api_retail_terminal', [self.pos1.pk]), ('api_retail_tenders', [])]:
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 200, name)
            self.assertNotIn('pin_hash', response.content.decode())
            self.assertNotIn('pbkdf2', response.content.decode())

    def test_login_api_returns_session_payload(self):
        response = self.client.post(reverse('api_retail_staff_login'), {'login_id': 'rahul.pos', 'password': self.password,
                                                                        'terminal_id': self.pos1.pk}, content_type='application/json')
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual((body['store_code'], body['location_code'], body['terminal_code']), ('ST001', 'L001', 'POS-01'))
        self.assertIn('pos.sale.create', body['permissions'])
        self.assertNotIn('password', body)

    def test_create_staff_via_store_api(self):
        response = self.client.post(reverse('api_retail_store_staff', args=[self.st1.pk]), {
            'employee_code': 'EMP300', 'name': 'API Staff', 'staff_role': self.cashier.pk, 'pos_access': True, 'is_active': True,
            'login_id': 'api.pos', 'password': 'api-pass-1', 'confirm_password': 'api-pass-1'}, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['location_code'], 'L001')
        dup = self.client.post(reverse('api_retail_store_tenders', args=[self.st1.pk]), {'tender': self.cash.pk, 'active': True},
                               content_type='application/json')
        self.assertEqual(dup.status_code, 201)
        again = self.client.post(reverse('api_retail_store_tenders', args=[self.st1.pk]), {'tender': self.cash.pk, 'active': True},
                                 content_type='application/json')
        self.assertEqual(again.status_code, 400)


class PageSmokeTests(RetailFixture):
    def test_all_pages_render(self):
        services.assign_store_tender(StoreTender(store=self.st1, tender=self.cash))
        self.pos_login(self.pos1)
        assignment = POSStaffAssignment.objects.create(staff=self.rahul, terminal=self.pos1, role='cashier')
        shift = POSShift.objects.get()
        names = [('retail_home', []), ('retail_locations', []), ('retail_location_new', []), ('retail_location_card', [self.mumbai.pk]),
                 ('retail_stores', []), ('retail_store_new', []), ('retail_store_card', [self.st1.pk]), ('retail_store_edit', [self.st1.pk]),
                 ('retail_store_add_staff', [self.st1.pk]), ('retail_store_add_terminal', [self.st1.pk]),
                 ('retail_store_add_tender', [self.st1.pk]), ('retail_staff', []), ('retail_staff_new', []),
                 ('retail_staff_card', [self.rahul.pk]), ('retail_staff_edit', [self.rahul.pk]),
                 ('retail_staff_reset_password', [self.rahul.pk]), ('retail_assignment_new', [self.rahul.pk]),
                 ('retail_assignments', []), ('retail_assignment_edit', [assignment.pk]), ('retail_terminals', []),
                 ('retail_terminal_new', []), ('retail_terminal_card', [self.pos1.pk]), ('retail_terminal_edit', [self.pos1.pk]),
                 ('retail_tenders', []), ('retail_tender_new', []), ('retail_tender_card', [self.cash.pk]),
                 ('retail_store_tenders', []), ('retail_roles', []), ('retail_role_new', []), ('retail_role_card', [self.cashier.pk]),
                 ('retail_sessions', []), ('retail_shifts', []), ('retail_shift_detail', [shift.pk]), ('retail_imports', []),
                 ('retail_audit', []), ('pos', [])]
        for name, args in names:
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 200, name)
        for kind, _ in RetailImportBatch.TYPES:
            self.assertEqual(self.client.get(reverse('retail_import_template', args=[kind])).status_code, 200)
            self.assertEqual(self.client.get(reverse('retail_export', args=[kind])).status_code, 200)

    def test_reset_password_is_audited_without_password(self):
        self.client.post(reverse('retail_staff_reset_password', args=[self.rahul.pk]),
                         {'password': 'brand-new-1', 'confirm_password': 'brand-new-1', 'require_change': 'on'})
        self.rahul.refresh_from_db()
        self.assertTrue(check_password('brand-new-1', self.rahul.pin_hash))
        log = AuditLog.objects.get(action='password_reset')
        self.assertNotIn('brand-new-1', str(log.old_value) + str(log.new_value) + log.description)
        self.assertTrue(self.rahul.password_change_required)
        self.assertLess(timezone.now() - self.rahul.password_changed_at, timedelta(minutes=1))
