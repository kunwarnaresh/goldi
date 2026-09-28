from decimal import Decimal
from datetime import date

from django.core.management.base import BaseCommand

from erp.models import (
    BankAccount, Barcode, BinLocation, Brand, Branch, BusinessUnit, Category, Channel,
    Company, CostCenter, Customer, CustomerDiscountGroup, CustomerFinanceProfile,
    CustomerPostingGroup, CustomerPriceGroup, Department, Division, DocumentNumberSeries,
    Employee, FinanceJournalTemplate, FinancePostingSetup, FinanceVoucherType, GLAccount,
    GSTComponent, GSTGroup, GSTRegistration, GSTState, GSTTaxRule, GSTRate, HSNCode,
    Item, ItemCategory, ItemLocation, ItemUnitOfMeasure, ItemVariant, Location,
    PaymentMethod, PaymentTerm, Permission, Product, Role, SACCode, SKU, SpecialGroup,
    Store, SubCategory, SupplyType, StockLedger, Supplier, UnitOfMeasure,
    VendorFinanceProfile, VendorPostingGroup, Warehouse, WarehouseZone,
)


class Command(BaseCommand):
    help = 'Seed demo jewelry ERP data for local development, including a working Finance posting setup.'

    def handle(self, *args, **options):
        company, _ = Company.objects.get_or_create(
            company_code='GOLDI', defaults={'company_name': 'Goldi Jewellers', 'status': 'active'},
        )

        customer, _ = Customer.objects.get_or_create(
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

        supplier, _ = Supplier.objects.get_or_create(
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

        self._seed_finance_posting_setup(company, customer, supplier)

        self.stdout.write(self.style.SUCCESS('Demo jewelry ERP seed data created successfully.'))

    def _seed_finance_posting_setup(self, company, customer, supplier):
        """Default chart of accounts + posting groups so Sales/Purchase documents can post to the G/L out of the box."""
        if FinancePostingSetup.objects.filter(company=company).exists():
            return

        def account(code, name, account_type):
            obj, _ = GLAccount.objects.get_or_create(
                account_code=code, defaults={'company': company, 'account_name': name, 'account_type': account_type},
            )
            return obj

        receivable = account('1100', 'Trade Receivables', 'asset')
        customer_advance = account('1150', 'Customer Advances', 'liability')
        payable = account('2000', 'Trade Payables', 'liability')
        vendor_advance = account('1200', 'Vendor Advances', 'asset')
        revenue = account('4000', 'Sales Revenue', 'revenue')
        gst_output = account('2100', 'GST Output Payable', 'liability')
        expense = account('5000', 'Purchase Expense', 'expense')
        gst_input = account('1300', 'GST Input Credit', 'asset')
        cash = account('1000', 'Cash', 'asset')

        FinancePostingSetup.objects.create(
            company=company, sales_revenue_account=revenue, gst_output_account=gst_output,
            purchase_expense_account=expense, gst_input_account=gst_input, default_cash_account=cash,
        )

        customer_group, _ = CustomerPostingGroup.objects.get_or_create(
            company=company, code='DOMESTIC',
            defaults={'name': 'Domestic Customers', 'receivable_account': receivable, 'advance_account': customer_advance},
        )
        customer.customer_posting_group = customer_group
        customer.save(update_fields=['customer_posting_group'])

        vendor_group, _ = VendorPostingGroup.objects.get_or_create(
            company=company, code='DOMESTIC-V',
            defaults={'name': 'Domestic Vendors', 'payable_account': payable, 'advance_account': vendor_advance},
        )
        VendorFinanceProfile.objects.get_or_create(vendor=supplier, defaults={'posting_group': vendor_group})
