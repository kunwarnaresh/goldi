"""Manufacturing permissions, number series, setup access and the audit trail.

Roles are granted per tenant (``ManufacturingRole``); tenant owners/admins hold every permission.
Screens, the API and the posting engine all ask ``require(actor, permission)``, so there is one rule set.
"""
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from inventory.engine import InventoryError
from inventory.models import NumberSeries
from inventory.services import Actor, actor_from_request  # noqa: F401 - re-exported for manufacturing callers
from inventory.tenancy import is_tenant_admin

from .models import ManufacturingAuditLog, ManufacturingRole, ManufacturingSetup


class ManufacturingError(InventoryError):
    """A manufacturing action was rejected. The message is safe to show to the user."""


class ConfirmationRequired(ManufacturingError):
    """The action affects posted transactions and must be confirmed explicitly."""


PERMISSIONS = {
    'view': ('OPERATOR', 'SUPERVISOR', 'MANAGER', 'QUALITY', 'COSTING', 'FINANCE', 'AUDITOR', 'BOM_MAKER', 'BOM_REVIEWER', 'BOM_APPROVER'),
    'execute': ('OPERATOR', 'SUPERVISOR', 'MANAGER'),
    'plan': ('SUPERVISOR', 'MANAGER'),
    'create_order': ('SUPERVISOR', 'MANAGER'),
    'release': ('SUPERVISOR', 'MANAGER'),
    'approve_order': ('MANAGER',),
    'approve_scrap': ('SUPERVISOR', 'MANAGER'),
    'approve_output': ('SUPERVISOR', 'MANAGER'),
    'override_shortage': ('MANAGER',),
    'qc': ('QUALITY', 'SUPERVISOR', 'MANAGER'),
    'finish': ('SUPERVISOR', 'MANAGER'),
    'reverse': ('SUPERVISOR', 'MANAGER'),
    'approve_reversal': ('MANAGER',),
    'reopen': ('MANAGER',),
    'bom_make': ('BOM_MAKER', 'MANAGER'),
    'bom_review': ('BOM_REVIEWER', 'MANAGER'),
    'bom_approve': ('BOM_APPROVER', 'MANAGER'),
    'bom_certify': ('MANAGER',),
    'cost': ('COSTING', 'MANAGER', 'FINANCE'),
    'approve_cost': ('COSTING',),
    'finance': ('FINANCE', 'COSTING'),
    'setup': ('MANAGER',),
    'import': ('MANAGER',),
}
PERMISSION_LABELS = {
    'execute': 'record shop-floor transactions', 'release': 'release production orders', 'approve_order': 'approve production orders',
    'qc': 'perform quality control', 'finish': 'finish production orders', 'reverse': 'reverse posted entries',
    'approve_reversal': 'approve reversals', 'reopen': 'reopen finished orders', 'bom_make': 'maintain BOMs and routings',
    'bom_review': 'review BOMs and routings', 'bom_approve': 'approve BOMs and routings', 'bom_certify': 'certify BOMs and routings',
    'setup': 'change manufacturing setup', 'override_shortage': 'release orders with material shortage',
}


def roles_of(actor):
    if not hasattr(actor, '_mfg_roles'):
        actor._mfg_roles = set(ManufacturingRole.objects.filter(tenant=actor.tenant, user=actor.user, active=True)
                               .values_list('role', flat=True))
    return actor._mfg_roles


def is_admin(actor):
    if not hasattr(actor, '_mfg_admin'):
        actor._mfg_admin = is_tenant_admin(actor.tenant, actor.user)
    return actor._mfg_admin


def can(actor, permission):
    if permission not in PERMISSIONS:
        raise ValueError(f'Unknown manufacturing permission: {permission}')
    return is_admin(actor) or bool(roles_of(actor) & set(PERMISSIONS[permission]))


def require(actor, permission):
    if not can(actor, permission):
        raise PermissionDenied(f'You are not allowed to {PERMISSION_LABELS.get(permission, permission.replace("_", " "))}.')


def permission_map(actor):
    return {name: can(actor, name) for name in PERMISSIONS}


# ---------------------------------------------------------------------------
# Setup and number series
# ---------------------------------------------------------------------------

SERIES = {
    'PRODUCTION_ORDER': 'MO', 'BOM': 'BOM', 'BOM_VERSION': 'BOMV', 'ROUTING': 'RTG', 'PRODUCTION_JOURNAL': 'PJ',
    'CONSUMPTION_JOURNAL': 'CJ', 'OUTPUT_JOURNAL': 'OJ', 'CAPACITY_JOURNAL': 'CAPJ', 'SCRAP_JOURNAL': 'SJ', 'REWORK_ORDER': 'RWK',
    'SUBCONTRACT_ORDER': 'SUB', 'POSTING_BATCH': 'MPB', 'PICK_LIST': 'PCK', 'MATERIAL_ISSUE': 'MIS', 'PRODUCTION_QC': 'PQC',
    'PLANNING_RUN': 'PLN',
}
JOURNAL_SERIES = {'PRODUCTION': 'PRODUCTION_JOURNAL', 'CONSUMPTION': 'CONSUMPTION_JOURNAL', 'OUTPUT': 'OUTPUT_JOURNAL',
                  'CAPACITY': 'CAPACITY_JOURNAL', 'SCRAP': 'SCRAP_JOURNAL', 'REWORK': 'PRODUCTION_JOURNAL',
                  'FLUSHING': 'PRODUCTION_JOURNAL', 'SUBCONTRACT': 'PRODUCTION_JOURNAL'}


def next_number(tenant, document_type):
    """Next number of a tenant-configurable series (shared with inventory's NumberSeries table)."""
    prefix = SERIES[document_type]
    with transaction.atomic():
        series = NumberSeries.objects.select_for_update().filter(tenant=tenant, document_type=document_type).first()
        if series is None:
            try:
                with transaction.atomic():
                    NumberSeries.objects.create(tenant=tenant, document_type=document_type, prefix=prefix)
            except IntegrityError:
                pass
            series = NumberSeries.objects.select_for_update().get(tenant=tenant, document_type=document_type)
        number = f'{series.prefix}-{series.next_number:0{series.padding}d}'
        series.next_number += 1
        series.save(update_fields=['next_number', 'updated_at'])
        return number


def get_setup(tenant):
    setup = ManufacturingSetup.objects.filter(tenant=tenant).first()
    if setup is None:
        try:
            with transaction.atomic():
                setup = ManufacturingSetup.objects.create(tenant=tenant)
        except IntegrityError:
            setup = ManufacturingSetup.objects.get(tenant=tenant)
    return setup


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit(actor, action, document_type, document_no='', *, order=None, old=None, new=None, reason='', batch=None):
    ManufacturingAuditLog.objects.create(
        tenant=actor.tenant, user=actor.user, ip_address=getattr(actor, 'ip', None), device=getattr(actor, 'device', '') or '',
        action=action, document_type=document_type, document_no=document_no, order=order,
        old_value={k: str(v) for k, v in (old or {}).items()}, new_value={k: str(v) for k, v in (new or {}).items()},
        reason=(reason or '')[:250], batch=batch,
    )
