"""Jewellery Savings permissions, number series, setup access and the audit trail.

Roles are granted per tenant (``SavingsRole``); tenant owners/admins hold every permission. Screens, the posting
engine and the scheduled jobs all ask ``require(actor, permission)``, so there is one rule set.
"""
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction

from inventory.engine import InventoryError
from inventory.models import NumberSeries
from inventory.services import Actor, actor_from_request  # noqa: F401 - re-exported for savings callers
from inventory.tenancy import is_tenant_admin

from .models import SavingsRole, SavingsSetup, SchemeAuditLog


class SchemeError(InventoryError):
    """A savings action was rejected. The message is safe to show to the user."""


class ConfirmationRequired(SchemeError):
    """The action needs an explicit confirmation (e.g. a possible duplicate)."""


PERMISSIONS = {
    'view': ('OPERATOR', 'MANAGER', 'FINANCE', 'ADMINISTRATOR', 'AUDITOR'),
    'enroll': ('OPERATOR', 'MANAGER'),
    'collect': ('OPERATOR', 'MANAGER'),
    'approve_enrollment': ('MANAGER',),
    'reverse_payment': ('MANAGER',),
    'mature': ('MANAGER', 'OPERATOR'),
    'approve_benefit': ('MANAGER',),
    'override_benefit': ('MANAGER',),
    'redeem': ('OPERATOR', 'MANAGER'),
    'reverse_redemption': ('MANAGER',),
    'cancel': ('OPERATOR', 'MANAGER'),
    'approve_cancellation': ('MANAGER',),
    'refund_request': ('OPERATOR', 'MANAGER', 'FINANCE'),
    'approve_refund': ('FINANCE', 'MANAGER'),
    'pay_refund': ('FINANCE',),
    'adjust': ('MANAGER', 'FINANCE'),
    'approve_adjustment': ('FINANCE', 'MANAGER'),
    'configure': ('ADMINISTRATOR',),
    'approve_scheme': ('MANAGER', 'ADMINISTRATOR'),
    'view_kyc': ('MANAGER', 'FINANCE', 'AUDITOR'),
}
PERMISSION_LABELS = {
    'enroll': 'create scheme enrolments', 'collect': 'collect installments', 'approve_enrollment': 'approve enrolments',
    'reverse_payment': 'reverse scheme payments', 'approve_benefit': 'approve scheme benefits', 'override_benefit': 'override the calculated benefit',
    'redeem': 'redeem scheme entitlement', 'reverse_redemption': 'reverse redemptions', 'approve_cancellation': 'approve cancellations',
    'approve_refund': 'approve refunds', 'pay_refund': 'pay refunds', 'adjust': 'create scheme adjustments',
    'approve_adjustment': 'approve scheme adjustments', 'configure': 'configure schemes', 'approve_scheme': 'approve scheme versions',
}


def roles_of(actor):
    if not hasattr(actor, '_sav_roles'):
        actor._sav_roles = set(SavingsRole.objects.filter(tenant=actor.tenant, user=actor.user, active=True).values_list('role', flat=True))
    return actor._sav_roles


def is_admin(actor):
    if not hasattr(actor, '_sav_admin'):
        actor._sav_admin = is_tenant_admin(actor.tenant, actor.user)
    return actor._sav_admin


def can(actor, permission):
    if permission not in PERMISSIONS:
        raise ValueError(f'Unknown savings permission: {permission}')
    return is_admin(actor) or bool(roles_of(actor) & set(PERMISSIONS[permission]))


def require(actor, permission):
    if not can(actor, permission):
        raise PermissionDenied(f'You are not allowed to {PERMISSION_LABELS.get(permission, permission.replace("_", " "))}.')


def permission_map(actor):
    return {name: can(actor, name) for name in PERMISSIONS}


def maker_checker(actor, maker, setup, what):
    """The approver must differ from the maker unless self-approval is switched on."""
    if maker is not None and maker.pk == actor.user.pk and not setup.allow_self_approval:
        raise SchemeError(f'Maker-checker: the person who created the {what} cannot approve it.')


SERIES = {
    'SCHEME_RECEIPT': 'SRC', 'SCHEME_REVERSAL': 'SRV', 'BENEFIT_CALC': 'SBC', 'SCHEME_REDEMPTION': 'SRD', 'SCHEME_CANCELLATION': 'SCN',
    'SCHEME_REFUND': 'SRF', 'SCHEME_ADJUSTMENT': 'SAJ',
}


def next_number(tenant, document_type, prefix=None):
    """Next number of a tenant-configurable series (shared with inventory's NumberSeries table)."""
    prefix = prefix or SERIES[document_type]
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
    setup = SavingsSetup.objects.filter(tenant=tenant).first()
    if setup is None:
        try:
            with transaction.atomic():
                setup = SavingsSetup.objects.create(tenant=tenant)
        except IntegrityError:
            setup = SavingsSetup.objects.get(tenant=tenant)
    return setup


def audit(actor, action, document_type, document_no='', *, enrollment=None, old=None, new=None, reason=''):
    SchemeAuditLog.objects.create(
        tenant=actor.tenant, user=actor.user, ip_address=getattr(actor, 'ip', None), device=getattr(actor, 'device', '') or '',
        action=action, document_type=document_type, document_no=document_no, enrollment=enrollment,
        old_value={k: str(v) for k, v in (old or {}).items()}, new_value={k: str(v) for k, v in (new or {}).items()},
        reason=(reason or '')[:250],
    )


def mask(value, visible=4):
    value = (value or '').strip()
    if len(value) <= visible:
        return value
    return '•' * (len(value) - visible) + value[-visible:]
