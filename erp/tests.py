from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth.models import User
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone as dj_timezone
from django.urls import reverse
from zipfile import ZipFile

from erp.models import (
    BankAccount, BankTransaction, BinLocation, Brand, BusinessUnit, Channel, Company,
    Barcode, Category, CostCenter, Customer, CustomerFinanceProfile, CustomerPostingGroup, Department,
    DimensionSet, DocumentRelationship, DocumentStatusHistory, Employee, FinanceJournalBatch,
    FinanceJournalTemplate, FinancePostedVoucher, FinanceVoucher, FinanceVoucherType,
    GSTRegistration,
    Division, GSTRate, GSTState, GSTSlab,
    GeneralLedger, GLAccount, InventoryMovement, ItemBarcode, ItemBatch,
    ItemCategory, ItemPrice, ItemSerial, JournalEntry, JournalEntryLine,
    Payment, Product, Role, SalesInvoice, StockLedger, Supplier,
    Item, ItemUnitOfMeasure, ItemVariant, Location, SKU, SpecialGroup, Store, SubCategory,
    Supplier, SupplierInvoice, UnitOfMeasure, UserProfile, VendorFinanceProfile, VendorPostingGroup,
    Warehouse, WarehouseUserAssignment, WarehouseZone, PaymentMethod, PaymentTerm,
    POSStaff, POSTerminal, POSStaffAssignment, POSSession, POSShift,
    JewelleryMetalRate, JewelleryPricingRule, JewelleryItemUnit, JewelleryStone,
    CustomerAddress, CustomerShippingAddress, CustomerCommunicationPreference,
    InvoicePrintLayout, StoreInvoicePrintSetup, InvoicePrintLog,
    Karigar, RepairOrder, CustomerOrnament, RepairCustodyEvent, RepairQC,
    PaymentReceipt, Quotation, SalesOrder,
    GoodsReceipt, PurchaseOrder, VendorPayment, FinancePostingSetup, FinanceVoucher,
)
from erp.services import (
    DOCUMENT_TYPE_PREFIXES, approve_finance_voucher, approve_purchase_order, approve_quotation,
    approve_sales_order, calculate_gst, calculate_jewellery_price, convert_quotation_to_sales_order,
    convert_receipt_to_purchase_invoice, convert_sales_order_to_invoice, create_finance_voucher,
    create_payment_receipt, create_purchase_order, create_quotation, create_vendor_payment,
    get_next_customer_number, get_next_number, get_related_documents, post_finance_voucher,
    post_payment_receipt, post_purchase_invoice, post_sales_invoice, post_vendor_payment,
    receive_goods, submit_for_approval,
    ManualPricingRule, ManualUnit, ProductWeightAsUnit, ResolvedRate, resolve_current_metal_rate,
)


class AuthenticationViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='login-user', password='test-pass')

    def test_login_accepts_valid_credentials(self):
        response = self.client.post(reverse('login'), {
            'username': 'login-user',
            'password': 'test-pass',
        })

        self.assertRedirects(response, reverse('dashboard'))
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_login_rejects_invalid_credentials(self):
        response = self.client.post(reverse('login'), {
            'username': 'login-user',
            'password': 'wrong-pass',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Invalid username or password.')


class CustomerMasterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='customer-admin', password='test-pass')
        self.client.force_login(self.user)

    def test_customer_create_generates_number_and_detail_loads_related_profiles(self):
        response = self.client.post(reverse('customer_create'), {
            'name': 'Naresh Kumar', 'phone': '9876543210', 'email': 'naresh@example.com',
            'customer_type': 'retail', 'customer_status': 'active', 'gst_customer_type': 'unregistered',
            'gst_registration_type': 'GSTIN', 'is_active': 'on',
        })

        customer = Customer.objects.get(phone='9876543210')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(customer.customer_no.startswith('CUS-'))
        CustomerAddress.objects.create(customer=customer, city='Gurugram', state='Haryana', pin_code='122002')
        CustomerShippingAddress.objects.create(customer=customer, address_code='HOME', address_name='Home', city='Gurugram', is_default=True)
        CustomerCommunicationPreference.objects.create(customer=customer, promotional_consent=False)
        detail = self.client.get(reverse('customer_detail', kwargs={'pk': customer.pk}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, customer.customer_no)
        self.assertContains(detail, 'Gurugram')

    def test_duplicate_mobile_is_rejected(self):
        Customer.objects.create(customer_no='CUS-000001', name='Existing', phone='9999999999')
        response = self.client.post(reverse('customer_create'), {
            'name': 'Duplicate', 'phone': '9999999999', 'customer_type': 'retail',
            'customer_status': 'active', 'gst_customer_type': 'unregistered',
            'gst_registration_type': 'GSTIN', 'is_active': 'on',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already uses this mobile number')


class RepairWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='repair-user', password='test-pass')
        company = Company.objects.create(company_code='REP-CO', company_name='Repair Company')
        self.store = Store.objects.create(company=company, code='REP', name='Repair Store')
        self.staff = POSStaff.objects.create(employee_code='REP-STAFF', name='Repair Staff', store=self.store, pin_hash=make_password('1234'))
        self.karigar = Karigar.objects.create(code='KAR-001', name='Master Karigar', store=self.store)
        self.customer = Customer.objects.create(customer_no='CUS-REP-001', name='Repair Customer', phone='9876543210')
        self.client.force_login(self.user)

    def test_customer_ornament_custody_workflow_and_payment_gate(self):
        response = self.client.post(reverse('repair_order_create'), {
            'customer': self.customer.pk, 'repair_type': 'Polishing',
            'repair_description': 'Polish ring and tighten setting', 'priority': 'normal',
            'description': 'Gold ring', 'metal_type': 'Gold', 'purity': '22K',
            'gross_weight': '8.420', 'stone_weight': '0.250', 'other_weight': '0',
            'condition_at_receipt': 'Minor scratches', 'customer_declared_value': '50000',
        })
        order = RepairOrder.objects.get(customer=self.customer)
        self.assertRedirects(response, reverse('repair_order_detail', kwargs={'pk': order.pk}))
        self.assertEqual(order.ornament.custody_status, 'at_store')
        self.assertEqual(order.ornament.condition_at_receipt, 'Minor scratches')

        self.client.get(reverse('repair_order_accept', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')
        self.client.get(reverse('repair_order_issue', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.ornament.current_location, 'KARIGAR:KAR-001')
        self.client.get(reverse('repair_order_receive', kwargs={'pk': order.pk}))
        self.client.get(reverse('repair_order_qc', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'qc_passed')

        blocked = self.client.get(reverse('repair_order_return', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'qc_passed')
        order.status = 'paid'
        order.save(update_fields=['status'])
        self.client.get(reverse('repair_order_return', kwargs={'pk': order.pk}))
        order.refresh_from_db()
        self.assertEqual(order.status, 'closed')
        self.assertEqual(order.ornament.custody_status, 'returned')
        self.assertEqual(RepairCustodyEvent.objects.filter(repair_order=order).count(), 4)


class POSCompletionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pos-cashier', password='test-pass')
        company = Company.objects.create(company_code='POS-SALE', company_name='POS Sales Company')
        store = Store.objects.create(company=company, code='SALE', name='Sales Store')
        terminal = POSTerminal.objects.create(store=store, code='POS-SALE-01', name='Sales Register')
        staff = POSStaff.objects.create(
            employee_code='EMP-SALE', name='POS Cashier', role='cashier', store=store,
            pin_hash=make_password('1234'),
        )
        POSStaffAssignment.objects.create(staff=staff, terminal=terminal, role='cashier')
        category = ItemCategory.objects.create(name='POS Test')
        self.product = Product.objects.create(
            item_category=category,
            sku='POS-SKU-001',
            name='POS Test Product',
            sale_price=Decimal('100.00'),
            mrp=Decimal('100.00'),
            barcode='POS-100',
        )
        self.client.force_login(self.user)
        self.client.post(reverse('pos_login'), {'staff_id': 'EMP-SALE', 'pin': '1234'})

    def test_complete_sale_posts_and_returns_to_fresh_pos(self):
        response = self.client.post(reverse('pos'), {
            'item_id': self.product.pk,
            'barcode': self.product.barcode,
            'quantity': '1',
            'sales_staff': '',
            'gst_rate': '3',
            'place_of_supply': '27',
        })

        invoice = SalesInvoice.objects.get(customer__name='Walk-in Customer')
        self.assertRedirects(response, f'{reverse("pos")}?completed={invoice.pk}')
        self.assertEqual(invoice.total_amount, Decimal('103.00'))

        receipt = self.client.get(reverse('invoice_pdf', kwargs={'pk': invoice.pk}))
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt['Content-Type'], 'application/pdf')
        self.assertTrue(receipt['Content-Disposition'].startswith('inline;'))

        StoreInvoicePrintSetup.objects.create(store=invoice.pos_terminal.store, invoice_print_type='A5_INVOICE')
        a5 = self.client.get(reverse('invoice_pdf', kwargs={'pk': invoice.pk}))
        self.assertEqual(a5.status_code, 200)
        self.assertEqual(a5['Content-Type'], 'application/pdf')
        thermal = self.client.get(reverse('invoice_pdf', kwargs={'pk': invoice.pk}) + '?format=THERMAL_40COL_RECEIPT')
        self.assertEqual(thermal['Content-Type'], 'text/plain; charset=utf-8')
        self.assertIn(invoice.invoice_no, thermal.content.decode())
        self.assertEqual(InvoicePrintLog.objects.filter(invoice=invoice).count(), 3)


class POSStaffSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pos-user', password='test-pass')
        self.company = Company.objects.create(company_code='POS-CO', company_name='POS Company')
        self.store = Store.objects.create(company=self.company, code='DKG', name='DKG Store')
        self.terminal = POSTerminal.objects.create(store=self.store, code='POS-003', name='Register 3')
        self.staff = POSStaff.objects.create(
            employee_code='EMP1024', name='Amit Kumar', role='cashier', store=self.store,
            pin_hash=make_password('1234'),
        )
        self.client.force_login(self.user)

    def test_pos_requires_staff_assignment(self):
        response = self.client.get(reverse('pos'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'erp/pos_login.html')
        self.assertFalse(POSSession.objects.exists())

    def test_assigned_staff_pin_opens_pos_session_and_shift(self):
        POSStaffAssignment.objects.create(staff=self.staff, terminal=self.terminal, role='cashier')

        response = self.client.post(reverse('pos_login'), {'staff_id': 'EMP1024', 'pin': '1234'})

        self.assertRedirects(response, reverse('pos'))
        self.assertTrue(POSSession.objects.filter(staff=self.staff, status='active').exists())
        self.assertTrue(POSShift.objects.filter(terminal=self.terminal, status='open').exists())


class MasterManagementViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='admin', password='admin123')
        self.category = ItemCategory.objects.create(name='Necklace', description='Gold jewelry')
        self.customer = Customer.objects.create(name='Test Customer', phone='9999999999', gstin='27ABCDE1234F1Z5')
        self.role = Role.objects.create(name='Store Manager', permissions={'inventory': True, 'billing': True})
        self.employee = Employee.objects.create(
            employee_code='EMP-001',
            full_name='Amit Shah',
            phone='9898989898',
            email='amit@example.com',
            department='Sales',
            designation='Manager',
            role=self.role,
        )
        self.user_profile = UserProfile.objects.create(user=self.user, employee=self.employee, role=self.role)
        self.product = Product.objects.create(
            item_category=self.category,
            sku='SKU-001',
            name='22K Gold Necklace',
            metal_type='gold',
            purity='22K',
            weight_grams=Decimal('12.500'),
            purchase_price=Decimal('9000.00'),
            sale_price=Decimal('12000.00'),
            mrp=Decimal('13500.00'),
            barcode='8901234567890',
            hsn_code='7113',
            stock_quantity=Decimal('5.000'),
        )
        ItemPrice.objects.create(item=self.product, price_type='sale', amount=Decimal('12000.00'))
        ItemBarcode.objects.create(item=self.product, barcode='BRC-001', barcode_type='EAN13', is_primary=True)
        batch = ItemBatch.objects.create(item=self.product, batch_no='BATCH-001', quantity=Decimal('2.000'))
        ItemSerial.objects.create(item=self.product, batch=batch, serial_no='SER-001', status='available')
        GSTSlab.objects.create(name='Jewellery GST', gst_rate=Decimal('3.00'), hsn_code='7113')
        Warehouse.objects.create(code='WH-01', name='Main Warehouse', city='Mumbai')

    def test_master_management_page_loads(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('master_management'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Master Management')
        self.assertContains(response, '22K Gold Necklace')

    def test_warehouse_and_gst_pages_load(self):
        self.client.force_login(self.user)
        warehouse_response = self.client.get(reverse('warehouse_management'))
        gst_response = self.client.get(reverse('gst_management'))

        self.assertEqual(warehouse_response.status_code, 200)
        self.assertContains(warehouse_response, 'Warehouse Management')
        self.assertEqual(gst_response.status_code, 200)
        self.assertContains(gst_response, 'GST Management')


class NumberSeriesServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            company_code='GOLDI-NO',
            company_name='Goldi Numbering',
            status='active',
        )

    def test_each_document_type_has_a_sequential_number(self):
        document_types = (
            'customer', 'vendor', 'gl_journal', 'sales_invoice',
            'sales_credit_memo', 'purchase_invoice', 'purchase_credit_memo',
            'payment', 'receipt', 'fixed_asset', 'gst_invoice', 'e_invoice',
            'e_way_bill',
        )

        for document_type in document_types:
            first = get_next_number(document_type, self.company)
            second = get_next_number(document_type, self.company)
            self.assertTrue(first.startswith(f'{DOCUMENT_TYPE_PREFIXES[document_type]}-'))
            self.assertEqual(int(second.rsplit('-', 1)[1]), int(first.rsplit('-', 1)[1]) + 1)

    def test_series_resets_by_fiscal_year_and_is_company_scoped(self):
        current_year_number = get_next_number('sales_invoice', self.company, date(2025, 4, 1))
        next_year_number = get_next_number('sales_invoice', self.company, date(2026, 4, 1))
        other_company = Company.objects.create(
            company_code='GOLDI-NO-2',
            company_name='Other Numbering Company',
            status='active',
        )
        other_company_number = get_next_number('sales_invoice', other_company, date(2025, 4, 1))

        self.assertTrue(current_year_number.endswith('-0001'))
        self.assertTrue(next_year_number.endswith('-0001'))
        self.assertTrue(other_company_number.endswith('-0001'))

    def test_unknown_document_type_is_rejected(self):
        with self.assertRaises(ValueError):
            get_next_number('unknown_document', self.company)

    def test_awms_document_series_are_available(self):
        self.assertEqual(get_next_number('transfer_order', self.company), 'TO-2026-0001')
        self.assertEqual(get_next_number('warehouse_receipt', self.company), 'WR-2026-0001')
        self.assertEqual(get_next_number('putaway', self.company), 'PA-2026-0001')
        self.assertEqual(get_next_number('pick', self.company), 'PK-2026-0001')


class AWMSPhase1Tests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            company_code='GOLDI-WMS',
            company_name='Goldi Warehouse',
            status='active',
        )
        self.user = User.objects.create_user(username='warehouse_user', password='test-pass')
        self.warehouse = Warehouse.objects.create(
            company=self.company,
            code='WH01',
            name='Mumbai Warehouse',
            directed_putaway=True,
            directed_pick=True,
            status='active',
        )

    def test_warehouse_zone_bin_hierarchy_and_controls(self):
        zone = WarehouseZone.objects.create(
            warehouse=self.warehouse,
            code='PICK',
            name='Fast Pick Zone',
            zone_type='fast_pick',
            priority=10,
            allow_pick=True,
            allow_putaway=False,
        )
        bin_location = BinLocation.objects.create(
            warehouse=self.warehouse,
            zone=zone,
            code='A01-R01-L01-P01',
            bin_type='pick',
            rank=1,
            capacity=100,
            capacity_unit='piece',
            allow_mixed_items=False,
        )

        self.assertEqual(bin_location.zone, zone)
        self.assertEqual(zone.warehouse, self.warehouse)
        self.assertEqual(bin_location.rank, 1)
        self.assertFalse(bin_location.allow_mixed_items)

    def test_worker_is_restricted_to_warehouse_operation(self):
        assignment = WarehouseUserAssignment.objects.create(
            user=self.user,
            warehouse=self.warehouse,
            operation='pick',
        )

        self.assertEqual(assignment.warehouse, self.warehouse)
        self.assertEqual(assignment.operation, 'pick')
        self.assertTrue(assignment.is_active)


class ERPPageAvailabilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='page_user', password='test-pass')
        self.client.force_login(self.user)

    def test_control_centre_pages_load(self):
        urls = (
            'finance_management', 'finance_operations', 'sales_receivables', 'purchase_payables',
            'gst_control_center', 'wms_hub', 'wms_reports', 'master_catalog',
            'wms_module',
        )
        for url_name in urls:
            kwargs = {'module': 'picks'} if url_name == 'wms_module' else {}
            response = self.client.get(reverse(url_name, kwargs=kwargs))
            self.assertEqual(response.status_code, 200, url_name)

    def test_master_crud_pages_load(self):
        response = self.client.get(reverse('master_crud', kwargs={'slug': 'warehouses'}))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse('master_create', kwargs={'slug': 'warehouses'}))
        self.assertEqual(response.status_code, 200)

    def test_import_center_loads(self):
        response = self.client.get(reverse('import_center'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Import center')

    def test_sales_register_has_filters_and_print_links(self):
        customer = Customer.objects.create(name='Register Customer', phone='9000000000')
        invoice = SalesInvoice.objects.create(
            invoice_no='REG-0001', customer=customer, subtotal=Decimal('100'),
            taxable_amount=Decimal('100'), gst_amount=Decimal('3'), total_amount=Decimal('103'),
            status='completed',
        )

        response = self.client.get(reverse('sales_receivables'), {'q': invoice.invoice_no, 'status': 'completed'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, invoice.invoice_no)
        self.assertContains(response, f'{reverse("invoice_pdf", kwargs={"pk": invoice.pk})}?format=A4_INVOICE')
        self.assertContains(response, f'{reverse("invoice_pdf", kwargs={"pk": invoice.pk})}?format=A5_INVOICE')
        self.assertContains(response, f'{reverse("invoice_pdf", kwargs={"pk": invoice.pk})}?format=THERMAL_40COL_RECEIPT')

    def test_master_template_download_returns_standard_workbook(self):
        response = self.client.get(reverse('download_template', kwargs={'slug': 'customers'}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('CUSTOMERS_IMPORT_TEMPLATE.xlsx', response['Content-Disposition'])
        with ZipFile(BytesIO(response.content)) as workbook:
            workbook_xml = workbook.read('xl/workbook.xml').decode()
            data_xml = workbook.read('xl/worksheets/sheet2.xml').decode()
        self.assertIn('01_Instructions', workbook_xml)
        self.assertIn('06_Errors', workbook_xml)
        self.assertIn('name *', data_xml)

    def test_item_template_download_is_available(self):
        response = self.client.get(reverse('download_template', kwargs={'slug': 'retail-items'}))

        self.assertEqual(response.status_code, 200)
        with ZipFile(BytesIO(response.content)) as workbook:
            self.assertIn('item_number *', workbook.read('xl/worksheets/sheet2.xml').decode())

    def test_upload_page_contains_file_picker_and_template_link(self):
        response = self.client.get(reverse('upload_import_file', kwargs={'slug': 'customers'}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'type="file"')
        self.assertContains(response, reverse('download_template', kwargs={'slug': 'customers'}))

    def test_upload_rejects_non_excel_file(self):
        response = self.client.post(
            reverse('upload_import_file', kwargs={'slug': 'customers'}),
            {'file': SimpleUploadedFile('customers.txt', b'not excel')},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Only .xlsx or .xlsm files are supported.')

    def test_master_search_filters_records(self):
        Warehouse.objects.create(code='WH-SEARCH', name='Search Warehouse', status='active')
        Warehouse.objects.create(code='WH-OTHER', name='Other Warehouse', status='active')

        response = self.client.get(reverse('master_crud', kwargs={'slug': 'warehouses'}), {'q': 'WH-SEARCH'})

        self.assertContains(response, 'WH-SEARCH')
        self.assertNotContains(response, 'WH-OTHER')

    def test_master_save_and_new_redirects_to_clean_entry(self):
        response = self.client.post(
            reverse('master_create', kwargs={'slug': 'warehouses'}),
            {
                'code': 'WH-NEW', 'name': 'New Warehouse', 'location_type': 'warehouse',
                'state_code': '27', 'country': 'IN', 'timezone': 'Asia/Kolkata',
                'status': 'active', 'save_and_new': '1',
            },
        )

        self.assertRedirects(response, reverse('master_create', kwargs={'slug': 'warehouses'}))
        self.assertTrue(Warehouse.objects.filter(code='WH-NEW').exists())


class GSTFoundationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            company_code='GOLDI-GST', company_name='Goldi GST Company', status='active',
        )
        self.state = GSTState.objects.create(
            state_code='27', state_name='Maharashtra', gst_state_code='27',
        )
        self.rate = GSTRate.objects.create(
            code='GST-18', description='Standard 18 percent', effective_from=date(2026, 4, 1),
            cgst_rate=Decimal('9'), sgst_rate=Decimal('9'), igst_rate=Decimal('18'),
        )

    def test_intrastate_calculation_uses_configured_rate(self):
        result = calculate_gst(amount=Decimal('1000'), rate_code='GST-18', interstate=False, transaction_date=date(2026, 5, 1))

        self.assertEqual(result['cgst'], Decimal('90.00'))
        self.assertEqual(result['sgst'], Decimal('90.00'))
        self.assertEqual(result['igst'], Decimal('0.00'))
        self.assertEqual(result['explanation']['component_rule'], 'CGST + SGST/UTGST')

    def test_interstate_calculation_uses_igst(self):
        result = calculate_gst(amount=Decimal('1000'), rate_code='GST-18', interstate=True, transaction_date=date(2026, 5, 1))

        self.assertEqual(result['igst'], Decimal('180.00'))
        self.assertEqual(result['cgst'], Decimal('0.00'))
        self.assertEqual(result['sgst'], Decimal('0.00'))

    def test_gstin_requires_fifteen_characters(self):
        registration = GSTRegistration(
            company=self.company, gstin='INVALID', legal_name='Invalid GST', state=self.state,
            effective_from=date(2026, 4, 1),
        )

        with self.assertRaises(ValidationError):
            registration.full_clean()


class JewelleryPricingEngineTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company_code='JWL-CO', company_name='Jewellery Company')
        self.store = Store.objects.create(company=self.company, code='JWL', name='Jewellery Store')
        self.category = ItemCategory.objects.create(name='Jewellery Engine')
        self.product = Product.objects.create(
            item_category=self.category, sku='JWL-001', name='Gold Ring', metal_type='gold', purity='22K',
        )
        self.unit = JewelleryItemUnit.objects.create(
            product=self.product, barcode='JWL-BAR-001', serial_number='JWL-SER-001',
            gross_weight=Decimal('8.250'), stone_weight=Decimal('0.450'), other_weight=Decimal('0.000'),
            metal_type='gold', purity='22K', current_store=self.store,
        )
        JewelleryStone.objects.create(unit=self.unit, stone_type='diamond', carat=Decimal('0.250'), rate_per_carat=Decimal('40000'))
        self.rule = JewelleryPricingRule.objects.create(
            product=self.product, making_method='percent', making_rate=Decimal('12'),
            wastage_method='weight', wastage_percent=Decimal('5'), tax_rate_code='GST-3', hallmark_charge=Decimal('45'),
        )
        JewelleryMetalRate.objects.create(store=self.store, metal_type='gold', purity='22K', rate_per_gram=Decimal('7500'))
        GSTRate.objects.create(
            code='GST-3', description='Jewellery GST', effective_from=date(2026, 1, 1),
            cgst_rate=Decimal('1.5'), sgst_rate=Decimal('1.5'), igst_rate=Decimal('3'),
        )

    def test_line_price_preserves_weight_and_component_breakdown(self):
        rate = JewelleryMetalRate.objects.get(store=self.store)
        result = calculate_jewellery_price(unit=self.unit, metal_rate=rate, pricing_rule=self.rule, tax_rate_code='GST-3')

        self.assertEqual(result['net_metal_weight'], Decimal('7.800'))
        self.assertEqual(result['metal_value'], Decimal('58500.00'))
        self.assertEqual(result['wastage_value'], Decimal('2925.00'))
        self.assertEqual(result['making_value'], Decimal('7020.00'))
        self.assertEqual(result['stone_value'], Decimal('10000.00'))
        self.assertEqual(result['taxable_value'], Decimal('78490.00'))
        self.assertEqual(result['final_amount'], Decimal('80844.70'))


class MetalRateResolutionTests(TestCase):
    """The Current Metal Rate Service: store fallback, fine-metal purity conversion, staleness, and
    product-level (non-serialized) dynamic pricing through the same calculate_jewellery_price engine."""

    def setUp(self):
        self.company = Company.objects.create(company_code='MRR-CO', company_name='Metal Rate Resolution Co')
        self.store = Store.objects.create(company=self.company, code='MRR', name='Metal Rate Resolution Store')
        GSTRate.objects.create(
            code='GST-3', description='Jewellery GST', effective_from=date(2026, 1, 1),
            cgst_rate=Decimal('1.5'), sgst_rate=Decimal('1.5'), igst_rate=Decimal('3'),
        )

    def test_exact_store_rate_is_preferred_over_company_wide(self):
        JewelleryMetalRate.objects.create(metal_type='gold', purity='22K', store=self.store, rate_per_gram=Decimal('7000'))
        JewelleryMetalRate.objects.create(metal_type='gold', purity='22K', store=None, rate_per_gram=Decimal('6800'))

        result = resolve_current_metal_rate(metal_type='gold', purity='22K', store=self.store)

        self.assertTrue(result['resolved'])
        self.assertEqual(result['rate_per_gram'], Decimal('7000'))
        self.assertFalse(result['is_store_fallback'])
        self.assertFalse(result['is_purity_converted'])

    def test_falls_back_to_company_wide_rate_when_no_store_rate(self):
        JewelleryMetalRate.objects.create(metal_type='gold', purity='22K', store=None, rate_per_gram=Decimal('6800'))

        result = resolve_current_metal_rate(metal_type='gold', purity='22K', store=self.store)

        self.assertTrue(result['resolved'])
        self.assertEqual(result['rate_per_gram'], Decimal('6800'))
        self.assertTrue(result['is_store_fallback'])

    def test_derives_purity_via_fine_metal_conversion_when_not_directly_maintained(self):
        JewelleryMetalRate.objects.create(metal_type='gold', purity='24K', store=self.store, rate_per_gram=Decimal('8000'))

        result = resolve_current_metal_rate(metal_type='gold', purity='22K', store=self.store)

        self.assertTrue(result['resolved'])
        self.assertTrue(result['is_purity_converted'])
        self.assertEqual(result['purity_used'], '24K')
        self.assertEqual(result['rate_per_gram'], (Decimal('8000') * Decimal('0.916')).quantize(Decimal('0.01')))

    def test_unresolved_when_no_rate_available_at_all(self):
        result = resolve_current_metal_rate(metal_type='gold', purity='22K', store=self.store)
        self.assertFalse(result['resolved'])
        self.assertIsNone(result['rate_per_gram'])

    def test_old_rate_is_flagged_stale(self):
        old_rate = JewelleryMetalRate.objects.create(
            metal_type='gold', purity='22K', store=self.store, rate_per_gram=Decimal('7000'),
            effective_from=dj_timezone.now() - timedelta(hours=5),
        )

        result = resolve_current_metal_rate(metal_type='gold', purity='22K', store=self.store, max_age_minutes=60)

        self.assertTrue(result['resolved'])
        self.assertTrue(result['is_stale'])
        self.assertGreater(result['age_minutes'], 60)

    def test_non_serialized_product_prices_dynamically_from_its_own_weight(self):
        category = ItemCategory.objects.create(name='Metal Rate Resolution Category')
        product = Product.objects.create(
            item_category=category, sku='MRR-001', name='Gold Chain (loose stock)',
            metal_type='gold', purity='22K', weight_grams=Decimal('5.000'),
        )
        rule = JewelleryPricingRule.objects.create(
            product=product, making_method='per_gram', making_rate=Decimal('500'),
            wastage_method='percent', wastage_percent=Decimal('4'), tax_rate_code='GST-3',
        )
        JewelleryMetalRate.objects.create(metal_type='gold', purity='22K', store=self.store, rate_per_gram=Decimal('7000'))

        rate_resolution = resolve_current_metal_rate(metal_type=product.metal_type, purity=product.purity, store=self.store)
        self.assertTrue(rate_resolution['resolved'])

        breakdown = calculate_jewellery_price(
            unit=ProductWeightAsUnit(product), metal_rate=ResolvedRate(rate_resolution['rate_per_gram']),
            pricing_rule=rule, tax_rate_code=rule.tax_rate_code,
        )

        self.assertEqual(breakdown['net_metal_weight'], Decimal('5.000'))
        self.assertEqual(breakdown['metal_value'], Decimal('35000.00'))
        self.assertGreater(breakdown['final_amount'], breakdown['metal_value'])

    def test_manual_unit_and_pricing_rule_drive_the_same_engine_for_ad_hoc_quotes(self):
        """The Price Simulator's ad-hoc inputs (no saved product) must go through the identical engine."""
        JewelleryMetalRate.objects.create(metal_type='silver', purity='999', store=None, rate_per_gram=Decimal('235'))

        rate_resolution = resolve_current_metal_rate(metal_type='silver', purity='999', store=None)
        self.assertTrue(rate_resolution['resolved'])

        breakdown = calculate_jewellery_price(
            unit=ManualUnit(gross_weight=Decimal('20.000')),
            metal_rate=ResolvedRate(rate_resolution['rate_per_gram']),
            pricing_rule=ManualPricingRule(making_method='fixed', making_rate=Decimal('300'), wastage_percent=Decimal('0')),
            tax_rate_code='GST-3',
        )

        self.assertEqual(breakdown['metal_value'], Decimal('4700.00'))
        self.assertEqual(breakdown['making_value'], Decimal('300.00'))


class FinanceOperationsPhase1Tests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='finance_operator', password='test-pass')
        self.company = Company.objects.create(
            company_code='GOLDI-FIN', company_name='Goldi Finance', status='active',
        )
        self.cash = GLAccount.objects.create(
            company=self.company, account_code='1000', account_name='Cash', account_type='asset', status='active',
        )
        self.expense = GLAccount.objects.create(
            company=self.company, account_code='6000', account_name='Office Expense', account_type='expense', status='active',
        )
        self.template = FinanceJournalTemplate.objects.create(
            code='JV-TEMPLATE', name='Journal Voucher', voucher_type='journal_voucher',
            approval_required=False,
        )
        self.batch = FinanceJournalBatch.objects.create(
            company=self.company, template=self.template, user=self.user,
            code='FIN-01', name='Finance Batch',
        )
        self.voucher_type = FinanceVoucherType.objects.create(
            company=self.company, code='journal_voucher', name='Journal Voucher',
            template=self.template, approval_required=False,
        )

    def test_balanced_voucher_posts_gl_and_snapshot_atomically(self):
        voucher = create_finance_voucher(
            company=self.company, batch=self.batch, voucher_type=self.voucher_type, user=self.user,
            narration='Office expense payment',
            lines=[
                {'line_no': 1, 'account': self.expense, 'description': 'Stationery', 'debit_amount': Decimal('1000.00')},
                {'line_no': 2, 'account': self.cash, 'description': 'Paid from cash', 'credit_amount': Decimal('1000.00')},
            ],
        )

        post_finance_voucher(voucher, self.user)
        voucher.refresh_from_db()

        self.assertEqual(voucher.status, 'posted')
        self.assertTrue(voucher.posting_no)
        self.assertEqual(voucher.posted_snapshot.posting_no, voucher.posting_no)
        self.assertEqual(voucher.posted_snapshot.lines.count(), 2)
        self.assertEqual(GeneralLedger.objects.filter(entry__reference=voucher.voucher_no).count(), 2)

    def test_approval_required_blocks_posting_until_approved(self):
        self.template.approval_required = True
        self.template.save(update_fields=['approval_required'])
        voucher = create_finance_voucher(
            company=self.company, batch=self.batch, voucher_type=self.voucher_type, user=self.user,
            lines=[
                {'line_no': 1, 'account': self.expense, 'debit_amount': Decimal('500.00')},
                {'line_no': 2, 'account': self.cash, 'credit_amount': Decimal('500.00')},
            ],
        )

        with self.assertRaises(ValueError):
            post_finance_voucher(voucher, self.user)
        approve_finance_voucher(voucher, self.user)
        post_finance_voucher(voucher, self.user)
        self.assertEqual(FinanceVoucher.objects.get(pk=voucher.pk).status, 'posted')

    def test_unbalanced_voucher_does_not_post(self):
        voucher = create_finance_voucher(
            company=self.company, batch=self.batch, voucher_type=self.voucher_type, user=self.user,
            lines=[
                {'line_no': 1, 'account': self.expense, 'debit_amount': Decimal('900.00')},
                {'line_no': 2, 'account': self.cash, 'credit_amount': Decimal('800.00')},
            ],
        )

        with self.assertRaises(ValueError):
            post_finance_voucher(voucher, self.user)
        self.assertFalse(FinancePostedVoucher.objects.filter(voucher=voucher).exists())


class SalesPayablesFoundationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='order_operator', password='test-pass')
        self.company = Company.objects.create(
            company_code='GOLDI-OTC', company_name='Goldi Commerce', status='active',
        )
        self.receivable = GLAccount.objects.create(
            company=self.company, account_code='1100', account_name='Trade Receivables', account_type='asset', status='active',
        )
        self.customer_advance = GLAccount.objects.create(
            company=self.company, account_code='2100', account_name='Customer Advances', account_type='liability', status='active',
        )
        self.payable = GLAccount.objects.create(
            company=self.company, account_code='2000', account_name='Trade Payables', account_type='liability', status='active',
        )
        self.vendor_advance = GLAccount.objects.create(
            company=self.company, account_code='1200', account_name='Vendor Advances', account_type='asset', status='active',
        )

    def test_customer_and_vendor_finance_profiles_use_configured_controls(self):
        term = PaymentTerm.objects.create(
            company=self.company, code='NET30', name='Net 30 days', due_days=30,
        )
        method = PaymentMethod.objects.create(
            company=self.company, code='BANK', name='Bank transfer', method_type='bank',
        )
        customer_group = CustomerPostingGroup.objects.create(
            company=self.company, code='DOMESTIC', name='Domestic customers',
            receivable_account=self.receivable, advance_account=self.customer_advance,
        )
        vendor_group = VendorPostingGroup.objects.create(
            company=self.company, code='DOMESTIC-V', name='Domestic vendors',
            payable_account=self.payable, advance_account=self.vendor_advance,
        )
        customer = Customer.objects.create(name='Retail Customer', customer_type='retail')
        vendor = Supplier.objects.create(name='Metal Supplier')
        customer_profile = CustomerFinanceProfile.objects.create(
            customer=customer, payment_term=term, payment_method=method,
            posting_group=customer_group, credit_limit=Decimal('100000.00'),
        )
        vendor_profile = VendorFinanceProfile.objects.create(
            vendor=vendor, payment_term=term, payment_method=method,
            posting_group=vendor_group,
        )

        self.assertEqual(customer_profile.payment_term.due_days, 30)
        self.assertEqual(customer_profile.posting_group.receivable_account, self.receivable)
        self.assertEqual(vendor_profile.posting_group.payable_account, self.payable)
        self.assertFalse(vendor_profile.payment_hold)

    def test_document_relationship_and_status_history_preserve_traceability(self):
        relationship = DocumentRelationship.objects.create(
            source_type='sales_order', source_id=101,
            target_type='sales_invoice', target_id=202,
            relationship_type='invoiced_from', created_by=self.user,
        )
        history = DocumentStatusHistory.objects.create(
            document_type='sales_order', document_id=101,
            from_status='draft', to_status='released', changed_by=self.user,
            reason='Credit and stock checks passed',
        )

        self.assertEqual(relationship.target_id, 202)
        self.assertEqual(history.from_status, 'draft')
        self.assertEqual(history.changed_by, self.user)


class ItemLocationMasterTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            company_code='GOLDI-ITEM', company_name='Goldi Merchandise', status='active',
        )
        self.division = Division.objects.create(company=self.company, code='APP', name='Apparel')
        self.special_group = SpecialGroup.objects.create(division=self.division, code='MEN', name='Mens')
        self.category = Category.objects.create(special_group=self.special_group, code='SHIRT', name='Shirts')
        self.subcategory = SubCategory.objects.create(category=self.category, code='FORMAL', name='Formal Shirts')
        self.uom = UnitOfMeasure.objects.create(company=self.company, code='PCS', name='Pieces')
        self.location = Location.objects.create(
            company=self.company, location_code='MUM-01', location_name='Mumbai Store', status='active',
        )

    def test_merchandise_hierarchy_and_item_validation(self):
        item = Item.objects.create(
            company=self.company, item_number='ITEM-0001', name='White Shirt',
            division=self.division, special_group=self.special_group,
            category=self.category, subcategory=self.subcategory, base_uom=self.uom,
            status='active',
        )

        item.full_clean()
        self.assertEqual(item.subcategory.category, self.category)
        self.assertEqual(item.category.special_group, self.special_group)
        self.assertEqual(item.special_group.division, self.division)

    def test_variant_sku_and_barcode_resolve_to_one_identity(self):
        item = Item.objects.create(
            company=self.company, item_number='ITEM-0002', name='Blue Shirt',
            division=self.division, special_group=self.special_group,
            category=self.category, subcategory=self.subcategory, base_uom=self.uom,
            status='active',
        )
        variant = ItemVariant.objects.create(item=item, code='BLUE-M', name='Blue Medium', color='Blue', size='M')
        sku = SKU.objects.create(item=item, variant=variant, location=self.location, code='SKU-BLUE-M')
        barcode = Barcode.objects.create(value='8900000000001', barcode_type='ean13', sku=sku, uom=self.uom, is_primary=True)

        self.assertEqual(barcode.sku.item, item)
        self.assertEqual(barcode.sku.variant, variant)
        self.assertEqual(barcode.sku.location, self.location)

    def test_uom_conversion_requires_positive_quantity(self):
        item = Item.objects.create(
            company=self.company, item_number='ITEM-0003', name='Boxed Shirt',
            division=self.division, special_group=self.special_group,
            category=self.category, subcategory=self.subcategory, base_uom=self.uom,
            status='active',
        )
        conversion = ItemUnitOfMeasure(item=item, uom=self.uom, quantity_per_uom=Decimal('0'))

        with self.assertRaises(ValidationError):
            conversion.full_clean()


class Phase1CoreFoundationTests(TestCase):
    def setUp(self):
        from erp.models import (
            AuditLog, Branch, BusinessUnit, Company, Department, Location,
            Permission, Project, RolePermission, UserRole,
        )

        self.company = Company.objects.create(
            company_code='GOLDI',
            company_name='Goldi Demo Pvt Ltd',
            legal_name='Goldi Demo Private Limited',
            registration_number='U74999MH2025PTC000000',
            country='IN',
            currency_code='INR',
            base_currency_code='INR',
            fiscal_year_start=4,
            timezone='Asia/Kolkata',
            date_format='DD/MM/YYYY',
            language='en-in',
            gstin='27ABCDE1234F1Z5',
            pan='ABCDE1234F',
            tan='MUMA12345A',
            email='finance@goldidemo.in',
            phone='+91-9999999999',
            website='https://www.goldidemo.in',
            status='active',
        )
        self.branch = Branch.objects.create(
            company=self.company,
            branch_code='MUM',
            branch_name='Mumbai HQ',
            city='Mumbai',
            state_code='27',
            status='active',
        )
        self.location = Location.objects.create(
            company=self.company,
            branch=self.branch,
            location_code='WH-01',
            location_name='Main Warehouse',
            city='Mumbai',
            state_code='27',
            status='active',
        )
        self.department = Department.objects.create(
            company=self.company,
            department_code='FIN',
            department_name='Finance',
            status='active',
        )
        self.business_unit = BusinessUnit.objects.create(
            company=self.company,
            business_unit_code='B1',
            business_unit_name='Jewellery Business',
            status='active',
        )
        self.project = Project.objects.create(
            company=self.company,
            project_code='PROJ-01',
            project_name='ERP Modernization',
            status='active',
        )
        self.role = Role.objects.create(name='Finance Manager', description='Finance leadership role')
        self.permission = Permission.objects.create(
            code='finance.post',
            name='Post Journal Entries',
            category='finance',
        )
        self.user = User.objects.create_user(username='finance_user', password='StrongPass123!')
        self.user_role = UserRole.objects.create(user=self.user, role=self.role, company=self.company)
        self.role_permission = RolePermission.objects.create(role=self.role, permission=self.permission)
        self.audit_log = AuditLog.objects.create(
            company=self.company,
            actor=self.user,
            table_name='companies',
            action='create',
            record_id=str(self.company.pk),
            description='Company created for ERP rollout',
            ip_address='127.0.0.1',
            device='web',
        )

    def test_company_hierarchy_and_metadata(self):
        self.assertEqual(self.company.company_code, 'GOLDI')
        self.assertEqual(self.branch.company, self.company)
        self.assertEqual(self.location.branch, self.branch)
        self.assertEqual(self.department.company, self.company)
        self.assertEqual(self.business_unit.company, self.company)
        self.assertEqual(self.project.company, self.company)
        self.assertEqual(self.company.status, 'active')

    def test_user_role_and_permission_assignment(self):
        self.assertEqual(self.user_role.user, self.user)
        self.assertEqual(self.user_role.role, self.role)
        self.assertIn(self.permission, self.role.permissions.all())

    def test_audit_record_tracking(self):
        self.assertEqual(self.audit_log.company, self.company)
        self.assertEqual(self.audit_log.actor, self.user)
        self.assertEqual(self.audit_log.table_name, 'companies')
        self.assertEqual(self.audit_log.action, 'create')
        self.assertEqual(self.audit_log.ip_address, '127.0.0.1')


class Phase2FinancialCoreTests(TestCase):
    def setUp(self):
        from erp.models import Branch, Company

        self.company = Company.objects.create(
            company_code='GOLDI-GL',
            company_name='Goldi Financials',
            country='IN',
            currency_code='INR',
            status='active',
        )
        self.branch = Branch.objects.create(
            company=self.company,
            branch_code='MUM',
            branch_name='Mumbai Office',
            city='Mumbai',
            state_code='27',
            status='active',
        )
        self.cash_account = GLAccount.objects.create(
            company=self.company,
            account_code='1000',
            account_name='Cash and Bank',
            account_type='asset',
            status='active',
        )
        self.revenue_account = GLAccount.objects.create(
            company=self.company,
            account_code='4000',
            account_name='Sales Revenue',
            account_type='revenue',
            status='active',
        )
        self.entry = JournalEntry.objects.create(
            company=self.company,
            branch=self.branch,
            entry_no='JV-0001',
            reference='SALE-001',
            narration='Cash sale recorded',
            status='draft',
        )
        JournalEntryLine.objects.create(
            entry=self.entry,
            account=self.cash_account,
            description='Cash received from customer',
            debit_amount=Decimal('15000.00'),
        )
        JournalEntryLine.objects.create(
            entry=self.entry,
            account=self.revenue_account,
            description='Revenue recognized on sale',
            credit_amount=Decimal('15000.00'),
        )

    def test_chart_of_accounts_and_balanced_journal_entry(self):
        self.assertEqual(self.cash_account.account_type, 'asset')
        self.assertEqual(self.revenue_account.account_type, 'revenue')
        self.assertEqual(self.entry.total_debit, Decimal('15000.00'))
        self.assertEqual(self.entry.total_credit, Decimal('15000.00'))
        self.assertTrue(self.entry.is_balanced)

    def test_posting_creates_ledger_entries(self):
        self.entry.post()
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, 'posted')
        self.assertTrue(GeneralLedger.objects.filter(entry=self.entry, account=self.cash_account).exists())
        self.assertTrue(GeneralLedger.objects.filter(entry=self.entry, account=self.revenue_account).exists())

    def test_dimension_set_combines_account_and_company_dimensions(self):
        department = Department.objects.create(
            company=self.company,
            department_code='FIN',
            department_name='Finance',
        )
        cost_center = CostCenter.objects.create(company=self.company, code='CC-01', name='Head Office')
        store = Store.objects.create(company=self.company, code='STORE-01', name='Mumbai Store')
        brand = Brand.objects.create(company=self.company, code='GOLDI', name='Goldi')
        channel = Channel.objects.create(company=self.company, code='POS', name='Point of Sale')
        dimension_set = DimensionSet.objects.create(
            company=self.company,
            gl_account=self.cash_account,
            department=department,
            cost_center=cost_center,
            store=store,
            brand=brand,
            business_unit=BusinessUnit.objects.create(
                company=self.company,
                business_unit_code='RETAIL',
                business_unit_name='Retail',
            ),
            project=None,
            channel=channel,
        )

        self.assertEqual(dimension_set.gl_account, self.cash_account)
        self.assertEqual(dimension_set.store, store)

    def test_dimension_set_rejects_dimensions_from_another_company(self):
        other_company = Company.objects.create(
            company_code='GOLDI-GL-2',
            company_name='Other Financials',
            status='active',
        )
        foreign_department = Department.objects.create(
            company=other_company,
            department_code='OPS',
            department_name='Operations',
        )
        dimension_set = DimensionSet(
            company=self.company,
            gl_account=self.cash_account,
            department=foreign_department,
        )

        with self.assertRaises(ValidationError):
            dimension_set.full_clean()


class Phase3BankingAndPaymentsTests(TestCase):
    def setUp(self):
        from erp.models import Company

        self.company = Company.objects.create(
            company_code='GOLDI-BANK',
            company_name='Goldi Banking',
            currency_code='INR',
            status='active',
        )
        self.bank = BankAccount.objects.create(
            company=self.company,
            bank_name='HDFC Bank',
            account_name='Goldi Current Account',
            account_number='1234567890',
            account_type='current',
            opening_balance=Decimal('50000.00'),
            current_balance=Decimal('50000.00'),
            status='active',
        )
        self.customer = Customer.objects.create(
            name='Amit Verma',
            phone='9090909090',
            gstin='27ABCDE1234F1Z5',
            is_active=True,
        )
        self.invoice = SalesInvoice.objects.create(
            invoice_no='INV-9001',
            customer=self.customer,
            subtotal=Decimal('15000.00'),
            gst_amount=Decimal('450.00'),
            total_amount=Decimal('15450.00'),
            status='approved',
        )

    def test_payment_post_updates_bank_balance(self):
        payment = Payment.objects.create(
            invoice=self.invoice,
            payment_method='bank_transfer',
            amount=Decimal('15450.00'),
            bank_account=self.bank,
            status='pending',
            reference='UTR-001',
        )

        payment.post()
        payment.refresh_from_db()

        self.assertEqual(payment.status, 'posted')
        self.assertEqual(self.bank.current_balance, Decimal('65450.00'))
        self.assertTrue(BankTransaction.objects.filter(payment=payment, bank_account=self.bank).exists())


class Phase4ProcurementAndPayablesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            company_code='GOLDI-AP',
            company_name='Goldi Procurement',
            currency_code='INR',
            status='active',
        )
        self.supplier = Supplier.objects.create(
            name='Kiran Metals Pvt Ltd',
            phone='9876543210',
            gstin='27ABCDE1234F1Z5',
            is_active=True,
        )
        self.bill = SupplierInvoice.objects.create(
            company=self.company,
            supplier=self.supplier,
            invoice_no='SUP-1001',
            invoice_date=date.today(),
            gross_amount=Decimal('30000.00'),
            tax_amount=Decimal('4500.00'),
            net_amount=Decimal('34500.00'),
            due_date=date.today() + timedelta(days=15),
            status='open',
        )

    def test_supplier_invoice_tracks_outstanding_ap(self):
        self.assertEqual(self.bill.outstanding_amount, Decimal('34500.00'))

        self.bill.apply_payment(Decimal('15000.00'))
        self.bill.refresh_from_db()

        self.assertEqual(self.bill.paid_amount, Decimal('15000.00'))
        self.assertEqual(self.bill.outstanding_amount, Decimal('19500.00'))

    def test_paid_supplier_invoice_updates_status(self):
        self.bill.apply_payment(self.bill.net_amount)
        self.bill.refresh_from_db()

        self.assertEqual(self.bill.status, 'paid')
        self.assertEqual(self.bill.outstanding_amount, Decimal('0.00'))


class Phase5InventoryControlTests(TestCase):
    def setUp(self):
        self.category = ItemCategory.objects.create(name='Gold Coins', description='Bullion inventory')
        self.product = Product.objects.create(
            item_category=self.category,
            sku='GOLD-100',
            name='22K Gold Coin',
            metal_type='gold',
            purity='22K',
            weight_grams=Decimal('10.000'),
            purchase_price=Decimal('8200.00'),
            sale_price=Decimal('9000.00'),
            mrp=Decimal('9500.00'),
            barcode='GOLD-100',
            hsn_code='7108',
            stock_quantity=Decimal('0.000'),
        )

    def test_inventory_inward_updates_stock_and_ledger(self):
        movement = InventoryMovement.objects.create(
            product=self.product,
            movement_type='inward',
            quantity=Decimal('25.000'),
            reference='PO-5001',
            notes='Initial stock received',
        )

        movement.post()
        self.product.refresh_from_db()

        self.assertEqual(self.product.stock_quantity, Decimal('25.000'))
        self.assertTrue(StockLedger.objects.filter(product=self.product, reference='PO-5001').exists())

    def test_inventory_outward_reduces_stock_and_prevents_negative_balance(self):
        InventoryMovement.objects.create(
            product=self.product,
            movement_type='inward',
            quantity=Decimal('20.000'),
            reference='GRN-5001',
        ).post()

        outward = InventoryMovement.objects.create(
            product=self.product,
            movement_type='outward',
            quantity=Decimal('8.000'),
            reference='SALE-5001',
        )

        outward.post()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, Decimal('12.000'))

        with self.assertRaises(ValueError):
            InventoryMovement.objects.create(
                product=self.product,
                movement_type='outward',
                quantity=Decimal('30.000'),
                reference='OVERDRAW-1',
            ).post()


def _configure_finance_posting(company, customer=None, vendor=None):
    """Create default GL accounts + posting groups + a FinancePostingSetup for a test company, wiring the given customer/vendor to them."""
    prefix = company.company_code
    accounts = {
        'receivable': GLAccount.objects.create(company=company, account_code=f'{prefix}-AR', account_name='Accounts Receivable', account_type='asset'),
        'advance_receivable': GLAccount.objects.create(company=company, account_code=f'{prefix}-CADV', account_name='Customer Advances', account_type='liability'),
        'payable': GLAccount.objects.create(company=company, account_code=f'{prefix}-AP', account_name='Accounts Payable', account_type='liability'),
        'advance_payable': GLAccount.objects.create(company=company, account_code=f'{prefix}-VADV', account_name='Vendor Advances', account_type='asset'),
        'revenue': GLAccount.objects.create(company=company, account_code=f'{prefix}-REV', account_name='Sales Revenue', account_type='revenue'),
        'gst_output': GLAccount.objects.create(company=company, account_code=f'{prefix}-GSTOUT', account_name='GST Output', account_type='liability'),
        'expense': GLAccount.objects.create(company=company, account_code=f'{prefix}-PEXP', account_name='Purchase Expense', account_type='expense'),
        'gst_input': GLAccount.objects.create(company=company, account_code=f'{prefix}-GSTIN', account_name='GST Input', account_type='asset'),
        'cash': GLAccount.objects.create(company=company, account_code=f'{prefix}-CASH', account_name='Cash', account_type='asset'),
    }
    FinancePostingSetup.objects.create(
        company=company, sales_revenue_account=accounts['revenue'], gst_output_account=accounts['gst_output'],
        purchase_expense_account=accounts['expense'], gst_input_account=accounts['gst_input'], default_cash_account=accounts['cash'],
    )
    if customer is not None:
        posting_group = CustomerPostingGroup.objects.create(
            company=company, code=f'{prefix}-CPG', name='Default Customers',
            receivable_account=accounts['receivable'], advance_account=accounts['advance_receivable'],
        )
        customer.customer_posting_group = posting_group
        customer.save(update_fields=['customer_posting_group'])
    if vendor is not None:
        posting_group = VendorPostingGroup.objects.create(
            company=company, code=f'{prefix}-VPG', name='Default Vendors',
            payable_account=accounts['payable'], advance_account=accounts['advance_payable'],
        )
        VendorFinanceProfile.objects.create(vendor=vendor, posting_group=posting_group)
    return accounts


class SalesReceivablesFlowTests(TestCase):
    """End-to-end chain: Quotation -> Sales Order -> Invoice -> Payment Receipt (spec S70/S71)."""

    def setUp(self):
        self.user = User.objects.create_user(username='sales-flow-user', password='test-pass')
        self.company = Company.objects.create(company_code='SR-CO', company_name='Sales Receivables Co')
        self.store = Store.objects.create(company=self.company, code='SR', name='Sales Receivables Store')
        self.customer = Customer.objects.create(name='Ananya Rao', phone='9990001111')
        self.gl_accounts = _configure_finance_posting(self.company, customer=self.customer)
        category = ItemCategory.objects.create(name='Sales Receivables Category')

        self.plain_product = Product.objects.create(
            item_category=category, sku='SR-PLAIN-001', name='Silver Chain', sale_price=Decimal('1000.00'), mrp=Decimal('1200.00'),
        )
        self.jewel_product = Product.objects.create(
            item_category=category, sku='SR-JWL-001', name='Gold Bangle', metal_type='gold', purity='22K',
        )
        self.jewellery_unit = JewelleryItemUnit.objects.create(
            product=self.jewel_product, barcode='SR-BAR-001', serial_number='SR-SER-001',
            gross_weight=Decimal('10.000'), stone_weight=Decimal('0.000'), other_weight=Decimal('0.000'),
            metal_type='gold', purity='22K', current_store=self.store,
        )
        self.pricing_rule = JewelleryPricingRule.objects.create(
            product=self.jewel_product, making_method='percent', making_rate=Decimal('10'),
            wastage_method='weight', wastage_percent=Decimal('4'), tax_rate_code='GST-3',
        )
        JewelleryMetalRate.objects.create(store=self.store, metal_type='gold', purity='22K', rate_per_gram=Decimal('6000'))
        GSTRate.objects.create(
            code='GST-3', description='Jewellery GST', effective_from=date(2026, 1, 1),
            cgst_rate=Decimal('1.5'), sgst_rate=Decimal('1.5'), igst_rate=Decimal('3'),
        )

    def test_full_chain_quotation_to_payment(self):
        quotation = create_quotation(
            customer=self.customer, store=self.store, salesperson=None,
            lines_data=[
                {'product': self.plain_product, 'quantity': Decimal('4'), 'discount_amount': Decimal('0')},
                {'product': self.jewel_product, 'quantity': Decimal('1'), 'discount_amount': Decimal('0'), 'jewellery_unit': self.jewellery_unit},
            ],
            user=self.user,
        )
        self.assertEqual(quotation.status, 'draft')
        self.assertEqual(quotation.lines.count(), 2)
        self.assertGreater(quotation.total_amount, Decimal('0'))

        submit_for_approval(quotation, 'quotation', self.user)
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, 'pending_approval')

        approve_quotation(quotation, self.user, approved=True)
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, 'approved')

        plain_line = quotation.lines.get(product=self.plain_product)
        jewel_line = quotation.lines.get(product=self.jewel_product)

        # Partial conversion: only 3 of the 4 plain units move to the Sales Order.
        sales_order = convert_quotation_to_sales_order(
            quotation, self.user, {str(plain_line.pk): Decimal('3'), str(jewel_line.pk): Decimal('1')},
        )
        plain_line.refresh_from_db()
        quotation.refresh_from_db()
        self.assertEqual(plain_line.remaining_quantity, Decimal('1.000'))
        self.assertEqual(quotation.status, 'approved')  # not fully converted, stays open
        self.assertEqual(sales_order.lines.count(), 2)

        submit_for_approval(sales_order, 'sales_order', self.user)
        sales_order, warning = approve_sales_order(sales_order, self.user, approved=True)
        self.assertEqual(sales_order.status, 'approved')
        self.jewellery_unit.refresh_from_db()
        self.assertEqual(self.jewellery_unit.current_status, 'reserved')

        so_plain_line = sales_order.lines.get(product=self.plain_product)
        so_jewel_line = sales_order.lines.get(product=self.jewel_product)
        self.assertEqual(so_plain_line.reserved_quantity, Decimal('3.000'))

        # Partial invoicing: only 2 of the 3 plain units are billed now.
        invoice = convert_sales_order_to_invoice(
            sales_order, self.user, {str(so_plain_line.pk): Decimal('2'), str(so_jewel_line.pk): Decimal('1')},
        )
        sales_order.refresh_from_db()
        so_plain_line.refresh_from_db()
        self.assertEqual(sales_order.status, 'partially_fulfilled')
        self.assertEqual(so_plain_line.remaining_quantity, Decimal('1.000'))
        self.assertEqual(invoice.items.count(), 2)
        self.assertEqual(invoice.sales_order_id, sales_order.pk)
        self.assertEqual(invoice.quotation_id, quotation.pk)
        self.assertEqual(invoice.status, 'approved')  # the sales order already cleared maker-checker approval

        post_sales_invoice(invoice, self.user)
        invoice.refresh_from_db()
        self.jewellery_unit.refresh_from_db()
        self.assertEqual(invoice.status, 'posted')
        self.assertEqual(self.jewellery_unit.current_status, 'sold')

        # The Finance Posting Bridge must have created a balanced G/L voucher for this invoice.
        self.gl_accounts['receivable'].refresh_from_db()
        self.gl_accounts['revenue'].refresh_from_db()
        self.gl_accounts['gst_output'].refresh_from_db()
        self.assertEqual(self.gl_accounts['receivable'].current_balance, invoice.total_amount)
        self.assertEqual(self.gl_accounts['revenue'].current_balance, -(invoice.total_amount - invoice.gst_amount))
        self.assertEqual(self.gl_accounts['gst_output'].current_balance, -invoice.gst_amount)
        voucher = FinanceVoucher.objects.get(document_no=invoice.invoice_no)
        self.assertEqual(voucher.status, 'posted')
        self.assertTrue(voucher.is_balanced)

        # First receipt pays half the invoice.
        half = (invoice.total_amount / Decimal('2')).quantize(Decimal('0.01'))
        receipt_1 = create_payment_receipt(
            customer=self.customer, store=self.store, payment_method=None, amount=half,
            allocations={invoice.pk: half}, user=self.user,
        )
        post_payment_receipt(receipt_1, self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'partially_paid')
        self.assertEqual(invoice.paid_amount, half)

        # Second receipt clears the remaining balance.
        remaining = invoice.balance_amount
        receipt_2 = create_payment_receipt(
            customer=self.customer, store=self.store, payment_method=None, amount=remaining,
            allocations={invoice.pk: remaining}, user=self.user,
        )
        post_payment_receipt(receipt_2, self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, 'paid')
        self.assertEqual(invoice.balance_amount, Decimal('0.00'))

        # Fully paid: receivable nets back to zero, cash received equals the invoice total, and the whole
        # chart of accounts still nets to zero (the fundamental double-entry invariant).
        self.gl_accounts['receivable'].refresh_from_db()
        self.gl_accounts['cash'].refresh_from_db()
        self.assertEqual(self.gl_accounts['receivable'].current_balance, Decimal('0.00'))
        self.assertEqual(self.gl_accounts['cash'].current_balance, invoice.total_amount)
        total_balance = GLAccount.objects.filter(company=self.company).aggregate(total=Sum('current_balance'))['total']
        self.assertEqual(total_balance, Decimal('0.00'))

        related = get_related_documents(invoice, 'sales_invoice')
        self.assertIn('sales_order', related)
        self.assertIn('quotation', related)
        self.assertIn('payment_receipt', related)
        self.assertIn('finance_voucher', related)
        self.assertEqual({r.pk for r in related['payment_receipt']}, {receipt_1.pk, receipt_2.pk})

        order_related = get_related_documents(sales_order, 'sales_order')
        self.assertIn('quotation', order_related)
        self.assertIn('sales_invoice', order_related)


class PurchasePayablesFlowTests(TestCase):
    """End-to-end chain: Vendor -> Purchase Order -> Goods Receipt -> Purchase Invoice -> Vendor Payment."""

    def setUp(self):
        self.user = User.objects.create_user(username='purchase-flow-user', password='test-pass')
        self.company = Company.objects.create(company_code='PP-CO', company_name='Purchase Payables Co')
        self.warehouse = Warehouse.objects.create(company=self.company, code='PP-WH', name='Purchase Payables Warehouse')
        self.vendor = Supplier.objects.create(name='Kiran Metals Pvt Ltd', phone='9990002222', gstin='27ABCDE1234F1Z5')
        self.gl_accounts = _configure_finance_posting(self.company, vendor=self.vendor)
        category = ItemCategory.objects.create(name='Purchase Payables Category')
        self.product = Product.objects.create(
            item_category=category, sku='PP-RAW-001', name='24K Gold Bar', purchase_price=Decimal('1000.00'),
        )

    def test_full_chain_purchase_order_to_payment(self):
        self.assertTrue(self.vendor.vendor_no.startswith('VEN-'))

        po = create_purchase_order(
            vendor=self.vendor, warehouse=self.warehouse, buyer=None,
            lines_data=[{'product': self.product, 'quantity': Decimal('100'), 'unit_price': Decimal('1000.00'), 'tax_rate': Decimal('18.00')}],
            user=self.user,
        )
        self.assertEqual(po.status, 'draft')
        self.assertEqual(po.total_amount, Decimal('118000.00'))

        submit_for_approval(po, 'purchase_order', self.user)
        po.refresh_from_db()
        self.assertEqual(po.status, 'pending_approval')

        approve_purchase_order(po, self.user, approved=True)
        po.refresh_from_db()
        self.assertEqual(po.status, 'approved')

        line = po.lines.get(product=self.product)

        # First partial shipment: 60 of 100 units.
        receipt_1 = receive_goods(po, self.user, {str(line.pk): Decimal('60')})
        po.refresh_from_db()
        line.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(po.status, 'partially_received')
        self.assertEqual(line.received_quantity, Decimal('60.000'))
        self.assertEqual(self.product.stock_quantity, Decimal('60.000'))
        self.assertTrue(StockLedger.objects.filter(product=self.product, reference=receipt_1.receipt_no).exists())

        # Second shipment completes the order.
        receive_goods(po, self.user, {str(line.pk): Decimal('40')})
        po.refresh_from_db()
        line.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(po.status, 'fully_received')
        self.assertEqual(line.received_quantity, Decimal('100.000'))
        self.assertEqual(self.product.stock_quantity, Decimal('100.000'))

        # Invoice only half of what's been received; the rest stays open for a later invoice.
        invoice = convert_receipt_to_purchase_invoice(
            receipt_1, self.user, vendor_invoice_no='VINV-1001', line_quantities={str(line.pk): Decimal('50')},
        )
        line.refresh_from_db()
        self.assertEqual(line.invoiced_quantity, Decimal('50.000'))
        self.assertEqual(line.remaining_to_invoice, Decimal('50.000'))
        self.assertEqual(invoice.workflow_status, 'approved')  # the PO already cleared maker-checker approval
        self.assertEqual(invoice.net_amount, Decimal('59000.00'))
        self.assertEqual(invoice.purchase_order_id, po.pk)

        post_purchase_invoice(invoice, self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.workflow_status, 'posted')

        # The Finance Posting Bridge must have created a balanced G/L voucher for this invoice.
        self.gl_accounts['payable'].refresh_from_db()
        self.gl_accounts['expense'].refresh_from_db()
        self.gl_accounts['gst_input'].refresh_from_db()
        self.assertEqual(self.gl_accounts['payable'].current_balance, -invoice.net_amount)
        self.assertEqual(self.gl_accounts['expense'].current_balance, invoice.net_amount - invoice.tax_amount)
        self.assertEqual(self.gl_accounts['gst_input'].current_balance, invoice.tax_amount)
        voucher = FinanceVoucher.objects.get(document_no=invoice.invoice_no)
        self.assertEqual(voucher.status, 'posted')
        self.assertTrue(voucher.is_balanced)

        half = (invoice.net_amount / Decimal('2')).quantize(Decimal('0.01'))
        payment_1 = create_vendor_payment(
            vendor=self.vendor, bank_account=None, payment_method=None, amount=half,
            allocations={invoice.pk: half}, user=self.user,
        )
        post_vendor_payment(payment_1, self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'partial')
        self.assertEqual(invoice.paid_amount, half)

        remaining = invoice.outstanding_amount
        payment_2 = create_vendor_payment(
            vendor=self.vendor, bank_account=None, payment_method=None, amount=remaining,
            allocations={invoice.pk: remaining}, user=self.user,
        )
        post_vendor_payment(payment_2, self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'paid')
        self.assertEqual(invoice.outstanding_amount, Decimal('0.00'))

        # Fully paid: payable nets back to zero, cash paid out equals the invoice total, and the whole
        # chart of accounts still nets to zero (the fundamental double-entry invariant).
        self.gl_accounts['payable'].refresh_from_db()
        self.gl_accounts['cash'].refresh_from_db()
        self.assertEqual(self.gl_accounts['payable'].current_balance, Decimal('0.00'))
        self.assertEqual(self.gl_accounts['cash'].current_balance, -invoice.net_amount)
        total_balance = GLAccount.objects.filter(company=self.company).aggregate(total=Sum('current_balance'))['total']
        self.assertEqual(total_balance, Decimal('0.00'))

        related = get_related_documents(invoice, 'purchase_invoice')
        self.assertIn('purchase_order', related)
        self.assertIn('goods_receipt', related)
        self.assertIn('vendor_payment', related)
        self.assertIn('finance_voucher', related)
        self.assertEqual({p.pk for p in related['vendor_payment']}, {payment_1.pk, payment_2.pk})

        order_related = get_related_documents(po, 'purchase_order')
        self.assertIn('goods_receipt', order_related)
        self.assertIn('purchase_invoice', order_related)


class FinanceGLIntegrationTests(TestCase):
    """Posting to the G/L is mandatory and atomic: if it fails, the whole document posting rolls back (spec S14/S43)."""

    def setUp(self):
        self.user = User.objects.create_user(username='gl-integration-user', password='test-pass')
        self.company = Company.objects.create(company_code='GL-CO', company_name='GL Integration Co')
        self.customer = Customer.objects.create(name='Unconfigured Customer', phone='9990003333')
        # Deliberately no _configure_finance_posting call: no posting group, no FinancePostingSetup.
        category = ItemCategory.objects.create(name='GL Integration Category')
        self.product = Product.objects.create(item_category=category, sku='GL-001', name='Test Item', sale_price=Decimal('500.00'))

    def test_posting_without_finance_setup_rolls_back_the_whole_invoice(self):
        quotation = create_quotation(
            customer=self.customer, store=None, salesperson=None,
            lines_data=[{'product': self.product, 'quantity': Decimal('1'), 'discount_amount': Decimal('0')}],
            user=self.user,
        )
        submit_for_approval(quotation, 'quotation', self.user)
        approve_quotation(quotation, self.user, approved=True)
        line = quotation.lines.get(product=self.product)
        sales_order = convert_quotation_to_sales_order(quotation, self.user, {str(line.pk): Decimal('1')})
        submit_for_approval(sales_order, 'sales_order', self.user)
        sales_order, _ = approve_sales_order(sales_order, self.user, approved=True)
        so_line = sales_order.lines.get(product=self.product)
        invoice = convert_sales_order_to_invoice(sales_order, self.user, {str(so_line.pk): Decimal('1')})

        with self.assertRaises(ValueError):
            post_sales_invoice(invoice, self.user)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'approved')  # unchanged: the whole atomic transaction rolled back
        self.assertFalse(FinanceVoucher.objects.filter(document_no=invoice.invoice_no).exists())
