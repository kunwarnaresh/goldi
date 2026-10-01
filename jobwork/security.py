"""Job work permissions, approval matrix, number series, setup access and the audit trail.

Roles are granted per tenant (``JobWorkRole``); tenant owners/admins hold every permission, but maker-checker still
applies to them unless setup allows self-approval. Screens, the API and services all ask ``require(actor, perm)``.
"""
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from inventory.engine import InventoryError
from inventory.models import NumberSeries
from inventory.services import Actor, actor_from_request  # noqa: F401 - re-exported for job work callers
from inventory.tenancy import is_tenant_admin

from .models import ApprovalRule, JobWorkAuditLog, JobWorkRole, JobWorkSetup


class JobWorkError(InventoryError):
    """A job work action was rejected. The message is safe to show to the user."""


PERMISSIONS = {
    'view': ('OPERATOR', 'SUPERVISOR', 'MANAGER', 'HEAD_OF_MANUFACTURING', 'QUALITY', 'FINANCE', 'TAX_ADMIN', 'AUDITOR'),
    'create': ('OPERATOR', 'SUPERVISOR', 'MANAGER', 'HEAD_OF_MANUFACTURING'),
    'execute': ('OPERATOR', 'SUPERVISOR', 'MANAGER', 'HEAD_OF_MANUFACTURING'),
    'approve': ('SUPERVISOR', 'MANAGER', 'HEAD_OF_MANUFACTURING'),
    'qc': ('QUALITY', 'SUPERVISOR', 'MANAGER'),
    'invoice': ('FINANCE', 'MANAGER'),
    'post_invoice': ('FINANCE',),
    'exception': ('SUPERVISOR', 'MANAGER', 'HEAD_OF_MANUFACTURING', 'FINANCE'),
    'reverse': ('MANAGER', 'HEAD_OF_MANUFACTURING'),
    'close': ('SUPERVISOR', 'MANAGER', 'HEAD_OF_MANUFACTURING'),
    'masters': ('MANAGER', 'HEAD_OF_MANUFACTURING'),
    'tax_admin': ('TAX_ADMIN',),
    'compliance': ('TAX_ADMIN', 'FINANCE', 'MANAGER'),
}
LABELS = {
    'create': 'create job work orders', 'execute': 'record job work movements', 'approve': 'approve job work documents',
    'qc': 'perform job work QC', 'invoice': 'enter job worker invoices', 'post_invoice': 'post job worker invoices',
    'exception': 'review job work exceptions', 'reverse': 'reverse posted job work documents', 'close': 'close job work orders',
    'masters': 'maintain job work masters', 'tax_admin': 'approve GST / compliance masters', 'compliance': 'prepare GST compliance returns',
}
# Approval seniority: a higher role satisfies a requirement for a lower one on the same ladder.
ROLE_RANK = {'OPERATOR': 0, 'SUPERVISOR': 1, 'MANAGER': 2, 'HEAD_OF_MANUFACTURING': 3}


def roles_of(actor):
    if not hasattr(actor, '_jw_roles'):
        actor._jw_roles = set(JobWorkRole.objects.filter(tenant=actor.tenant, user=actor.user, active=True).values_list('role', flat=True))
    return actor._jw_roles


def is_admin(actor):
    if not hasattr(actor, '_jw_admin'):
        actor._jw_admin = is_tenant_admin(actor.tenant, actor.user)
    return actor._jw_admin


def can(actor, permission):
    if permission not in PERMISSIONS:
        raise ValueError(f'Unknown job work permission: {permission}')
    return is_admin(actor) or bool(roles_of(actor) & set(PERMISSIONS[permission]))


def require(actor, permission):
    if not can(actor, permission):
        raise PermissionDenied(f'You are not allowed to {LABELS.get(permission, permission.replace("_", " "))}.')


def permission_map(actor):
    return {name: can(actor, name) for name in PERMISSIONS}


def has_role(actor, role):
    """True when the actor holds `role` (or a more senior role on the approval ladder)."""
    if not role or is_admin(actor):
        return True
    held = roles_of(actor)
    if role in held:
        return True
    rank = ROLE_RANK.get(role)
    return rank is not None and any(ROLE_RANK.get(r, -1) >= rank for r in held)


def required_role(tenant, metric, value):
    """The role the approval matrix demands for `value` of `metric` (the most senior matching band), or ''."""
    value = Decimal(str(value or 0))
    roles = [rule.required_role for rule in ApprovalRule.objects.filter(tenant=tenant, metric=metric, active=True)
             if value >= rule.min_value and (rule.max_value is None or value < rule.max_value)]
    if not roles:
        return ''
    return max(roles, key=lambda r: ROLE_RANK.get(r, 99))


def check_maker_checker(actor, maker, setup=None):
    setup = setup or get_setup(actor.tenant)
    if maker is not None and maker == actor.user and not setup.allow_self_approval:
        raise JobWorkError('Maker-checker: the approver must be different from the person who prepared the document.')


# ---------------------------------------------------------------------------
# Setup and number series (tenant-configurable through inventory NumberSeries)
# ---------------------------------------------------------------------------

SERIES = {
    'JOB_WORK_ORDER': 'JWO', 'SUBCONTRACT_ORDER': 'SCO', 'JOB_WORK_DISPATCH': 'JWD', 'DELIVERY_CHALLAN': 'DC', 'JOB_WORK_RECEIPT': 'JWR',
    'WIP_TRANSFER': 'WIPT', 'PROCESS_REPORT': 'JWP', 'JOB_WORK_QC': 'JWQ', 'LOSS_APPROVAL': 'JWX', 'DEBIT_NOTE': 'JWDN',
    'CREDIT_NOTE': 'JWCN', 'VENDOR_INVOICE': 'JWI', 'GST_SNAPSHOT': 'JWT', 'ITC04': 'ITC04', 'STOCK_ENTRY': 'JWS', 'COST_ENTRY': 'JWC',
    'AGREEMENT': 'JWA',
}


def next_number(tenant, document_type):
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
    setup = JobWorkSetup.objects.filter(tenant=tenant).first()
    if setup is None:
        try:
            with transaction.atomic():
                setup = JobWorkSetup.objects.create(tenant=tenant)
        except IntegrityError:
            setup = JobWorkSetup.objects.get(tenant=tenant)
    return setup


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit(actor, action, document_type, document_no='', *, order=None, old=None, new=None, reason='', source='WEB'):
    JobWorkAuditLog.objects.create(
        tenant=actor.tenant, user=actor.user, ip_address=getattr(actor, 'ip', None), device=getattr(actor, 'device', '') or '',
        action=action, document_type=document_type, document_no=document_no, order=order,
        old_value={k: str(v) for k, v in (old or {}).items()}, new_value={k: str(v) for k, v in (new or {}).items()},
        reason=(reason or '')[:250], source=getattr(actor, 'source', None) or source,
    )
