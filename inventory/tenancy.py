"""Tenant resolution and location-level security.

The tenant is always derived server-side from the authenticated user's membership - never from
a request parameter, header or payload - and every query in this app is filtered through it.
"""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.text import slugify

from .models import Location, LocationPermission, Tenant, TenantMembership, UnitOfMeasure

DEFAULT_UOMS = [('PCS', 'Pieces', 0), ('GM', 'Gram', 3), ('KG', 'Kilogram', 3), ('CT', 'Carat', 3)]


def get_membership(user, tenant=None):
    if not getattr(user, 'is_authenticated', False):
        return None
    memberships = TenantMembership.objects.select_related('tenant').filter(user=user, active=True, tenant__active=True)
    if tenant is not None:
        memberships = memberships.filter(tenant=tenant)
    return memberships.order_by('-is_default', 'id').first()


def get_current_tenant(request):
    """The tenant for this request, cached on the request object."""
    if not hasattr(request, '_inventory_membership'):
        request._inventory_membership = get_membership(request.user) or provision_legacy_user(request.user)
    membership = request._inventory_membership
    if membership is None:
        raise PermissionDenied('Your account is not linked to an inventory workspace.')
    return membership.tenant


def provision_legacy_user(user):
    """Accounts created before workspaces existed (or outside registration) get their own workspace on first use.
    Users who ever had a membership are left alone, so a deactivated member is still denied."""
    if not getattr(user, 'is_authenticated', False) or TenantMembership.objects.filter(user=user).exists():
        return None
    from erp.models import Company
    company = Company.objects.order_by('id').first()
    name = (company.company_name if company else '') or user.get_full_name() or user.get_username()
    with transaction.atomic():
        create_tenant_for_user(user, name)
    return get_membership(user)


def is_tenant_admin(tenant, user):
    membership = get_membership(user, tenant)
    return bool(membership and membership.is_admin)


def allowed_locations(tenant, user, action='view'):
    """Locations of `tenant` on which `user` may perform `action` (see LocationPermission.ACTIONS)."""
    if action not in LocationPermission.ACTIONS:
        raise ValueError(f'Unknown location action: {action}')
    membership = get_membership(user, tenant)
    locations = Location.objects.for_tenant(tenant)
    if membership is None:
        return locations.none()
    if membership.is_admin or membership.all_locations:
        return locations
    return locations.filter(user_permissions__user=user, **{f'user_permissions__can_{action}': True}).distinct()


def require_location(tenant, user, location, action):
    if location is None or location.tenant_id != tenant.id:
        raise PermissionDenied('Location not found.')
    if not allowed_locations(tenant, user, action).filter(pk=location.pk).exists():
        raise PermissionDenied(f'You are not allowed to {action.replace("_", " ")} at {location.code}.')


def approval_level(tenant, user, location):
    membership = get_membership(user, tenant)
    if membership is None:
        return -1
    if membership.is_admin:
        return 99
    permission = LocationPermission.objects.filter(tenant=tenant, user=user, location=location).first()
    return permission.approval_level if permission else 0


@transaction.atomic
def create_tenant_for_user(user, business_name):
    """Provision an isolated inventory workspace: tenant, owner membership, base UOMs and a transit location."""
    base = slugify(business_name)[:30] or 'workspace'
    code, suffix = base, 1
    while Tenant.objects.filter(code=code).exists():
        suffix += 1
        code = f'{base}-{suffix}'
    tenant = Tenant.objects.create(code=code, name=business_name)
    TenantMembership.objects.create(tenant=tenant, user=user, role='owner', all_locations=True, is_default=True)
    for uom_code, name, places in DEFAULT_UOMS:
        UnitOfMeasure.objects.create(tenant=tenant, code=uom_code, name=name, decimal_places=places, created_by=user)
    Location.objects.create(
        tenant=tenant, code='IN-TRANSIT', name='In Transit', location_type='TRANSIT', created_by=user,
        allow_purchase=False, allow_sales=False, allow_adjustment=False, allow_physical_count=False,
    )
    return tenant
