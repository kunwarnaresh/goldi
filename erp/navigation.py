"""Main sidebar menu: every workspace module with its drill-down sub-menus, exposed to templates as `main_menu`."""
from django.urls import NoReverseMatch, reverse

# A link is (url_name, label) or (url_name, label, args); url_name may also be a literal path starting with '/'.
# A menu's children are groups of (group_label or None, [links]).
WMS_MODULES = [
    ('transfer-orders', 'Transfer Orders'), ('warehouse-receipts', 'Warehouse Receipts'), ('putaways', 'Put-away'),
    ('picks', 'Picking'), ('shipments', 'Warehouse Shipments'), ('movements', 'Internal Movement'),
    ('replenishment', 'Replenishment'), ('adjustments', 'Inventory Adjustments'), ('item-journals', 'Item Journals'),
    ('stock-takes', 'Stock Takes'), ('cycle-counts', 'Cycle Counts'), ('quality', 'QC and Quarantine'),
    ('returns', 'Returns'), ('cross-dock', 'Cross Dock'),
]

SECTIONS = [
    ('Workspace', [
        ('Dashboard', 'fa-gauge-high', 'dashboard', []),
    ]),
    ('Analytics Center', [
        ('Executive dashboard', 'fa-chart-line', 'analytics:dashboard', []),
        ('Report explorer', 'fa-table-columns', 'analytics:explorer', []),
    ]),
    ('Operations', [
        ('Finance', 'fa-scale-balanced', None, [
            (None, [('finance_management', 'Finance overview')]),
            ('Vouchers', [
                ('master_create', 'Voucher entry', ['finance-vouchers']),
                ('finance_operations', 'Voucher operations'),
                ('master_crud', 'Vouchers', ['finance-vouchers']),
                ('master_crud', 'Posted vouchers', ['posted-vouchers']),
            ]),
            ('Journals', [
                ('master_crud', 'Journal batches', ['finance-journal-batches']),
                ('master_crud', 'Journal templates', ['finance-journal-templates']),
                ('master_crud', 'Voucher types', ['finance-voucher-types']),
            ]),
            ('Accounts', [
                ('master_crud', 'Chart of accounts', ['gl-accounts']),
                ('master_crud', 'Bank accounts', ['bank-accounts']),
                ('master_crud', 'Cost centers', ['cost-centers']),
                ('master_crud', 'Payment terms', ['payment-terms']),
                ('master_crud', 'Payment methods', ['payment-methods']),
                ('master_crud', 'Customer posting groups', ['customer-posting-groups']),
                ('master_crud', 'Vendor posting groups', ['vendor-posting-groups']),
            ]),
            ('Reports', [('trial_balance_report', 'Trial balance')]),
        ]),
        ('Sales & Receivables', 'fa-file-invoice-dollar', None, [
            (None, [('sales_receivables', 'Sales overview'), ('pos', 'Point of sale')]),
            ('Documents', [
                ('customer_list', 'Customers'),
                ('quotation_list', 'Quotations'),
                ('quotation_create', 'New quotation'),
                ('sales_receivables_placeholder', 'Proforma invoices', ['proforma']),
                ('sales_order_list', 'Sales orders'),
                ('sales_receivables_placeholder', 'Delivery challans', ['delivery_challans']),
                ('invoice_list', 'Sales invoices'),
                ('sales_receivables_placeholder', 'Credit notes', ['credit_notes']),
            ]),
            ('Receivables', [
                ('payment_receipt_list', 'Payment receipts'),
                ('payment_receipt_create', 'New payment receipt'),
            ]),
            ('Counter', [
                ('sales_approval', 'Sales approvals'),
                ('sales_return', 'Sales returns'),
                ('exchange', 'Old gold exchange'),
                ('repair_order_list', 'Repair orders'),
                ('repair_order_create', 'New repair order'),
            ]),
        ]),
        ('Purchase & Payables', 'fa-cart-flatbed', None, [
            (None, [('purchase_payables', 'Purchase overview')]),
            ('Vendors', [
                ('purchase_payables_placeholder', 'Vendor leads', ['vendor-leads']),
                ('supplier_list', 'Suppliers'),
                ('purchase_payables_placeholder', 'Hire the best vendors', ['hire-best-vendors']),
            ]),
            ('Documents', [
                ('purchase_order_list', 'Purchase orders'),
                ('purchase_order_create', 'New purchase order'),
                ('purchase_invoice_list', 'Purchase invoices'),
                ('purchase_payables_placeholder', 'Debit notes', ['debit-notes']),
            ]),
            ('Payables', [
                ('vendor_payment_list', 'Vendor payments'),
                ('vendor_payment_create', 'New vendor payment'),
            ]),
        ]),
        ('AWMS', 'fa-warehouse', None, [
            (None, [('wms_hub', 'Warehouse hub'), ('warehouse_management', 'Warehouses & bins')]),
            ('Inbound', [('wms_module', label, [slug]) for slug, label in WMS_MODULES[1:3]]),
            ('Outbound', [('wms_module', label, [slug]) for slug, label in WMS_MODULES[3:5]]),
            ('Internal', [('wms_module', label, [slug]) for slug, label in (WMS_MODULES[:1] + WMS_MODULES[5:9])]),
            ('Counting & quality', [('wms_module', label, [slug]) for slug, label in WMS_MODULES[9:]]),
            ('Reports', [('wms_reports', 'Warehouse reports')]),
        ]),
        ('GST', 'fa-receipt', None, [
            (None, [('gst_control_center', 'GST control center'), ('gst_management', 'GST slabs')]),
            ('Setup', [
                ('master_crud', 'Registrations', ['gst-registrations']),
                ('master_crud', 'GST rates', ['gst-rates']),
                ('master_crud', 'GST groups', ['gst-groups']),
                ('master_crud', 'HSN codes', ['hsn-codes']),
                ('master_crud', 'SAC codes', ['sac-codes']),
                ('master_crud', 'Tax rules', ['gst-tax-rules']),
            ]),
            ('Registers', [('master_crud', 'GST ledger', ['gst-ledger'])]),
        ]),
        ('Metal Rates', 'fa-coins', None, [
            (None, [
                ('metal_rate_list', 'Metal rates'),
                ('metal_rate_create', 'New metal rate'),
                ('metal_price_simulator', 'Price simulator'),
            ]),
        ]),
    ]),
    ('Supply chain', [
        ('Inventory', 'fa-boxes-stacked', 'inventory', []),
        ('Manufacturing', 'fa-industry', 'manufacturing', []),
        ('Job Work', 'fa-people-carry-box', 'jobwork', []),
        ('Jewellery Savings', 'fa-piggy-bank', 'savings', []),
    ]),
    ('Setup', [
        ('Masters', 'fa-database', None, [
            (None, [('master_catalog', 'Master catalog'), ('master_management', 'Master management')]),
            ('Items', [
                ('product_list', 'Products'),
                ('master_crud', 'Retail items', ['retail-items']),
                ('master_crud', 'Categories', ['categories']),
                ('master_crud', 'Units of measure', ['units-of-measure']),
                ('master_crud', 'Barcodes', ['retail-barcodes']),
            ]),
            ('Parties', [
                ('customer_list', 'Customers'),
                ('supplier_list', 'Suppliers'),
            ]),
            ('Channels', [
                ('master_crud', 'Brands', ['brands']),
                ('master_crud', 'Channels', ['channels']),
            ]),
            ('Data', [('import_center', 'Import center'), ('stock_ledger', 'Legacy stock ledger')]),
        ]),
        ('Stores & POS', 'fa-store', None, [
            (None, [('retail_home', 'Stores & POS overview')]),
            ('Organisation', [
                ('retail_locations', 'Locations'),
                ('retail_stores', 'Stores'),
                ('retail_store_new', 'New store'),
            ]),
            ('Staff', [
                ('retail_staff', 'Staff'),
                ('retail_staff_new', 'New staff'),
                ('retail_roles', 'POS roles'),
                ('retail_assignments', 'Staff POS access'),
            ]),
            ('POS setup', [
                ('retail_terminals', 'POS terminals'),
                ('retail_tenders', 'Tender master'),
                ('retail_store_tenders', 'Store tenders'),
            ]),
            ('Operations', [
                ('retail_sessions', 'POS sessions'),
                ('retail_shifts', 'POS shifts'),
                ('retail_imports', 'Excel import / export'),
                ('retail_audit', 'Audit trail'),
            ]),
        ]),
        ('Admin', 'fa-user-shield', None, [
            ('Organisation', [
                ('master_crud', 'Companies', ['companies']),
                ('master_crud', 'Branches', ['branches']),
                ('retail_stores', 'Stores'),
                ('retail_locations', 'Locations'),
                ('master_crud', 'Departments', ['departments']),
            ]),
            ('Users & access', [
                ('role_list', 'ERP roles'),
                ('employee_list', 'Employees'),
                ('retail_staff', 'POS staff'),
                ('retail_roles', 'POS roles'),
                ('/admin/auth/user/', 'User accounts'),
                ('/admin/auth/group/', 'User groups'),
            ]),
            ('Configuration', [
                ('master_crud', 'Number series', ['number-series']),
                ('invoice_settings', 'Invoice settings'),
                ('import_center', 'Data imports'),
            ]),
            ('System', [('/admin/', 'Django admin')]),
        ]),
    ]),
]


def _module_menu(app):
    """Re-use the in-module menus the supply-chain apps already define, so both stay in sync."""
    from inventory.views import menu_links as inventory_links
    from jobwork.views import menu_links as jobwork_links
    from manufacturing.views import menu_links as manufacturing_links
    from savings.views import menu_links as savings_links
    groups = {'inventory': inventory_links, 'manufacturing': manufacturing_links,
              'jobwork': jobwork_links, 'savings': savings_links}[app]()
    if app == 'inventory':
        groups = groups + [('Legacy', [(reverse('stock_ledger'), 'Stock ledger')])]
    return [{'label': group, 'links': [{'url': url, 'label': label} for url, label in links]} for group, links in groups]


def _resolve(link):
    name, label, *rest = link
    if name.startswith('/'):
        return {'url': name, 'label': label}
    try:
        return {'url': reverse(name, args=rest[0] if rest else None), 'label': label}
    except NoReverseMatch:
        return None


def build_menu(path):
    sections = []
    for title, menus in SECTIONS:
        items = []
        for label, icon, target, children in menus:
            if target in ('inventory', 'manufacturing', 'jobwork', 'savings'):
                groups = _module_menu(target)
                url = reverse(f'{target}_dashboard')
            else:
                groups = [{'label': g, 'links': [r for r in map(_resolve, links) if r]} for g, links in children]
                url = reverse(target) if target else None
            items.append({'label': label, 'icon': icon, 'url': url, 'groups': groups})
        sections.append({'title': title, 'items': items})

    # Highlight the most specific link whose URL is a prefix of the current path, and open its menu.
    best, best_len = None, 0
    for section in sections:
        for item in section['items']:
            candidates = [(item, item)] if item['url'] else []
            candidates += [(item, link) for group in item['groups'] for link in group['links']]
            for owner, link in candidates:
                url = link['url'].split('?')[0]
                matches = path == url or (url != '/' and path.startswith(url))
                if matches and len(url) > best_len:
                    best, best_len = (owner, link), len(url)
    if best:
        best[0]['open'] = True
        best[1]['active'] = True
    return sections


def main_menu(request):
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    return {'main_menu': build_menu(request.path)}
