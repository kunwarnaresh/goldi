from decimal import Decimal
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from erp.models import (
    BankAccount, Barcode, BinLocation, Brand, Branch, BusinessUnit, Category, Channel,
    Company, CostCenter, Customer, CustomerDiscountGroup, CustomerFinanceProfile, Project,
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

        self._seed_master_catalog(company, customer, supplier)
        self._seed_finance_posting_setup(company, customer, supplier)

        self.stdout.write(self.style.SUCCESS('Demo jewelry ERP seed data created successfully.'))

    @transaction.atomic
    def _seed_master_catalog(self, company, customer, supplier):
        branch, _ = Branch.objects.get_or_create(
            company=company, branch_code='MUM',
            defaults={'branch_name': 'Mumbai Flagship', 'city': 'Mumbai', 'state_code': '27', 'status': 'active'},
        )
        department, _ = Department.objects.get_or_create(
            company=company, department_code='RETAIL',
            defaults={'department_name': 'Retail Operations', 'status': 'active'},
        )
        BusinessUnit.objects.get_or_create(
            company=company, business_unit_code='JEWELLERY',
            defaults={'business_unit_name': 'Jewellery Retail', 'status': 'active'},
        )
        CostCenter.objects.get_or_create(company=company, code='MUM-RETAIL', defaults={'name': 'Mumbai Retail'})
        Project.objects.get_or_create(
            company=company, project_code='SHOWROOM-2026',
            defaults={'project_name': 'Flagship showroom operations', 'status': 'active'},
        )
        store, _ = Store.objects.get_or_create(
            company=company, code='MUM-01',
            defaults={
                'branch': branch, 'name': 'Mumbai Flagship Store', 'store_type': 'flagship',
                'city': 'Mumbai', 'state': 'Maharashtra', 'pin_code': '400001', 'status': 'active',
            },
        )
        location, _ = Location.objects.get_or_create(
            company=company, location_code='MUM-SHOWROOM',
            defaults={
                'branch': branch, 'location_name': 'Mumbai showroom', 'city': 'Mumbai',
                'state_code': '27', 'status': 'active',
            },
        )
        if store.location_id is None:
            store.location = location
            store.save(update_fields=['location'])

        brand, _ = Brand.objects.get_or_create(
            company=company, code='GOLDI', defaults={'name': 'Goldi Jewellers'},
        )
        channel, _ = Channel.objects.get_or_create(
            company=company, code='SHOWROOM', defaults={'name': 'Showroom'},
        )
        division, _ = Division.objects.get_or_create(
            company=company, code='GOLD', defaults={'name': 'Gold Jewellery', 'active': True},
        )
        special_group, _ = SpecialGroup.objects.get_or_create(
            division=division, code='RINGS', defaults={'name': 'Rings', 'active': True},
        )
        category, _ = Category.objects.get_or_create(
            special_group=special_group, code='GOLD-RINGS', defaults={'name': 'Gold Rings', 'active': True},
        )
        subcategory, _ = SubCategory.objects.get_or_create(
            category=category, code='BANDS', defaults={'name': 'Gold Bands', 'active': True},
        )
        uom, _ = UnitOfMeasure.objects.get_or_create(
            company=company, code='PCS', defaults={'name': 'Pieces', 'is_active': True},
        )
        item, _ = Item.objects.get_or_create(
            item_number='ITEM-GOLD-RING-001',
            defaults={
                'company': company, 'name': '22K Gold Band Ring', 'division': division,
                'special_group': special_group, 'category': category, 'subcategory': subcategory,
                'brand': brand, 'base_uom': uom, 'standard_cost': Decimal('62000'), 'status': 'active',
            },
        )
        ItemUnitOfMeasure.objects.get_or_create(
            item=item, uom=uom,
            defaults={'quantity_per_uom': Decimal('1'), 'is_sales_uom': True, 'is_inventory_uom': True},
        )
        variant, _ = ItemVariant.objects.get_or_create(
            item=item, code='22K-YELLOW',
            defaults={'name': '22K Yellow Gold', 'color': 'Yellow Gold', 'active': True},
        )
        sku, _ = SKU.objects.get_or_create(
            code='SKU-GOLD-RING-001',
            defaults={'item': item, 'variant': variant, 'location': location, 'cost': Decimal('62000'), 'price': Decimal('78000')},
        )
        Barcode.objects.get_or_create(
            value='8901000000011',
            defaults={'barcode_type': 'ean13', 'sku': sku, 'uom': uom, 'is_primary': True},
        )
        ItemLocation.objects.get_or_create(
            item=item, variant=variant, location=location,
            defaults={'reorder_point': Decimal('2'), 'reorder_quantity': Decimal('5'), 'active': True},
        )

        warehouse, _ = Warehouse.objects.get_or_create(
            code='MUM-WH',
            defaults={'company': company, 'name': 'Mumbai Central Warehouse', 'location_type': 'warehouse', 'status': 'active'},
        )
        zone, _ = WarehouseZone.objects.get_or_create(
            warehouse=warehouse, code='STORAGE',
            defaults={'name': 'Main storage', 'zone_type': 'bulk', 'status': 'active'},
        )
        BinLocation.objects.get_or_create(
            warehouse=warehouse, code='MUM-ST-01',
            defaults={'zone': zone, 'bin_type': 'bulk', 'capacity': Decimal('100'), 'is_active': True},
        )

        payment_term, _ = PaymentTerm.objects.get_or_create(
            company=company, code='NET30',
            defaults={'name': 'Net 30 days', 'due_days': 30, 'is_active': True},
        )
        payment_method, _ = PaymentMethod.objects.get_or_create(
            company=company, code='UPI',
            defaults={'name': 'UPI', 'method_type': 'upi', 'is_active': True},
        )
        Role.objects.get_or_create(name='Store Manager', defaults={'description': 'Manages showroom operations'})
        Employee.objects.get_or_create(
            employee_code='EMP-MUM-001',
            defaults={'full_name': 'Aarav Mehta', 'designation': 'Store Manager', 'is_active': True},
        )

        GSTState.objects.get_or_create(
            state_code='MH',
            defaults={'state_name': 'Maharashtra', 'gst_state_code': '27', 'is_active': True},
        )
        gst_group, _ = GSTGroup.objects.get_or_create(
            code='GST18', defaults={'description': 'Standard GST 18%', 'taxability': 'taxable',
                                    'effective_from': date(2026, 1, 1), 'status': 'active'},
        )
        GSTComponent.objects.get_or_create(
            code='cgst', defaults={'name': 'Central GST', 'recoverable': True, 'payable': True, 'receivable': True, 'status': 'active'},
        )
        GSTRate.objects.get_or_create(
            code='GST18', defaults={'description': 'GST 18 percent', 'cgst_rate': Decimal('9'), 'sgst_rate': Decimal('9'),
                                    'igst_rate': Decimal('18'), 'effective_from': date(2026, 1, 1), 'status': 'active'},
        )
        HSNCode.objects.get_or_create(
            code='7113', defaults={'description': 'Articles of jewellery', 'gst_group': gst_group,
                                   'effective_from': date(2026, 1, 1), 'status': 'active'},
        )
        SACCode.objects.get_or_create(
            code='998599', defaults={'description': 'Jewellery repair services', 'service_category': 'Repair',
                                     'gst_group': gst_group, 'effective_from': date(2026, 1, 1), 'status': 'active'},
        )
        SupplyType.objects.get_or_create(code='B2C', defaults={'description': 'Business to consumer', 'status': 'active'})

        CustomerFinanceProfile.objects.get_or_create(
            customer=customer,
            defaults={'payment_term': payment_term, 'payment_method': payment_method, 'credit_limit': Decimal('100000')},
        )
        VendorFinanceProfile.objects.get_or_create(
            vendor=supplier, defaults={'payment_term': payment_term, 'payment_method': payment_method},
        )

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
