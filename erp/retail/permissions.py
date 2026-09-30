"""POS permission catalog and the default staff roles built from it."""

CATALOG = [
    # (code, label, group)
    ('pos.sale.create', 'Create sale', 'Billing'),
    ('pos.payment.receive', 'Receive payment', 'Billing'),
    ('pos.receipt.print', 'Print receipt', 'Billing'),
    ('pos.bill.hold', 'Hold bill', 'Billing'),
    ('pos.bill.recall', 'Recall bill', 'Billing'),
    ('pos.customer.search', 'Customer search', 'Billing'),
    ('pos.discount.approve', 'Approve discount', 'Approvals'),
    ('pos.price_override.approve', 'Approve price override', 'Approvals'),
    ('pos.return.approve', 'Approve return', 'Approvals'),
    ('pos.exchange.approve', 'Approve exchange', 'Approvals'),
    ('pos.void.approve', 'Approve void', 'Approvals'),
    ('pos.refund.approve', 'Approve refund', 'Approvals'),
    ('pos.tax.change', 'Change tax', 'Administration'),
    ('pos.tender.setup', 'Change tender setup', 'Administration'),
    ('pos.staff.manage', 'Manage staff', 'Administration'),
    ('pos.shift.close', 'Close shift / cash up', 'Administration'),
    ('pos.report.view', 'View POS reports', 'Administration'),
]
CODES = [code for code, _, _ in CATALOG]
LABELS = {code: label for code, label, _ in CATALOG}

_CASHIER = ['pos.sale.create', 'pos.payment.receive', 'pos.receipt.print', 'pos.bill.hold', 'pos.bill.recall',
            'pos.customer.search']
_APPROVALS = ['pos.discount.approve', 'pos.price_override.approve', 'pos.return.approve', 'pos.exchange.approve',
              'pos.void.approve', 'pos.refund.approve']

# (code, name, base_role, pos_access, permissions)
DEFAULT_ROLES = [
    ('CASHIER', 'Cashier', 'cashier', True, _CASHIER),
    ('SALES', 'Sales Staff', 'sales_staff', True, ['pos.customer.search', 'pos.bill.hold', 'pos.bill.recall']),
    ('SUPERVISOR', 'Supervisor', 'manager', True, _CASHIER + ['pos.discount.approve', 'pos.return.approve',
                                                               'pos.exchange.approve', 'pos.shift.close']),
    ('MANAGER', 'Store Manager', 'manager', True, _CASHIER + _APPROVALS + ['pos.shift.close', 'pos.report.view',
                                                                            'pos.staff.manage']),
    ('POSADMIN', 'POS Admin', 'manager', True, list(CODES)),
    ('AUDITOR', 'Auditor', 'manager', False, ['pos.report.view']),
]

# Used for staff created before configurable roles existed (only the legacy `role` field is set).
LEGACY_ROLE_PERMISSIONS = {
    'cashier': _CASHIER,
    'sales_staff': ['pos.customer.search', 'pos.bill.hold', 'pos.bill.recall'],
    'manager': _CASHIER + _APPROVALS + ['pos.shift.close', 'pos.report.view'],
}


def staff_permissions(staff):
    role = staff.staff_role
    if role is not None:
        return set(role.permissions or []) if role.is_active else set()
    return set(LEGACY_ROLE_PERMISSIONS.get(staff.role, []))


def staff_has_pos_access(staff):
    role = staff.staff_role
    if role is not None:
        return role.is_active and role.pos_access
    return staff.role in LEGACY_ROLE_PERMISSIONS
