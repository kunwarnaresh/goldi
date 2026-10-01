"""Manufacturing workflows: BOM / routing maker-checker, production order lifecycle, material availability,
reservation, picking, shop floor execution, QC, rework, subcontracting, HUID and planning.

Every stock or value change is delegated to ``manufacturing.engine`` (which in turn calls the central
inventory and finance engines); this module only validates, orchestrates and audits.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone as dj_timezone

from inventory.engine import InventoryError, InventoryPostingEngine, available_qty
from inventory.models import InventoryBalance, InventoryReservation, Item, JewelleryUnit, Location
from inventory.services import create_transfer_order, create_transfer_request, estimated_unit_cost, find_unit

from . import engine as eng
from .calc import (
    D, ZERO, FormulaError, component_requirement, money, operation_capacity_minutes, per_piece_requirement, q3, schedule_backward,
    schedule_forward,
)
from .models import (
    BOMLine, BOMVersion, ItemManufacturingProfile, OperationEvent, OutputUnit, PlanningRun, PlanningSuggestion, ProductionBOM,
    ProductionOrder, ProductionOrderComponent, ProductionOrderLine, ProductionOrderRoutingLine, ProductionPickLine, ProductionPickList,
    ProductionQC, ProductionQCLine, ProductionRework, ProductionReworkLine, Routing, RoutingLine, RoutingVersion, SubcontractLine,
    SubcontractOrder,
)
from .security import ConfirmationRequired, ManufacturingError, audit, can, get_setup, next_number, require


def _get(model, actor, pk):
    obj = model.objects.filter(tenant=actor.tenant, pk=getattr(pk, 'pk', pk)).first()
    if obj is None:
        raise ManufacturingError(f'{model._meta.verbose_name.title()} not found.')
    return obj


def profile_of(item):
    return ItemManufacturingProfile.objects.filter(item=item).first()


# ---------------------------------------------------------------------------
# BOM and routing masters (versioned, maker-checker)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_bom(actor, *, item, bom_name='', bom_no=None, sku=None, variant=None, location=None, base_quantity=1, effective_from=None,
               expected_net_weight=0, expected_gross_weight=0, expected_stone_weight=0, description=''):
    require(actor, 'bom_make')
    if item.tenant_id != actor.tenant.id:
        raise ManufacturingError('Item not found.')
    bom = ProductionBOM.objects.create(
        tenant=actor.tenant, bom_no=bom_no or next_number(actor.tenant, 'BOM'), bom_name=bom_name or item.description, item=item,
        sku=sku, variant=variant, location=location, description=description, created_by=actor.user,
    )
    version = BOMVersion.objects.create(
        tenant=actor.tenant, bom=bom, version_no=1, base_quantity=D(base_quantity) or 1, base_uom=item.base_uom,
        effective_from=effective_from or dj_timezone.localdate(), expected_net_weight=D(expected_net_weight),
        expected_gross_weight=D(expected_gross_weight), expected_stone_weight=D(expected_stone_weight), created_by=actor.user,
    )
    profile, _ = ItemManufacturingProfile.objects.get_or_create(tenant=actor.tenant, item=item,
                                                                defaults={'manufacturing_type': 'MANUFACTURED', 'created_by': actor.user})
    if profile.default_bom_id is None:
        profile.default_bom = bom
        profile.save(update_fields=['default_bom', 'updated_at'])
    audit(actor, 'create', 'BOM', bom.bom_no, new={'item': item.item_no, 'version': 1})
    return bom, version


def _draft(version):
    if not version.editable:
        raise ManufacturingError(f'{version} is {version.get_status_display().lower()}. Create a new version to change it.')


@transaction.atomic
def save_bom_line(actor, version, *, line=None, **fields):
    require(actor, 'bom_make')
    version = BOMVersion.objects.select_for_update().get(pk=version.pk, tenant=actor.tenant)
    _draft(version)
    if line is None:
        last = version.lines.order_by('-line_no').values_list('line_no', flat=True).first() or 0
        line = BOMLine(tenant=actor.tenant, version=version, line_no=fields.pop('line_no', None) or last + 10, created_by=actor.user)
    for key, value in fields.items():
        setattr(line, key, value)
    item = line.component_item
    if item is not None:
        line.uom = line.uom or item.base_uom
        line.metal = line.metal or (item.metal if item.metal in ('GOLD', 'SILVER', 'PLATINUM') else '')
        line.purity = line.purity or item.purity
        line.description = line.description or item.description
    try:
        line.full_clean(exclude=['version', 'tenant'])
    except Exception as exc:
        raise ManufacturingError('; '.join(getattr(exc, 'messages', [str(exc)])))
    line.updated_by = actor.user
    line.save()
    audit(actor, 'update', 'BOM', version.bom.bom_no, new={'version': version.code, 'line': line.line_no, 'item': item.item_no})
    return line


@transaction.atomic
def delete_bom_line(actor, line):
    require(actor, 'bom_make')
    _draft(line.version)
    audit(actor, 'delete_line', 'BOM', line.version.bom.bom_no, old={'line': line.line_no, 'item': line.component_item.item_no})
    line.delete()


@transaction.atomic
def new_bom_version(actor, bom, *, copy_from=None, effective_from=None, change_note=''):
    require(actor, 'bom_make')
    bom = ProductionBOM.objects.select_for_update().get(pk=bom.pk, tenant=actor.tenant)
    source = copy_from or bom.versions.order_by('-version_no').first()
    number = (bom.versions.order_by('-version_no').values_list('version_no', flat=True).first() or 0) + 1
    version = BOMVersion.objects.create(
        tenant=actor.tenant, bom=bom, version_no=number, effective_from=effective_from or dj_timezone.localdate(), change_note=change_note,
        base_quantity=source.base_quantity if source else 1, base_uom=source.base_uom if source else bom.item.base_uom,
        expected_net_weight=source.expected_net_weight if source else 0, expected_gross_weight=source.expected_gross_weight if source else 0,
        expected_stone_weight=source.expected_stone_weight if source else 0, created_by=actor.user,
    )
    if source is not None:
        for line in source.lines.all():
            line.pk, line.id, line.version, line.created_by = None, None, version, actor.user
            line.save()
    audit(actor, 'new_version', 'BOM', bom.bom_no, new={'version': version.code, 'copied_from': source.code if source else ''})
    return version


@transaction.atomic
def create_routing(actor, *, description, item=None, routing_no=None, effective_from=None):
    require(actor, 'bom_make')
    routing = Routing.objects.create(tenant=actor.tenant, routing_no=routing_no or next_number(actor.tenant, 'ROUTING'),
                                     description=description, item=item, created_by=actor.user)
    version = RoutingVersion.objects.create(tenant=actor.tenant, routing=routing, version_no=1,
                                            effective_from=effective_from or dj_timezone.localdate(), created_by=actor.user)
    if item is not None:
        profile, _ = ItemManufacturingProfile.objects.get_or_create(tenant=actor.tenant, item=item,
                                                                    defaults={'manufacturing_type': 'MANUFACTURED', 'created_by': actor.user})
        if profile.default_routing_id is None:
            profile.default_routing = routing
            profile.save(update_fields=['default_routing', 'updated_at'])
    audit(actor, 'create', 'ROUTING', routing.routing_no, new={'item': item.item_no if item else ''})
    return routing, version


@transaction.atomic
def save_routing_line(actor, version, *, line=None, **fields):
    require(actor, 'bom_make')
    version = RoutingVersion.objects.select_for_update().get(pk=version.pk, tenant=actor.tenant)
    _draft(version)
    if line is None:
        line = RoutingLine(tenant=actor.tenant, version=version, created_by=actor.user)
    standard = fields.get('standard_operation')
    if standard is not None and line.pk is None:
        for name in ('work_center', 'machine_center', 'setup_time', 'run_time', 'wait_time', 'move_time', 'queue_time'):
            fields.setdefault(name, getattr(standard, name))
        fields.setdefault('description', standard.description)
        fields.setdefault('quality_check_required', standard.quality_check_required)
    for key, value in fields.items():
        setattr(line, key, value)
    if not line.sequence:
        line.sequence = int(line.operation_no) if str(line.operation_no).isdigit() else (version.lines.count() + 1) * 10
    try:
        line.full_clean(exclude=['version', 'tenant'])
    except Exception as exc:
        raise ManufacturingError('; '.join(getattr(exc, 'messages', [str(exc)])))
    line.updated_by = actor.user
    line.save()
    audit(actor, 'update', 'ROUTING', version.routing.routing_no, new={'version': version.code, 'operation': line.operation_no})
    return line


@transaction.atomic
def delete_routing_line(actor, line):
    require(actor, 'bom_make')
    _draft(line.version)
    audit(actor, 'delete_line', 'ROUTING', line.version.routing.routing_no, old={'operation': line.operation_no})
    line.delete()


@transaction.atomic
def new_routing_version(actor, routing, *, effective_from=None, change_note=''):
    require(actor, 'bom_make')
    routing = Routing.objects.select_for_update().get(pk=routing.pk, tenant=actor.tenant)
    source = routing.versions.order_by('-version_no').first()
    version = RoutingVersion.objects.create(tenant=actor.tenant, routing=routing, version_no=(source.version_no if source else 0) + 1,
                                            effective_from=effective_from or dj_timezone.localdate(), change_note=change_note,
                                            created_by=actor.user)
    if source is not None:
        for line in source.lines.all():
            line.pk, line.id, line.version, line.created_by = None, None, version, actor.user
            line.save()
    audit(actor, 'new_version', 'ROUTING', routing.routing_no, new={'version': version.code})
    return version


def validate_bom_version(version):
    errors = []
    lines = list(version.lines.select_related('component_item', 'uom'))
    if not lines:
        errors.append('A BOM needs at least one component line.')
    parent = version.bom.item
    sample = {'net_weight': version.expected_net_weight or 1, 'gross_weight': version.expected_gross_weight or 1,
              'stone_weight': version.expected_stone_weight, 'wastage_percent': 0, 'qty': 1}
    for line in lines:
        if line.component_item_id == parent.id:
            errors.append(f'Line {line.line_no}: a BOM cannot contain its own parent item.')
        if line.component_type not in ('BY_PRODUCT', 'CO_PRODUCT', 'SCRAP') and line.consumption_basis != 'FORMULA' and line.quantity <= 0:
            errors.append(f'Line {line.line_no}: quantity must be greater than zero.')
        if line.component_item.blocked or not line.component_item.active:
            errors.append(f'Line {line.line_no}: {line.component_item.item_no} is blocked or inactive.')
        if line.consumption_basis == 'FORMULA':
            try:
                per_piece_requirement(basis='FORMULA', quantity=line.quantity, formula=line.formula, parent=sample)
            except FormulaError as exc:
                errors.append(f'Line {line.line_no}: {exc}')
        if _reaches(line.component_item, parent, depth=0):
            errors.append(f'Line {line.line_no}: circular BOM - {line.component_item.item_no} (indirectly) uses {parent.item_no}.')
    return errors


def _reaches(item, target, depth):
    if depth > 12:
        return False
    for bom in ProductionBOM.objects.filter(tenant=item.tenant, item=item):
        version = bom.active_version() or bom.versions.order_by('-version_no').first()
        if version is None:
            continue
        for line in version.lines.select_related('component_item'):
            if line.component_item_id == target.id or _reaches(line.component_item, target, depth + 1):
                return True
    return False


def validate_routing_version(version):
    errors = []
    lines = list(version.lines.select_related('work_center'))
    if not lines:
        errors.append('A routing needs at least one operation.')
    for line in lines:
        if not line.work_center.usable:
            errors.append(f'Operation {line.operation_no}: work centre {line.work_center.code} is blocked or inactive.')
        if line.subcontracting and line.subcontractor is None:
            errors.append(f'Operation {line.operation_no}: select the subcontractor.')
    return errors


TRANSITIONS = {
    'submit': (('DRAFT',), 'SUBMITTED', 'bom_make'),
    'review': (('SUBMITTED',), 'REVIEWED', 'bom_review'),
    'approve': (('REVIEWED',), 'APPROVED', 'bom_approve'),
    'certify': (('APPROVED',), 'CERTIFIED', 'bom_certify'),
    'reject': (('SUBMITTED', 'REVIEWED', 'APPROVED'), 'DRAFT', 'bom_review'),
    'block': (('CERTIFIED',), 'BLOCKED', 'bom_certify'),
    'unblock': (('BLOCKED',), 'CERTIFIED', 'bom_certify'),
    'archive': (('DRAFT', 'BLOCKED', 'EXPIRED', 'CERTIFIED'), 'ARCHIVED', 'bom_certify'),
}


@transaction.atomic
def transition_version(actor, version, action, *, note=''):
    """Maker-checker workflow shared by BOM and routing versions: Draft -> Submitted -> Reviewed -> Approved -> Certified."""
    if action not in TRANSITIONS:
        raise ManufacturingError('Unknown workflow action.')
    sources, target, permission = TRANSITIONS[action]
    require(actor, permission)
    model = type(version)
    version = model.objects.select_for_update().get(pk=version.pk, tenant=actor.tenant)
    if version.status not in sources:
        raise ManufacturingError(f'{version} is {version.get_status_display().lower()}; cannot {action}.')
    setup = get_setup(actor.tenant)
    doc_type, doc_no = ('BOM', version.bom.bom_no) if model is BOMVersion else ('ROUTING', version.routing.routing_no)
    if action == 'submit':
        errors = validate_bom_version(version) if model is BOMVersion else validate_routing_version(version)
        if errors:
            raise ManufacturingError(' '.join(errors))
    if action in ('review', 'approve', 'certify') and not setup.allow_self_approval:
        makers = {version.submitted_by_id, version.created_by_id} - {None}
        if actor.user.pk in makers:
            raise ManufacturingError('Maker-checker: the person who created or submitted this version cannot review, approve or certify it.')
    if action == 'approve' and not setup.allow_self_approval and version.reviewed_by_id == actor.user.pk:
        raise ManufacturingError('Maker-checker: the reviewer cannot also approve.')
    now = dj_timezone.now()
    old = version.status
    stamp = {'submit': ('submitted_by', 'submitted_at'), 'review': ('reviewed_by', 'reviewed_at'),
             'approve': ('approved_by', 'approved_at'), 'certify': ('certified_by', 'certified_at')}.get(action)
    if stamp:
        setattr(version, stamp[0], actor.user)
        setattr(version, stamp[1], now)
    if action == 'reject':
        version.submitted_by = version.reviewed_by = version.approved_by = None
        version.submitted_at = version.reviewed_at = version.approved_at = None
    if action == 'certify':
        siblings = model.objects.filter(status='CERTIFIED', effective_to__isnull=True, effective_from__lt=version.effective_from)
        siblings = siblings.filter(bom=version.bom) if model is BOMVersion else siblings.filter(routing=version.routing)
        for sibling in siblings:
            sibling.effective_to = version.effective_from - timedelta(days=1)
            if sibling.effective_to < dj_timezone.localdate():
                sibling.status = 'EXPIRED'
            sibling.save(update_fields=['effective_to', 'status', 'updated_at'])
    version.status = target
    if note:
        version.change_note = note[:250]
    version.updated_by = actor.user
    version.save()
    audit(actor, action, doc_type, doc_no, old={'version': version.code, 'status': old}, new={'status': target}, reason=note)
    return version


def component_unit_cost(tenant, line_or_component, location=None, depth=0):
    """Standard unit cost of a component: BOM override, else current inventory cost, else its own certified BOM rolled up."""
    item = line_or_component.component_item if isinstance(line_or_component, BOMLine) else line_or_component.item
    override = getattr(line_or_component, 'component_cost', ZERO)
    if override:
        return D(override)
    location = location or getattr(line_or_component, 'location', None)
    if location is not None:
        cost = estimated_unit_cost(tenant, item, getattr(line_or_component, 'variant', None), location)
        if cost:
            return D(cost)
    if item.standard_cost:
        return item.standard_cost
    if depth < 6:
        bom = ProductionBOM.objects.filter(tenant=tenant, item=item).first()
        version = bom.active_version() if bom else None
        if version is not None:
            return bom_cost_rollup(tenant, version, depth=depth + 1)['unit_total']
    return ZERO


def bom_cost_rollup(tenant, version, *, quantity=1, routing_version=None, depth=0):
    """Standard cost per piece rolled up from components (incl. scrap/loss) and the routing."""
    parent = _parent_weights(version, version.bom.item)
    rows, material = [], ZERO
    for line in version.lines.select_related('component_item', 'uom', 'location'):
        if line.component_type in ('BY_PRODUCT', 'CO_PRODUCT', 'SCRAP'):
            continue
        per_piece = per_piece_requirement(basis=line.consumption_basis, quantity=line.quantity, base_quantity=version.base_quantity,
                                          formula=line.formula, parent=parent)
        req = component_requirement(per_piece=per_piece, production_qty=quantity, scrap_percent=line.scrap_percent,
                                    loss_percent=line.expected_loss_percent, fixed_scrap=line.fixed_scrap_qty, fixed_loss=line.expected_loss_qty)
        unit_cost = component_unit_cost(tenant, line, depth=depth)
        cost = money(req['expected'] * unit_cost)
        material += cost
        rows.append({'line': line, 'per_piece': q3(per_piece), 'gross': req['gross'], 'scrap': req['scrap'], 'expected': req['expected'],
                     'unit_cost': unit_cost, 'cost': cost})
    if routing_version is None:
        profile = profile_of(version.bom.item)
        routing = profile.default_routing if profile and profile.default_routing_id else Routing.objects.filter(tenant=tenant, item=version.bom.item).first()
        routing_version = routing.active_version() if routing else None
    labour = machine = overhead = ZERO
    if routing_version is not None:
        for line in routing_version.lines.select_related('work_center', 'machine_center'):
            minutes = operation_capacity_minutes(setup_time=line.setup_time, run_time=line.run_time, quantity=quantity,
                                                 efficiency=line.efficiency, concurrent_capacity=line.concurrent_capacity)
            hours = minutes['total'] / 60
            rates = line.rates()
            labour += money(hours * rates['labour'])
            machine += money(hours * rates['machine'])
            overhead += money(hours * rates['overhead'])
    total = material + labour + machine + overhead
    return {'rows': rows, 'material': material, 'labour': labour, 'machine': machine, 'overhead': overhead, 'total': total,
            'unit_total': money(total / D(quantity)) if D(quantity) else ZERO}


def _parent_weights(version, item, qty=1):
    profile = profile_of(item)
    return {'net_weight': version.expected_net_weight or (profile.net_metal_weight if profile else ZERO),
            'gross_weight': version.expected_gross_weight or (profile.gross_weight if profile else ZERO),
            'stone_weight': version.expected_stone_weight or (profile.stone_weight if profile else ZERO),
            'wastage_percent': profile.wastage_percent if profile else ZERO, 'qty': qty}


def explode_bom(tenant, version, quantity=1, level=1, max_level=8):
    """Multi-level BOM explosion: [(level, line, requirement, sub_version)]."""
    rows = []
    parent = _parent_weights(version, version.bom.item, quantity)
    for line in version.lines.select_related('component_item', 'uom'):
        per_piece = per_piece_requirement(basis=line.consumption_basis, quantity=line.quantity, base_quantity=version.base_quantity,
                                          formula=line.formula, parent=parent)
        req = component_requirement(per_piece=per_piece, production_qty=quantity, scrap_percent=line.scrap_percent,
                                    loss_percent=line.expected_loss_percent, fixed_scrap=line.fixed_scrap_qty, fixed_loss=line.expected_loss_qty)
        sub = ProductionBOM.objects.filter(tenant=tenant, item=line.component_item).first()
        sub_version = sub.active_version() if sub else None
        rows.append((level, line, req['expected'], sub_version))
        if sub_version is not None and level < max_level:
            rows.extend(explode_bom(tenant, sub_version, req['expected'] / (sub_version.base_quantity or 1), level + 1, max_level))
    return rows


def where_used(tenant, item):
    return BOMLine.objects.filter(tenant=tenant, component_item=item).select_related('version__bom__item').order_by('version__bom__bom_no')


# ---------------------------------------------------------------------------
# Production orders: create, refresh, approve, release, cancel
# ---------------------------------------------------------------------------

def resolve_bom(tenant, item, on_date, bom=None, version=None):
    if version is not None:
        if version.status != 'CERTIFIED':
            raise ManufacturingError(f'{version} is not certified. Only certified BOM versions can be used on production orders.')
        return version.bom, version
    if bom is None:
        profile = profile_of(item)
        bom = profile.default_bom if profile and profile.default_bom_id else None
        bom = bom or next((b for b in ProductionBOM.objects.filter(tenant=tenant, item=item, blocked=False) if b.active_version(on_date)), None)
    if bom is None:
        raise ManufacturingError(f'{item.item_no} has no certified production BOM.')
    if bom.blocked:
        raise ManufacturingError(f'BOM {bom.bom_no} is blocked.')
    version = bom.active_version(on_date)
    if version is None:
        raise ManufacturingError(f'BOM {bom.bom_no} has no certified version effective on {on_date}.')
    return bom, version


def resolve_routing(tenant, item, on_date, routing=None, version=None):
    if version is not None:
        if version.status != 'CERTIFIED':
            raise ManufacturingError(f'{version} is not certified.')
        return version.routing, version
    if routing is None:
        profile = profile_of(item)
        routing = profile.default_routing if profile and profile.default_routing_id else None
        routing = routing or next((r for r in Routing.objects.filter(tenant=tenant, item=item, blocked=False) if r.active_version(on_date)), None)
    if routing is None:
        return None, None
    if routing.blocked:
        raise ManufacturingError(f'Routing {routing.routing_no} is blocked.')
    version = routing.active_version(on_date)
    if version is None:
        raise ManufacturingError(f'Routing {routing.routing_no} has no certified version effective on {on_date}.')
    return routing, version


@transaction.atomic
def create_order(actor, *, item, quantity, location=None, due_date=None, planned_start=None, variant=None, bom=None, bom_version=None,
                 routing=None, routing_version=None, source_type='MANUAL', source_no='', sales_order=None, customer=None, priority='NORMAL',
                 status='DRAFT', order_type='STANDARD', department='', project='', remarks='', parent_order=None, refresh=True):
    require(actor, 'create_order')
    setup = get_setup(actor.tenant)
    if not setup.manufacturing_enabled:
        raise ManufacturingError('Manufacturing is disabled in manufacturing setup.')
    if item.tenant_id != actor.tenant.id:
        raise ManufacturingError('Item not found.')
    if item.blocked or not item.active:
        raise ManufacturingError(f'{item.item_no} is blocked or inactive.')
    quantity = q3(quantity)
    if quantity <= 0:
        raise ManufacturingError('Quantity must be greater than zero.')
    location = location or setup.default_production_location
    if location is None:
        raise ManufacturingError('Select the production location (or set a default in manufacturing setup).')
    if location.tenant_id != actor.tenant.id or not location.usable:
        raise ManufacturingError(f'Location {location.code} is not usable.')
    on_date = due_date or dj_timezone.localdate()
    bom, bom_version = resolve_bom(actor.tenant, item, on_date, bom, bom_version)
    routing, routing_version = resolve_routing(actor.tenant, item, on_date, routing, routing_version)
    fg_location = setup.default_finished_goods_location or location
    order = ProductionOrder.objects.create(
        tenant=actor.tenant, company=location.company, order_no=next_number(actor.tenant, 'PRODUCTION_ORDER'), order_type=order_type,
        status=status, location=location, department=department, project=project, source_type=source_type, source_no=source_no,
        sales_order=sales_order, customer=customer or (sales_order.customer if sales_order else None), priority=priority, item=item,
        variant=variant, sku=eng.ensure_sku(actor, item, variant, fg_location, bom.sku), description=item.description, bom=bom,
        bom_version=bom_version, bom_effective_date=on_date, routing=routing, routing_version=routing_version, planned_qty=quantity,
        due_date=due_date, planned_start=planned_start, costing_method=bom.costing_method or setup.default_costing_method,
        material_location=location, material_bin=setup.default_material_bin if _bin_at(setup.default_material_bin, location) else None,
        production_bin=setup.default_production_bin if _bin_at(setup.default_production_bin, location) else None,
        wip_location=setup.default_wip_location or location, finished_goods_location=fg_location,
        finished_goods_bin=setup.default_finished_goods_bin if _bin_at(setup.default_finished_goods_bin, fg_location) else None,
        qc_location=location, qc_bin=setup.default_qc_bin if _bin_at(setup.default_qc_bin, location) else None,
        scrap_location=location, scrap_bin=setup.default_scrap_bin if _bin_at(setup.default_scrap_bin, location) else None,
        rework_bin=setup.default_rework_bin if _bin_at(setup.default_rework_bin, location) else None,
        parent_order=parent_order, remarks=remarks, created_by=actor.user,
    )
    audit(actor, 'create', 'PRODUCTION_ORDER', order.order_no, order=order,
          new={'item': item.item_no, 'qty': quantity, 'bom': bom_version, 'routing': routing_version or '-', 'source': source_type})
    if refresh:
        refresh_order(actor, order)
    return order


def _bin_at(bin_, location):
    return bin_ is not None and location is not None and bin_.location_id == location.id


def has_postings(order):
    return order.posting_batches.exists()


@transaction.atomic
def refresh_order(actor, order, *, confirm=False):
    """Refresh / calculate: components from the BOM version, operations from the routing version, capacity, schedule and cost.

    Once transactions are posted the snapshot is kept and only requirements/costs are recalculated - never silently rebuilt.
    """
    require(actor, 'create_order')
    order = eng.lock_order(actor, order)
    if order.is_closed:
        raise ManufacturingError(f'{order.order_no} is {order.get_status_display().lower()}.')
    posted = has_postings(order)
    if order.is_executable and not confirm:
        raise ConfirmationRequired('Existing production transactions may be affected. Continue?')
    if not posted:
        order.components.all().delete()
        order.operations.all().delete()
        order.lines.all().delete()
        _build_lines(actor, order)
        _build_components(actor, order)
        _build_operations(actor, order)
    else:
        _recalculate(actor, order)
    _schedule(order)
    _plan_costs(order)
    order.refreshed_at = dj_timezone.now()
    order.updated_by = actor.user
    order.save()
    audit(actor, 'refresh', 'PRODUCTION_ORDER', order.order_no, order=order,
          new={'components': order.components.count(), 'operations': order.operations.count(), 'planned_cost': order.planned_total_cost,
               'mode': 'recalculate' if posted else 'rebuild'})
    return order


def _build_lines(actor, order):
    ProductionOrderLine.objects.create(tenant=actor.tenant, company=order.company, order=order, line_no=10000, line_type='MAIN',
                                       item=order.item, sku=order.sku, variant=order.variant, description=order.description,
                                       quantity=order.planned_qty, uom=order.item.base_uom, location=order.finished_goods_location,
                                       bin=order.finished_goods_bin, created_by=actor.user)
    number = 20000
    for line in order.bom_version.lines.filter(component_type__in=('BY_PRODUCT', 'CO_PRODUCT')).select_related('component_item'):
        per_piece = per_piece_requirement(basis=line.consumption_basis, quantity=line.quantity, base_quantity=order.bom_version.base_quantity,
                                          formula=line.formula, parent=_parent_weights(order.bom_version, order.item, order.planned_qty))
        ProductionOrderLine.objects.create(
            tenant=actor.tenant, company=order.company, order=order, line_no=number, line_type=line.component_type,
            item=line.component_item, variant=line.variant, description=line.description or line.component_item.description,
            quantity=q3(per_piece * order.planned_qty), uom=line.uom or line.component_item.base_uom, unit_cost=line.component_cost,
            location=order.scrap_location if line.component_type == 'BY_PRODUCT' else order.finished_goods_location,
            bin=order.scrap_bin if line.component_type == 'BY_PRODUCT' else order.finished_goods_bin, created_by=actor.user)
        number += 10000


def _build_components(actor, order):
    setup = get_setup(actor.tenant)
    version = order.bom_version
    parent = _parent_weights(version, order.item, order.planned_qty)
    on_date = order.bom_effective_date or dj_timezone.localdate()
    for line in version.lines.exclude(component_type__in=('BY_PRODUCT', 'CO_PRODUCT')).select_related('component_item', 'uom'):
        if (line.effective_from and line.effective_from > on_date) or (line.effective_to and line.effective_to < on_date):
            continue
        item = line.component_item
        per_piece = per_piece_requirement(basis=line.consumption_basis, quantity=line.quantity, base_quantity=version.base_quantity,
                                          formula=line.formula, parent={**parent, 'loss_percent': line.expected_loss_percent,
                                                                        'scrap_percent': line.scrap_percent})
        flushing = line.flushing_method or ('BACKWARD' if line.backflush_enabled else setup.default_consumption_method)
        if item.serial_tracking:
            flushing = 'MANUAL'  # a serialized piece is consumed by scanning it
        location = line.location or order.location
        component = ProductionOrderComponent(
            tenant=actor.tenant, company=order.company, order=order, line_no=line.line_no * 100, bom_line=line, item=item,
            variant=line.variant, sku=line.component_sku, description=line.description or item.description,
            component_type=line.component_type, consumption_basis=line.consumption_basis, formula=line.formula, uom=line.uom or item.base_uom,
            scrap_percent=line.scrap_percent, fixed_scrap_qty=line.fixed_scrap_qty, expected_loss_percent=line.expected_loss_percent,
            expected_loss_qty=line.expected_loss_qty, routing_link_code=line.routing_link_code, operation_no=line.operation_no,
            flushing_method=flushing, supply_method=line.supply_method, location=location,
            bin=line.bin if _bin_at(line.bin, location) else (order.material_bin if location.id == order.location_id else None),
            substitute_item=line.substitute_item, lot_required=line.lot_required or line.batch_required,
            serial_required=line.serial_required or item.serial_tracking, optional=line.optional,
            metal=line.metal or (item.metal if item.metal in ('GOLD', 'SILVER', 'PLATINUM') else ''), purity=line.purity or item.purity,
            qty_per=per_piece, created_by=actor.user,
        )
        component.unit_cost = component_unit_cost(actor.tenant, line, location)
        _apply_requirement(component, order.planned_qty)
        component.save()


def _apply_requirement(component, quantity):
    req = component_requirement(per_piece=component.qty_per, production_qty=quantity, scrap_percent=component.scrap_percent,
                                loss_percent=component.expected_loss_percent, fixed_scrap=component.fixed_scrap_qty,
                                fixed_loss=component.expected_loss_qty)
    component.gross_requirement, component.scrap_requirement, component.expected_qty = req['gross'], req['scrap'], req['expected']
    component.expected_cost = money(component.expected_qty * component.unit_cost)


def _build_operations(actor, order):
    if order.routing_version is None:
        return
    on_date = order.bom_effective_date or dj_timezone.localdate()
    for line in order.routing_version.lines.select_related('work_center', 'machine_center', 'subcontractor'):
        if (line.start_date and line.start_date > on_date) or (line.end_date and line.end_date < on_date):
            continue
        rates = line.rates()
        operation = ProductionOrderRoutingLine(
            tenant=actor.tenant, company=order.company, order=order, routing_line=line, operation_no=line.operation_no, sequence=line.sequence,
            description=line.description, work_center=line.work_center, machine_center=line.machine_center, resource=line.resource,
            routing_link_code=line.routing_link_code, subcontracting=line.subcontracting, subcontractor=line.subcontractor,
            quality_check_required=line.quality_check_required, setup_time=line.setup_time, run_time=line.run_time, wait_time=line.wait_time,
            move_time=line.move_time, queue_time=line.queue_time or line.work_center.queue_time, efficiency=line.efficiency,
            concurrent_capacity=line.concurrent_capacity, send_ahead_quantity=line.send_ahead_quantity, scrap_percent=line.scrap_percent,
            labour_rate=rates['labour'], machine_rate=rates['machine'], overhead_rate=rates['overhead'], created_by=actor.user,
        )
        _apply_operation_plan(operation, order)
        operation.save()


def _apply_operation_plan(operation, order):
    setup = get_setup(order.tenant)
    minutes = operation_capacity_minutes(setup_time=operation.setup_time, run_time=operation.run_time, quantity=order.planned_qty,
                                         efficiency=operation.efficiency, concurrent_capacity=operation.concurrent_capacity)
    operation.planned_setup_minutes, operation.planned_run_minutes, operation.planned_total_minutes = minutes['setup'], minutes['run'], minutes['total']
    hours = minutes['total'] / 60
    if operation.subcontracting and operation.subcontractor_id:
        sub = operation.subcontractor
        operation.planned_labour_cost = operation.planned_machine_cost = operation.planned_overhead_cost = ZERO
        operation.planned_subcontract_cost = money(sub.rate_per_unit * order.planned_qty
                                                   + sub.rate_per_gram * (order.expected_net_weight or ZERO) * order.planned_qty)
    else:
        operation.planned_labour_cost = money(hours * operation.labour_rate)
        operation.planned_machine_cost = money(hours * operation.machine_rate)
        operation.planned_overhead_cost = money(hours * operation.overhead_rate) if setup.overhead_enabled else ZERO
        operation.planned_subcontract_cost = ZERO


def _recalculate(actor, order):
    for component in order.components.all():
        if component.bom_line_id is not None:
            _apply_requirement(component, order.planned_qty)
            component.save()
    for operation in order.operations.filter(is_rework=False):
        _apply_operation_plan(operation, order)
        operation.save()
    order.lines.filter(line_type='MAIN').update(quantity=order.planned_qty)


def _schedule(order):
    """Forward from the planned start, else backward from the due date, through each work centre's calendar."""
    setup = get_setup(order.tenant)
    operations = list(order.operations.select_related('work_center__calendar', 'subcontractor').order_by('sequence', 'operation_no'))
    if not operations:
        if order.planned_start is None:
            order.planned_start = dj_timezone.now()
        order.planned_end = order.planned_end or order.planned_start
        return
    def calendar_of(op):
        return op.work_center.calendar or setup.default_calendar
    def duration(op, start, forward=True):
        if op.subcontracting:
            days = op.subcontractor.lead_time_days if op.subcontractor_id else 1
            return start + timedelta(days=days) if forward else start - timedelta(days=days)
        fn = schedule_forward if forward else schedule_backward
        return fn(start, op.planned_total_minutes, calendar_of(op), op.work_center.capacity, op.work_center.working_minutes_per_day)
    if order.planned_start is None and order.due_date is not None:
        end = dj_timezone.make_aware(datetime.combine(order.due_date, time(18, 0)))
        for op in reversed(operations):
            op.planned_end = end - timedelta(minutes=float(op.move_time))
            op.planned_start = duration(op, op.planned_end, forward=False)
            end = op.planned_start - timedelta(minutes=float(op.wait_time + op.queue_time))
            op.save(update_fields=['planned_start', 'planned_end', 'updated_at'])
        order.planned_start = operations[0].planned_start
        order.planned_end = operations[-1].planned_end
    else:
        start = order.planned_start or dj_timezone.now()
        order.planned_start = start
        for op in operations:
            op.planned_start = start + timedelta(minutes=float(op.wait_time + op.queue_time))
            op.planned_end = duration(op, op.planned_start)
            start = op.planned_end + timedelta(minutes=float(op.move_time))
            op.save(update_fields=['planned_start', 'planned_end', 'updated_at'])
        order.planned_end = operations[-1].planned_end


def _plan_costs(order):
    setup = get_setup(order.tenant)
    components = list(order.components.all())
    operations = list(order.operations.filter(is_rework=False))
    material = sum((c.expected_cost for c in components), ZERO)
    order.planned_material_cost = material
    order.planned_labour_cost = sum((o.planned_labour_cost for o in operations), ZERO)
    order.planned_machine_cost = sum((o.planned_machine_cost for o in operations), ZERO)
    material_overhead = money(material * setup.material_overhead_percent / 100) if setup.overhead_enabled else ZERO
    order.planned_overhead_cost = sum((o.planned_overhead_cost for o in operations), ZERO) + material_overhead
    order.planned_subcontract_cost = sum((o.planned_subcontract_cost for o in operations), ZERO)
    order.planned_minutes = sum((o.planned_total_minutes for o in operations), ZERO)
    profile = profile_of(order.item)
    order.expected_net_weight = order.bom_version.expected_net_weight or (profile.net_metal_weight if profile else ZERO)
    metal = [c for c in components if c.metal in ('GOLD', 'SILVER', 'PLATINUM') and c.expected_qty]
    metal_rate = (sum((c.expected_cost for c in metal), ZERO) / sum((c.expected_qty for c in metal), ZERO)) if metal else ZERO
    credit = ZERO
    co_value = ZERO
    for line in order.lines.exclude(line_type='MAIN'):
        if line.line_type == 'BY_PRODUCT':
            if not line.unit_cost:
                line.unit_cost = metal_rate.quantize(Decimal('0.0001')) if metal_rate else ZERO
                line.save(update_fields=['unit_cost', 'updated_at'])
            credit += money(line.quantity * line.unit_cost)
        else:
            co_value += money(line.quantity * line.unit_cost)
    order.planned_byproduct_credit = credit
    order.planned_total_cost = (order.planned_material_cost + order.planned_labour_cost + order.planned_machine_cost
                                + order.planned_overhead_cost + order.planned_subcontract_cost)
    net = order.planned_total_cost - credit - co_value
    order.planned_unit_cost = (net / order.planned_qty).quantize(Decimal('0.0001')) if order.planned_qty else ZERO
    order.lines.filter(line_type='MAIN').update(unit_cost=order.planned_unit_cost)


@transaction.atomic
def update_planning_status(actor, order, status):
    """Planned -> Firm planned (and back), used by the planning worksheet."""
    require(actor, 'plan')
    order = eng.lock_order(actor, order)
    if order.status not in ('DRAFT', 'PLANNED', 'FIRM_PLANNED') or status not in ('PLANNED', 'FIRM_PLANNED'):
        raise ManufacturingError(f'Cannot change {order.order_no} from {order.get_status_display()} to {status}.')
    old = order.status
    order.status = status
    order.save(update_fields=['status', 'updated_at'])
    audit(actor, 'status', 'PRODUCTION_ORDER', order.order_no, order=order, old={'status': old}, new={'status': status})
    return order


@transaction.atomic
def approve_order(actor, order):
    require(actor, 'approve_order')
    order = eng.lock_order(actor, order)
    if order.status not in ('DRAFT', 'PLANNED', 'FIRM_PLANNED'):
        raise ManufacturingError(f'{order.order_no} is {order.get_status_display().lower()}.')
    setup = get_setup(actor.tenant)
    if not setup.allow_self_approval and order.created_by_id == actor.user.pk and not can(actor, 'setup'):
        raise ManufacturingError('Maker-checker: the order creator cannot approve it.')
    old = order.status
    order.status, order.approved_by, order.approved_at = 'APPROVED', actor.user, dj_timezone.now()
    order.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
    audit(actor, 'approve', 'PRODUCTION_ORDER', order.order_no, order=order, old={'status': old}, new={'status': 'APPROVED'})
    return order


def material_availability(order):
    """Per component: required / consumed / issued / reserved / available / shortage, all from the inventory engine."""
    rows = []
    for component in order.components.select_related('item', 'location', 'uom'):
        reserved = component.reserved_qty
        free_here = available_qty(order.tenant, item=component.item, location=component.location, variant=component.variant)
        elsewhere = InventoryBalance.objects.filter(tenant=order.tenant, item=component.item, variant=component.variant) \
            .exclude(location=component.location).exclude(location__location_type='TRANSIT') \
            .values('location__code', 'location_id').annotate(q=Sum('available_qty')).filter(q__gt=0)
        need = component.to_issue_qty
        shortage = max(need - reserved - free_here, ZERO)
        if component.item.blocked or not component.item.active:
            status = 'BLOCKED'
        elif need <= 0 or shortage == 0:
            status = 'AVAILABLE'
        elif reserved + free_here > 0:
            status = 'PARTIALLY_AVAILABLE'
        else:
            status = 'SHORT'
        rows.append({'component': component, 'required': component.expected_qty, 'consumed': component.consumed_qty,
                     'issued': component.issued_open_qty, 'reserved': reserved, 'available': free_here, 'to_issue': need,
                     'shortage': shortage, 'status': status, 'elsewhere': list(elsewhere)})
    return rows


@transaction.atomic
def release_order(actor, order, *, override_shortage=False):
    require(actor, 'release')
    order = eng.lock_order(actor, order)
    setup = get_setup(actor.tenant)
    allowed = ('APPROVED',) if setup.require_manager_approval else ('DRAFT', 'PLANNED', 'FIRM_PLANNED', 'APPROVED')
    if order.status not in allowed:
        hint = ' It must be approved by a manufacturing manager first.' if setup.require_manager_approval and order.is_planning else ''
        raise ManufacturingError(f'{order.order_no} is {order.get_status_display().lower()}.{hint}')
    if order.refreshed_at is None or not order.components.exists():
        raise ManufacturingError('Refresh the order (BOM + routing) before releasing it.')
    if order.bom_version.status != 'CERTIFIED':
        raise ManufacturingError(f'{order.bom_version} is no longer certified.')
    if order.routing_version_id and order.routing_version.status != 'CERTIFIED':
        raise ManufacturingError(f'{order.routing_version} is no longer certified.')
    shortages = [r for r in material_availability(order) if r['status'] in ('SHORT', 'PARTIALLY_AVAILABLE', 'BLOCKED') and not r['component'].optional]
    if shortages:
        text = ', '.join(f"{r['component'].item.item_no} short {r['shortage']}" for r in shortages)
        if not override_shortage:
            raise ConfirmationRequired(f'Material shortage: {text}. Release anyway?')
        if not (setup.allow_release_with_shortage or can(actor, 'override_shortage')):
            raise ManufacturingError(f'Material shortage ({text}). Only a manufacturing manager can release despite a shortage.')
        order.shortage_override_by = actor.user
    old = order.status
    order.status, order.released_by, order.released_at, order.released_qty = 'RELEASED', actor.user, dj_timezone.now(), order.planned_qty
    eng.derive_status(order, setup)  # material reserved before release keeps the order at MATERIAL_RESERVED
    order.save()
    for operation in order.operations.filter(status='NOT_STARTED').order_by('sequence')[:1]:
        operation.status = 'READY'
        operation.save(update_fields=['status', 'updated_at'])
    audit(actor, 'release', 'PRODUCTION_ORDER', order.order_no, order=order, old={'status': old}, new={'status': 'RELEASED'},
          reason='Released despite shortage' if order.shortage_override_by_id else '')
    if setup.auto_reserve_material:
        reserve_material(actor, order, strict=False)
    if setup.auto_post_consumption and order.components.filter(flushing_method__in=('FORWARD', 'PICK_FORWARD')).exists():
        _flush(actor, order, None)
    return order


class _NothingToFlush(Exception):
    pass


def _flush(actor, order, operation):
    """Forward-flush components for the operation (or unlinked ones at release); no posting is kept when nothing flushed."""
    try:
        with transaction.atomic():
            engine = eng.ManufacturingPostingEngine(actor, eng.lock_order(actor, order), kind='JOURNAL')
            engine.forward_flush(operation)
            if not engine.records['consumption']:
                raise _NothingToFlush
            return engine.finalize(action='forward_flush')
    except _NothingToFlush:
        return None


@transaction.atomic
def cancel_order(actor, order, *, reason):
    require(actor, 'release')
    order = eng.lock_order(actor, order)
    if order.is_closed:
        raise ManufacturingError(f'{order.order_no} is already {order.get_status_display().lower()}.')
    if order.posting_batches.exists():
        raise ManufacturingError('Transactions were posted on this order - reverse them, or finish the order instead of cancelling.')
    if not (reason or '').strip():
        raise ManufacturingError('A reason is required to cancel.')
    inventory = InventoryPostingEngine(actor.tenant, actor.user, document_type='PRODUCTION_CANCEL', document_no=order.order_no)
    for component in order.components.all():
        for reservation in eng.open_reservations(order, component):
            inventory.release(reservation)
        eng.sync_reserved(component)
    old = order.status
    order.status, order.cancelled_reason = 'CANCELLED', reason[:250]
    order.save(update_fields=['status', 'cancelled_reason', 'updated_at'])
    audit(actor, 'cancel', 'PRODUCTION_ORDER', order.order_no, order=order, old={'status': old}, new={'status': 'CANCELLED'}, reason=reason)
    return order


@transaction.atomic
def close_order(actor, order):
    require(actor, 'finish')
    order = eng.lock_order(actor, order)
    if order.status != 'FINISHED':
        raise ManufacturingError('Only finished orders can be closed.')
    order.status = 'CLOSED'
    order.save(update_fields=['status', 'updated_at'])
    audit(actor, 'close', 'PRODUCTION_ORDER', order.order_no, order=order, old={'status': 'FINISHED'}, new={'status': 'CLOSED'})
    return order


def create_orders_from_sales_order(actor, sales_order, *, location=None):
    """Make-to-order: one production order per sales order line whose product maps to an item with a certified BOM."""
    created, skipped = [], []
    for line in sales_order.lines.select_related('product'):
        if line.remaining_quantity <= 0:
            continue
        item = Item.objects.filter(tenant=actor.tenant, legacy_product=line.product).first()
        if item is None:
            skipped.append(f'{line.product}: no inventory item linked')
            continue
        try:
            created.append(create_order(actor, item=item, quantity=line.remaining_quantity, location=location, source_type='SALES_ORDER',
                                        source_no=sales_order.order_no, sales_order=sales_order, order_type='MAKE_TO_ORDER',
                                        due_date=sales_order.expected_delivery_date, priority='HIGH'))
        except ManufacturingError as exc:
            skipped.append(f'{item.item_no}: {exc}')
    return created, skipped


# ---------------------------------------------------------------------------
# Reservation, transfer to production, picking
# ---------------------------------------------------------------------------

@transaction.atomic
def reserve_material(actor, order, *, component=None, units=None, strict=True):
    """Reserve available stock at each component's location (actual jewellery units for serialized components)."""
    require(actor, 'release')
    order = eng.lock_order(actor, order)
    if not (order.is_executable or order.status == 'APPROVED'):
        raise ManufacturingError('Material can be reserved once the order is approved or released.')
    inventory = InventoryPostingEngine(actor.tenant, actor.user, document_type='PRODUCTION_RESERVE', document_no=order.order_no)
    components = [component] if component is not None else list(order.components.select_related('item', 'location'))
    reserved_any = []
    for comp in components:
        comp = ProductionOrderComponent.objects.select_for_update().get(pk=comp.pk, order=order)
        need = comp.to_issue_qty - comp.reserved_qty
        if need <= 0:
            continue
        if comp.item.serial_tracking:
            candidates = units if units else list(JewelleryUnit.objects.filter(
                tenant=actor.tenant, item=comp.item, variant=comp.variant, current_location=comp.location, status='AVAILABLE').order_by('id')[:int(need)])
            for unit in candidates[:int(need)]:
                inventory.reserve(item=comp.item, variant=comp.variant, location=comp.location, quantity=1, unit=unit, source_type='PRODUCTION_ORDER',
                                  source_no=order.order_no, source_line_no=comp.line_no)
                reserved_any.append(f'{comp.item.item_no} {unit.barcode}')
        else:
            free = available_qty(actor.tenant, item=comp.item, location=comp.location, variant=comp.variant)
            qty = min(need, free)
            if qty > 0:
                inventory.reserve(item=comp.item, variant=comp.variant, location=comp.location, quantity=qty, source_type='PRODUCTION_ORDER',
                                  source_no=order.order_no, source_line_no=comp.line_no)
                reserved_any.append(f'{comp.item.item_no} {qty}')
            elif strict and component is not None:
                raise ManufacturingError(f'Nothing available to reserve for {comp.item.item_no} at {comp.location.code}.')
        eng.sync_reserved(comp)
    if order.is_executable:
        eng.derive_status(order, actor=actor)
        order.save(update_fields=['status', 'updated_at'])
    audit(actor, 'reserve', 'PRODUCTION_ORDER', order.order_no, order=order, new={'reserved': '; '.join(reserved_any) or 'nothing'})
    return reserved_any


@transaction.atomic
def release_reservations(actor, order, component=None):
    require(actor, 'release')
    order = eng.lock_order(actor, order)
    inventory = InventoryPostingEngine(actor.tenant, actor.user, document_type='PRODUCTION_RESERVE', document_no=order.order_no)
    for comp in ([component] if component else order.components.all()):
        for reservation in eng.open_reservations(order, comp):
            inventory.release(reservation)
        eng.sync_reserved(comp)
    if order.is_executable:
        eng.derive_status(order, actor=actor)
        order.save(update_fields=['status', 'updated_at'])
    audit(actor, 'unreserve', 'PRODUCTION_ORDER', order.order_no, order=order)


def create_material_transfer(actor, order, *, from_location):
    """Shortage at the production location + stock elsewhere -> a real inventory transfer order to production."""
    require(actor, 'release')
    lines = []
    for row in material_availability(order):
        comp = row['component']
        if row['shortage'] <= 0 or comp.location_id != order.location_id:
            continue
        free = available_qty(actor.tenant, item=comp.item, location=from_location, variant=comp.variant)
        qty = min(row['shortage'], free)
        if qty <= 0:
            continue
        if comp.item.serial_tracking:
            units = JewelleryUnit.objects.filter(tenant=actor.tenant, item=comp.item, current_location=from_location, status='AVAILABLE')[:int(qty)]
            lines.extend({'unit': u} for u in units)
        else:
            lines.append({'item': comp.item, 'variant': comp.variant, 'quantity': qty})
    if not lines:
        raise ManufacturingError(f'Nothing short at {order.location.code} is available at {from_location.code}.')
    transfer = create_transfer_order(actor, from_location=from_location, to_location=order.location, lines=lines, source_type='WAREHOUSE_PLANNING',
                                     reason=f'Material for production order {order.order_no}', priority=order.priority)
    audit(actor, 'transfer_request', 'PRODUCTION_ORDER', order.order_no, order=order, new={'transfer': transfer.transfer_no})
    return transfer


def create_purchase_requests(actor, order):
    """Shortage with no stock anywhere -> purchase suggestions (a planning run scoped to this order)."""
    require(actor, 'plan')
    rows = [r for r in material_availability(order) if r['shortage'] > 0 and not r['elsewhere']]
    if not rows:
        raise ManufacturingError('No component needs purchasing (check transfers from other locations first).')
    with transaction.atomic():
        run = PlanningRun.objects.create(tenant=actor.tenant, run_no=next_number(actor.tenant, 'PLANNING_RUN'), horizon_days=0,
                                         parameters={'order': order.order_no, 'type': 'shortage'}, created_by=actor.user)
        for row in rows:
            comp = row['component']
            PlanningSuggestion.objects.create(tenant=actor.tenant, run=run, suggestion_type='PURCHASE', level=1, item=comp.item,
                                              variant=comp.variant, location=comp.location, demand_qty=row['to_issue'],
                                              supply_qty=row['reserved'] + row['available'], quantity=row['shortage'],
                                              due_date=(order.planned_start.date() if order.planned_start else order.due_date),
                                              demand_source=f'Production order {order.order_no}', created_by=actor.user)
        audit(actor, 'purchase_request', 'PRODUCTION_ORDER', order.order_no, order=order, new={'run': run.run_no, 'lines': len(rows)})
    return run


@transaction.atomic
def create_pick_list(actor, order):
    require(actor, 'execute')
    order = eng.lock_order(actor, order)
    eng.assert_executable(order)
    if order.pick_lists.filter(status='OPEN').exists():
        raise ManufacturingError('An open pick list already exists for this order.')
    pick = ProductionPickList.objects.create(tenant=actor.tenant, company=order.company, pick_no=next_number(actor.tenant, 'PICK_LIST'),
                                             order=order, location=order.location, to_bin=order.production_bin, created_by=actor.user)
    number = 0
    for comp in order.components.select_related('item').filter(location=order.location):
        need = comp.to_issue_qty
        if need <= 0 or comp.flushing_method in ('FORWARD', 'BACKWARD'):
            continue
        for reservation in eng.open_reservations(order, comp).select_related('bin', 'jewellery_unit'):
            take = min(reservation.open_quantity, need)
            if take <= 0:
                break
            number += 10
            ProductionPickLine.objects.create(tenant=actor.tenant, company=order.company, pick=pick, line_no=number, component=comp,
                                              reservation=reservation, item=comp.item, from_bin=reservation.bin,
                                              jewellery_unit=reservation.jewellery_unit, qty_to_pick=take, created_by=actor.user)
            need -= take
        if need > 0 and not comp.item.serial_tracking:
            balances = InventoryBalance.objects.filter(tenant=actor.tenant, item=comp.item, variant=comp.variant, location=order.location,
                                                       available_qty__gt=0, jewellery_unit__isnull=True)
            if order.production_bin_id:
                balances = balances.exclude(bin=order.production_bin)
            for balance in balances.order_by(F('bin__pick_sequence').asc(nulls_first=True), 'id'):
                take = min(balance.available_qty, need)
                number += 10
                ProductionPickLine.objects.create(tenant=actor.tenant, company=order.company, pick=pick, line_no=number, component=comp,
                                                  item=comp.item, from_bin=balance.bin, lot_no=balance.lot_no, qty_to_pick=take, created_by=actor.user)
                need -= take
                if need <= 0:
                    break
    if not number:
        pick.delete()
        raise ManufacturingError('Nothing to pick: all pickable material is issued, or reserve / transfer material to the production location first.')
    audit(actor, 'create', 'PICK_LIST', pick.pick_no, order=order, new={'lines': number // 10})
    return pick


@transaction.atomic
def scan_pick(actor, pick, code, *, quantity=None):
    """Validate a scan against the pick list: jewellery unit / HUID, item or SKU barcode, then confirm quantity."""
    require(actor, 'execute')
    pick = ProductionPickList.objects.select_for_update().get(pk=pick.pk, tenant=actor.tenant)
    if pick.status != 'OPEN':
        raise ManufacturingError(f'{pick.pick_no} is {pick.get_status_display().lower()}.')
    code = (code or '').strip()
    if not code:
        raise ManufacturingError('Scan a barcode.')
    unit = find_unit(actor.tenant, code)
    lines = pick.lines.select_related('item', 'from_bin', 'jewellery_unit').filter(confirmed=False)
    if unit is not None:
        line = lines.filter(jewellery_unit=unit).first() or lines.filter(item=unit.item, jewellery_unit__isnull=True).first()
        if line is None:
            raise ManufacturingError(f'{unit.barcode} ({unit.item.item_no}) is not on this pick list.')
        if line.jewellery_unit_id is None:
            if unit.status != 'AVAILABLE' or unit.current_location_id != pick.location_id:
                raise ManufacturingError(f'{unit.barcode} is not available at {pick.location.code}.')
            line.jewellery_unit = unit
        line.qty_picked, line.scanned_code, line.confirmed = Decimal('1'), code, True
        line.save()
        return line
    line = next((l for l in lines if code in (l.item.item_no, l.from_bin.code if l.from_bin_id else None)
                 or l.item.skus.filter(barcode=code).exists()), None)
    if line is None:
        raise ManufacturingError(f'"{code}" does not match any open pick line (item, SKU barcode or bin).')
    if line.item.serial_tracking:
        raise ManufacturingError(f'{line.item.item_no} is serialized: scan each piece, not the item barcode.')
    qty = q3(quantity) if quantity not in (None, '') else line.qty_to_pick
    if qty <= 0 or qty > line.qty_to_pick:
        raise ManufacturingError(f'Pick quantity must be between 0 and {line.qty_to_pick}.')
    line.qty_picked, line.scanned_code, line.confirmed = qty, code, True
    line.save()
    return line


def post_pick_list(actor, pick):
    pick = _get(ProductionPickList, actor, pick)
    lines = list(pick.lines.filter(confirmed=True).select_related('component', 'jewellery_unit', 'from_bin'))
    if not lines:
        raise ManufacturingError('Scan / confirm at least one pick line before posting.')
    requests = [{'component': l.component, 'quantity': l.qty_picked, 'unit': l.jewellery_unit, 'from_bin': l.from_bin, 'pick_line': l}
                for l in lines]
    return eng.issue_material(actor, pick.order, requests, pick_list=pick)


# ---------------------------------------------------------------------------
# Shop floor: start / pause / resume / complete
# ---------------------------------------------------------------------------

def _lock_operation(actor, operation):
    op = ProductionOrderRoutingLine.objects.select_for_update().select_related('order', 'machine_center').filter(
        tenant=actor.tenant, pk=getattr(operation, 'pk', operation)).first()
    if op is None:
        raise ManufacturingError('Operation not found.')
    return op


def _clock(op, now):
    if op.clock_started_at:
        op.clocked_minutes += q3(D((now - op.clock_started_at).total_seconds()) / 60)
        op.clock_started_at = None


@transaction.atomic
def start_operation(actor, operation, *, machine=None, note=''):
    require(actor, 'execute')
    op = _lock_operation(actor, operation)
    order = eng.lock_order(actor, op.order_id)
    eng.assert_executable(order)
    if op.status not in ('NOT_STARTED', 'READY'):
        raise ManufacturingError(f'Operation {op.operation_no} is {op.get_status_display().lower()}.')
    setup = get_setup(actor.tenant)
    previous = eng.previous_operation(op) if not op.is_rework else None
    if previous is not None and setup.enforce_operation_sequence:
        ready = previous.status in eng.DONE_OPERATION or (previous.send_ahead_quantity and previous.output_qty >= previous.send_ahead_quantity) \
            or previous.output_qty > 0
        if not ready:
            raise ManufacturingError(f'Operation {previous.operation_no} has not produced anything yet.')
    machine = machine or op.machine_center
    if machine is not None and machine.status in ('MAINTENANCE', 'BREAKDOWN', 'BLOCKED'):
        raise ManufacturingError(f'Machine {machine.code} is {machine.get_status_display().lower()}.')
    now = dj_timezone.now()
    op.status, op.actual_start, op.operator, op.clock_started_at = 'STARTED', op.actual_start or now, actor.user, now
    if machine is not None:
        op.machine_center = machine
        machine.status = 'RUNNING'
        machine.save(update_fields=['status', 'updated_at'])
    op.save()
    OperationEvent.objects.create(tenant=actor.tenant, company=order.company, order=order, operation=op, event='START', at=now,
                                  operator=actor.user, machine_center=machine, note=note, created_by=actor.user)
    order.started_qty = max(order.started_qty, order.planned_qty)
    order.actual_start = order.actual_start or now
    eng.derive_status(order, actor=actor)
    order.save()
    audit(actor, 'start_operation', 'PRODUCTION_ORDER', order.order_no, order=order, new={'operation': op.operation_no})
    if setup.auto_post_consumption and order.components.filter(flushing_method__in=('FORWARD', 'PICK_FORWARD'), routing_link_code=op.routing_link_code) \
            .exclude(routing_link_code='').exists():
        _flush(actor, order, op)
    return op


@transaction.atomic
def pause_operation(actor, operation, *, reason=None, note=''):
    require(actor, 'execute')
    op = _lock_operation(actor, operation)
    if op.status != 'STARTED':
        raise ManufacturingError(f'Operation {op.operation_no} is not running.')
    now = dj_timezone.now()
    _clock(op, now)
    op.status = 'PAUSED'
    op.save()
    if op.machine_center_id:
        op.machine_center.status = 'IDLE'
        op.machine_center.save(update_fields=['status', 'updated_at'])
    OperationEvent.objects.create(tenant=actor.tenant, company=op.order.company, order=op.order, operation=op, event='PAUSE', at=now,
                                  operator=actor.user, downtime_reason=reason, note=note, created_by=actor.user)
    audit(actor, 'pause_operation', 'PRODUCTION_ORDER', op.order.order_no, order=op.order,
          new={'operation': op.operation_no, 'reason': reason or ''})
    return op


@transaction.atomic
def resume_operation(actor, operation, *, note=''):
    require(actor, 'execute')
    op = _lock_operation(actor, operation)
    if op.status != 'PAUSED':
        raise ManufacturingError(f'Operation {op.operation_no} is not paused.')
    now = dj_timezone.now()
    op.status, op.clock_started_at = 'STARTED', now
    op.save()
    if op.machine_center_id:
        op.machine_center.status = 'RUNNING'
        op.machine_center.save(update_fields=['status', 'updated_at'])
    pause = op.events.filter(event='PAUSE').order_by('-at').first()
    OperationEvent.objects.create(tenant=actor.tenant, company=op.order.company, order=op.order, operation=op, event='RESUME', at=now,
                                  operator=actor.user, downtime_reason=pause.downtime_reason if pause else None, note=note, created_by=actor.user)
    audit(actor, 'resume_operation', 'PRODUCTION_ORDER', op.order.order_no, order=op.order, new={'operation': op.operation_no})
    return op


def paused_minutes(op):
    """Downtime = time between PAUSE and RESUME events not yet posted."""
    total, paused_at = ZERO, None
    for event in op.events.filter(event__in=('PAUSE', 'RESUME')).order_by('at'):
        if event.event == 'PAUSE':
            paused_at = event.at
        elif paused_at is not None:
            total += D((event.at - paused_at).total_seconds()) / 60
            paused_at = None
    return q3(total)


def complete_operation(actor, operation, *, output_qty=0, scrap_qty=0, setup_minutes=None, run_minutes=None, downtime_minutes=None,
                       downtime_reason=None, units=None, finished=True, note='', idempotency_key='', gross_weight=None, stone_weight=None):
    """Stop the clock and post runtime + output (+ backflush) for the operation through one production journal."""
    require(actor, 'execute')
    with transaction.atomic():
        op = _lock_operation(actor, operation)
        order = eng.lock_order(actor, op.order_id)
        eng.assert_executable(order)
        if op.status not in ('STARTED', 'PAUSED', 'READY', 'NOT_STARTED'):
            raise ManufacturingError(f'Operation {op.operation_no} is {op.get_status_display().lower()}.')
        now = dj_timezone.now()
        _clock(op, now)
        clocked, op.clocked_minutes = op.clocked_minutes, ZERO
        op.save()
        setup = get_setup(actor.tenant)
        lines = []
        run = D(run_minutes) if run_minutes not in (None, '') else clocked
        setup_m = D(setup_minutes) if setup_minutes not in (None, '') else ZERO
        down = D(downtime_minutes) if downtime_minutes not in (None, '') else paused_minutes(op)
        if setup.auto_post_runtime and (run or setup_m or down) and not op.subcontracting:
            lines.append({'entry_type': 'RUNTIME', 'operation': op, 'setup_minutes': q3(setup_m), 'run_minutes': q3(run),
                          'downtime_minutes': q3(down), 'downtime_reason': downtime_reason, 'operator': actor.user,
                          'machine_center': op.machine_center, 'start_time': op.actual_start, 'end_time': now})
        if D(output_qty) or D(scrap_qty):
            lines.append({'entry_type': 'OUTPUT', 'operation': op, 'quantity': q3(output_qty), 'scrap_qty': q3(scrap_qty),
                          'output_units': units or [], 'operator': actor.user, 'machine_center': op.machine_center, 'finished': finished,
                          'gross_weight': gross_weight, 'stone_weight': stone_weight})
        OperationEvent.objects.create(tenant=actor.tenant, company=order.company, order=order, operation=op, event='COMPLETE' if finished else 'STOP',
                                      at=now, operator=actor.user, downtime_reason=downtime_reason, note=note, created_by=actor.user)
        batch = None
        if lines:
            batch = eng.quick_post(actor, order, lines, journal_type='PRODUCTION', description=f'Shop floor op {op.operation_no}',
                                   idempotency_key=idempotency_key, rework=op.rework if op.is_rework else None)
        op.refresh_from_db()
        if finished and op.status not in eng.DONE_OPERATION + ('QC_PENDING',):
            eng.mark_operation_complete(op)
            op.save()
        elif not finished and op.status == 'STARTED':
            op.status = 'PAUSED'
            op.save(update_fields=['status', 'updated_at'])
        if op.machine_center_id and finished:
            op.machine_center.status = 'AVAILABLE'
            op.machine_center.save(update_fields=['status', 'updated_at'])
        _ready_next(op)
        order.refresh_from_db()
        eng.derive_status(order, actor=actor)
        order.save(update_fields=['status', 'updated_at'])
        audit(actor, 'complete_operation' if finished else 'stop_operation', 'PRODUCTION_ORDER', order.order_no, order=order,
              new={'operation': op.operation_no, 'output': output_qty, 'scrap': scrap_qty, 'run_minutes': run}, batch=batch)
        return op, batch


def _ready_next(op):
    following = op.order.operations.filter(is_rework=False, status='NOT_STARTED', sequence__gt=op.sequence).order_by('sequence').first()
    if following is not None and (op.output_qty > 0 or op.status in eng.DONE_OPERATION):
        following.status = 'READY'
        following.save(update_fields=['status', 'updated_at'])


# ---------------------------------------------------------------------------
# HUID / hallmark, QC, rework
# ---------------------------------------------------------------------------

@transaction.atomic
def assign_huid(actor, order, unit, *, huid=None, hallmark_status=None, hallmark_date=None, assay_centre=None, certificate_no=None):
    if not (can(actor, 'qc') or can(actor, 'execute')):
        require(actor, 'qc')
    output_unit = OutputUnit.objects.select_for_update().filter(tenant=actor.tenant, order=order, jewellery_unit=unit, reversed=False).first()
    if output_unit is None:
        raise ManufacturingError(f'{unit.barcode} was not produced by {order.order_no}.')
    unit = JewelleryUnit.objects.select_for_update().get(pk=unit.pk, tenant=actor.tenant)
    old = {'huid': unit.huid or '', 'hallmark': output_unit.hallmark_status, 'certificate': unit.certificate_no}
    if huid is not None and huid.strip():
        huid = huid.strip().upper()
        if len(huid) != 6 or not huid.isalnum():
            raise ManufacturingError('HUID must be 6 alphanumeric characters.')
        if JewelleryUnit.objects.filter(tenant=actor.tenant, huid=huid).exclude(pk=unit.pk).exists():
            raise ManufacturingError(f'HUID {huid} is already assigned to another piece.')
        unit.huid = huid
    if certificate_no is not None:
        unit.certificate_no = output_unit.certificate_no = certificate_no.strip()
    if hallmark_status:
        output_unit.hallmark_status = hallmark_status
        if hallmark_status == 'HALLMARKED':
            unit.hallmark = unit.hallmark or 'BIS'
    if hallmark_date is not None:
        output_unit.hallmark_date = hallmark_date
    if assay_centre is not None:
        output_unit.assay_centre = assay_centre.strip()
    unit.version += 1
    unit.updated_by = actor.user
    unit.save()
    output_unit.save()
    audit(actor, 'assign_huid', 'JEWELLERY_UNIT', unit.unit_no, order=order, old=old,
          new={'huid': unit.huid or '', 'hallmark': output_unit.hallmark_status, 'certificate': unit.certificate_no})
    return output_unit


@transaction.atomic
def create_qc(actor, order, *, stage='FINAL', operation=None, inspected_qty=0, passed_qty=0, failed_qty=0, rework_qty=0, hold_qty=0,
              result='PASS', remarks='', parameters=None):
    """Record an inspection. `parameters`: [(QualityParameter, actual_value, 'PASS'|'FAIL', unit_or_None)]."""
    require(actor, 'qc')
    qc = ProductionQC.objects.create(tenant=actor.tenant, company=order.company, qc_no=next_number(actor.tenant, 'PRODUCTION_QC'), order=order,
                                     operation=operation, stage=stage, inspected_qty=q3(inspected_qty), passed_qty=q3(passed_qty),
                                     failed_qty=q3(failed_qty), rework_qty=q3(rework_qty), hold_qty=q3(hold_qty), result=result,
                                     inspector=actor.user, remarks=remarks, created_by=actor.user)
    for parameter, actual, outcome, unit in parameters or []:
        expected = ''
        if parameter.min_value is not None or parameter.max_value is not None:
            expected = f'{parameter.min_value if parameter.min_value is not None else ""}–{parameter.max_value if parameter.max_value is not None else ""}'
            try:
                value = D(actual)
                if (parameter.min_value is not None and value < parameter.min_value) or (parameter.max_value is not None and value > parameter.max_value):
                    outcome = 'FAIL'
            except Exception:
                pass
        ProductionQCLine.objects.create(tenant=actor.tenant, company=order.company, qc=qc, parameter=parameter, jewellery_unit=unit,
                                        expected_value=expected, actual_value=str(actual)[:60], result=outcome, created_by=actor.user)
    audit(actor, 'create', 'PRODUCTION_QC', qc.qc_no, order=order, new={'stage': stage, 'inspected': inspected_qty})
    return qc


def pending_qc_units(order):
    return JewelleryUnit.objects.filter(production_output__order=order, production_output__reversed=False, status='QC').order_by('unit_no')


@transaction.atomic
def create_rework(actor, order, *, qc=None, quantity, reason='', operations=None, materials=None):
    """Rework never alters the original history: it adds rework operations/material to the order, costed separately."""
    if not (can(actor, 'qc') or can(actor, 'release')):
        require(actor, 'release')
    order = eng.lock_order(actor, order)
    rework = ProductionRework.objects.create(tenant=actor.tenant, company=order.company, rework_no=next_number(actor.tenant, 'REWORK_ORDER'),
                                             order=order, qc=qc, quantity=q3(quantity), reason=reason[:250], created_by=actor.user)
    last_seq = order.operations.order_by('-sequence').values_list('sequence', flat=True).first() or 0
    final = eng.final_operation(order)
    specs = operations or [{'work_center': (final.work_center if final else get_setup(actor.tenant).default_work_center),
                            'description': 'Rework', 'minutes': 0}]
    number = order.reworks.count()
    for index, spec in enumerate(specs, 1):
        if spec.get('work_center') is None:
            continue
        base = final
        operation = ProductionOrderRoutingLine.objects.create(
            tenant=actor.tenant, company=order.company, order=order, operation_no=f'R{number}{index}', sequence=last_seq + index,
            description=spec.get('description') or 'Rework', work_center=spec['work_center'], is_rework=True, rework=rework,
            status='READY', run_time=D(spec.get('minutes') or 0), planned_run_minutes=D(spec.get('minutes') or 0),
            planned_total_minutes=D(spec.get('minutes') or 0),
            labour_rate=base.labour_rate if base else spec['work_center'].direct_labour_rate + spec['work_center'].indirect_labour_rate,
            machine_rate=base.machine_rate if base else ZERO, overhead_rate=base.overhead_rate if base else spec['work_center'].overhead_rate,
            created_by=actor.user)
        ProductionReworkLine.objects.create(tenant=actor.tenant, company=order.company, rework=rework, line_type='OPERATION',
                                            work_center=spec['work_center'], description=operation.description,
                                            planned_minutes=operation.planned_total_minutes, operation=operation, created_by=actor.user)
    last_line = order.components.order_by('-line_no').values_list('line_no', flat=True).first() or 0
    for index, spec in enumerate(materials or [], 1):
        item = spec['item']
        component = ProductionOrderComponent.objects.create(
            tenant=actor.tenant, company=order.company, order=order, line_no=last_line + index * 100, item=item, uom=item.base_uom,
            description=f'Rework {rework.rework_no}: {item.description}', location=order.location, bin=order.material_bin,
            qty_per=ZERO, expected_qty=q3(spec['quantity']), unit_cost=component_unit_cost(actor.tenant, _ItemRef(item), order.location),
            flushing_method='MANUAL', optional=True, metal=item.metal if item.metal in ('GOLD', 'SILVER', 'PLATINUM') else '',
            purity=item.purity, created_by=actor.user)
        ProductionReworkLine.objects.create(tenant=actor.tenant, company=order.company, rework=rework, line_type='MATERIAL', item=item,
                                            quantity=q3(spec['quantity']), component=component, created_by=actor.user)
    audit(actor, 'create', 'REWORK_ORDER', rework.rework_no, order=order, new={'qty': quantity, 'qc': qc.qc_no if qc else ''}, reason=reason)
    return rework


class _ItemRef:
    """Adapter so component_unit_cost can price a bare item."""
    def __init__(self, item):
        self.item, self.variant, self.location, self.component_cost = item, None, None, ZERO


@transaction.atomic
def complete_rework(actor, rework):
    require(actor, 'execute')
    rework = ProductionRework.objects.select_for_update().get(pk=rework.pk, tenant=actor.tenant)
    if rework.status not in ('OPEN', 'IN_PROGRESS'):
        raise ManufacturingError(f'{rework.rework_no} is {rework.get_status_display().lower()}.')
    open_ops = [op.operation_no for op in rework.operations.exclude(status__in=eng.DONE_OPERATION)]
    if open_ops:
        raise ManufacturingError(f'Complete rework operation(s) {", ".join(open_ops)} first.')
    rework.status, rework.completed_at = 'COMPLETED', dj_timezone.now()
    rework.save(update_fields=['status', 'completed_at', 'updated_at'])
    if rework.qc_id and rework.qc.stage == 'OPERATION' and rework.qc.operation_id:
        operation = rework.qc.operation
        operation.status = 'QC_PENDING'
        operation.save(update_fields=['status', 'updated_at'])
    audit(actor, 'complete', 'REWORK_ORDER', rework.rework_no, order=rework.order, new={'status': 'COMPLETED'})
    return rework


# ---------------------------------------------------------------------------
# Subcontracting
# ---------------------------------------------------------------------------

@transaction.atomic
def create_subcontract(actor, order, operation, *, quantity=None, expected_return_date=None):
    require(actor, 'release')
    setup = get_setup(actor.tenant)
    if not setup.subcontracting_enabled:
        raise ManufacturingError('Subcontracting is disabled in manufacturing setup.')
    if not operation.subcontracting or operation.subcontractor is None:
        raise ManufacturingError(f'Operation {operation.operation_no} is not a subcontracted operation.')
    sub = operation.subcontractor
    if sub.location is None:
        raise ManufacturingError(f'Subcontractor {sub.code} has no inventory location for material held at the vendor.')
    order = eng.lock_order(actor, order)
    eng.assert_executable(order)
    record = SubcontractOrder.objects.create(
        tenant=actor.tenant, company=order.company, subcontract_no=next_number(actor.tenant, 'SUBCONTRACT_ORDER'), order=order, operation=operation,
        subcontractor=sub, vendor_location=sub.location, quantity=q3(quantity or order.planned_qty),
        expected_return_date=expected_return_date or dj_timezone.localdate() + timedelta(days=sub.lead_time_days), created_by=actor.user)
    audit(actor, 'create', 'SUBCONTRACT_ORDER', record.subcontract_no, order=order, new={'vendor': sub.code, 'operation': operation.operation_no})
    return record


def send_to_subcontractor(actor, subcontract, lines):
    """Issued material leaves the production bin for the vendor location (still PICKED - it belongs to this order)."""
    require(actor, 'execute')
    with transaction.atomic():
        subcontract = SubcontractOrder.objects.select_for_update().get(pk=subcontract.pk, tenant=actor.tenant)
        if subcontract.status not in ('OPEN', 'SENT', 'PARTIALLY_RECEIVED'):
            raise ManufacturingError(f'{subcontract.subcontract_no} is {subcontract.get_status_display().lower()}.')
        order = eng.lock_order(actor, subcontract.order_id)
        eng.assert_executable(order)
        engine = eng.ManufacturingPostingEngine(actor, order, kind='SUBCONTRACT')
        for spec in lines:
            component, unit = spec['component'], spec.get('unit')
            qty = Decimal('1') if unit is not None else q3(spec['quantity'])
            if qty <= 0:
                continue
            if qty > component.issued_open_qty:
                raise ManufacturingError(f'Only {component.issued_open_qty} of {component.item.item_no} is issued to production - issue it first.')
            outs, ins = engine.inventory.move(transaction_type='PRODUCTION', item=component.item, variant=component.variant,
                                              from_location=order.location, to_location=subcontract.vendor_location,
                                              from_bin=order.production_bin if unit is None else None, quantity=qty, unit=unit,
                                              consume_state='PICKED', into_state='PICKED', line_no=component.line_no)
            gross = sum((e.gross_weight for e in ins), ZERO) or (qty if component.is_metal else ZERO)
            SubcontractLine.objects.create(tenant=actor.tenant, company=order.company, subcontract=subcontract, component=component,
                                           jewellery_unit=unit, qty_sent=qty, gross_sent=gross, created_by=actor.user)
            subcontract.weight_sent += gross
        subcontract.status = 'SENT'
        subcontract.save()
        engine.extra['subcontract'] = {'no': subcontract.subcontract_no, 'action': 'send', 'weight': str(subcontract.weight_sent)}
        return engine.finalize(action='subcontract_send')


def receive_from_subcontractor(actor, subcontract, *, returns, service_cost=0, invoice_reference='', purchase_invoice=None, output_qty=0,
                               close=False):
    """Processed material comes back to the production bin; on close the unreturned weight is consumed as vendor loss and
    the service cost enters WIP."""
    require(actor, 'execute')
    with transaction.atomic():
        subcontract = SubcontractOrder.objects.select_for_update().get(pk=subcontract.pk, tenant=actor.tenant)
        if subcontract.status not in ('SENT', 'PARTIALLY_RECEIVED'):
            raise ManufacturingError(f'{subcontract.subcontract_no} has nothing at the vendor.')
        order = eng.lock_order(actor, subcontract.order_id)
        eng.assert_executable(order)
        engine = eng.ManufacturingPostingEngine(actor, order, kind='SUBCONTRACT')
        for spec in returns:
            line = SubcontractLine.objects.select_for_update().get(pk=spec['line'].pk, subcontract=subcontract)
            qty = q3(spec.get('quantity') or 0)
            if qty <= 0:
                continue
            if qty > line.qty_at_vendor:
                raise ManufacturingError(f'Only {line.qty_at_vendor} of {line.component.item.item_no} is at the vendor.')
            outs, ins = engine.inventory.move(transaction_type='PRODUCTION', item=line.component.item, variant=line.component.variant,
                                              from_location=subcontract.vendor_location, to_location=order.location, to_bin=order.production_bin,
                                              quantity=qty, unit=line.jewellery_unit, consume_state='PICKED', into_state='PICKED',
                                              line_no=line.component.line_no)
            gross = D(spec.get('gross_weight')) if spec.get('gross_weight') not in (None, '') else sum((e.gross_weight for e in ins), ZERO)
            line.qty_returned += qty
            line.gross_returned += gross
            line.save()
            subcontract.weight_returned += gross
        if close:
            for line in subcontract.lines.select_related('component__item', 'component__uom'):
                loss = line.qty_at_vendor
                if loss > 0:
                    component = ProductionOrderComponent.objects.select_for_update().get(pk=line.component_id)
                    engine.consume(component, loss, location=subcontract.vendor_location, from_state='PICKED', source='SUBCONTRACT_LOSS',
                                   unit=line.jewellery_unit)
                    line.qty_lost += loss
                    line.save()
                    subcontract.loss_weight += loss if component.is_metal else ZERO
            if subcontract.weight_sent and subcontract.subcontractor.allowed_loss_percent:
                allowed = subcontract.weight_sent * subcontract.subcontractor.allowed_loss_percent / 100
                if subcontract.loss_weight > allowed:
                    engine.notes.append(f'Vendor loss {subcontract.loss_weight} g exceeds the allowed {q3(allowed)} g.')
            subcontract.status, subcontract.actual_return_date = 'RECEIVED', dj_timezone.localdate()
        else:
            subcontract.status = 'PARTIALLY_RECEIVED' if any(l.qty_at_vendor for l in subcontract.lines.all()) else 'RECEIVED'
            if subcontract.status == 'RECEIVED':
                subcontract.actual_return_date = dj_timezone.localdate()
        if D(service_cost) > 0:
            engine.cost('SUBCONTRACT', service_cost, source=subcontract,
                        description=f'{subcontract.subcontractor.code} service {invoice_reference or subcontract.subcontract_no}')
            subcontract.service_cost += money(service_cost)
            subcontract.operation.actual_cost += money(service_cost)
            subcontract.operation.save(update_fields=['actual_cost', 'updated_at'])
        subcontract.invoice_reference = invoice_reference or subcontract.invoice_reference
        subcontract.purchase_invoice = purchase_invoice or subcontract.purchase_invoice
        subcontract.save()
        if D(output_qty) > 0:
            engine.output(operation=subcontract.operation, quantity=output_qty)
        engine.extra['subcontract'] = {'no': subcontract.subcontract_no, 'action': 'receive', 'returned': str(subcontract.weight_returned),
                                       'loss': str(subcontract.loss_weight), 'service_cost': str(money(service_cost))}
        return engine.finalize(action='subcontract_receive')


# ---------------------------------------------------------------------------
# Planning (MRP) and capacity
# ---------------------------------------------------------------------------

OPEN_SALES_STATUSES = ('approved', 'released', 'partially_fulfilled')


def run_planning(actor, *, horizon_days=30, location=None):
    """Net demand (open sales orders, SKU reorder points, safety stock) against stock and open production,
    then explode suggestions level by level into sub-assembly production and component transfer / purchase."""
    require(actor, 'plan')
    from erp.models import SalesOrderLine
    from inventory.models import SKU
    setup = get_setup(actor.tenant)
    production_location = location or setup.default_production_location
    if production_location is None:
        raise ManufacturingError('Set a default production location in manufacturing setup first.')
    fg_location = setup.default_finished_goods_location or production_location
    horizon = dj_timezone.localdate() + timedelta(days=horizon_days)
    with transaction.atomic():
        run = PlanningRun.objects.create(tenant=actor.tenant, run_no=next_number(actor.tenant, 'PLANNING_RUN'), horizon_days=horizon_days,
                                         parameters={'location': production_location.code}, created_by=actor.user)
        demand = {}
        items = {b.item_id: b.item for b in ProductionBOM.objects.filter(tenant=actor.tenant, blocked=False).select_related('item')
                 if b.active_version()}
        product_map = {i.legacy_product_id: i for i in items.values() if i.legacy_product_id}

        def entry_for(item_id, due):
            return demand.setdefault(item_id, {'sales': ZERO, 'reorder': ZERO, 'due': due, 'sources': []})

        if product_map:
            lines = SalesOrderLine.objects.filter(sales_order__status__in=OPEN_SALES_STATUSES, product_id__in=product_map) \
                .select_related('sales_order')
            for line in lines:
                due = line.sales_order.expected_delivery_date or dj_timezone.localdate()
                if due > horizon or line.remaining_quantity <= 0:
                    continue
                entry = entry_for(product_map[line.product_id].pk, due)
                entry['sales'] += line.remaining_quantity
                entry['due'] = min(entry['due'], due)
                entry['sources'].append(line.sales_order.order_no)
        for sku in SKU.objects.filter(tenant=actor.tenant, item_id__in=items, active=True, blocked=False):
            available = available_qty(actor.tenant, item=sku.item, location=sku.location, variant=sku.variant)
            target = max(sku.reorder_point, sku.safety_stock, sku.minimum_stock)
            if target and available < target:
                entry = entry_for(sku.item_id, dj_timezone.localdate() + timedelta(days=sku.lead_time_days or 7))
                entry['reorder'] += max((sku.maximum_stock or target) - available, sku.reorder_quantity)  # already net of stock
                entry['sources'].append(f'Reorder {sku.code}')
        context = {'allocated': {}}
        suggestions = 0
        for item_id, entry in demand.items():
            item = items[item_id]
            stock = available_qty(actor.tenant, item=item, location=fg_location)
            open_production = _open_production(actor.tenant, item)
            gross = entry['sales'] + entry['reorder']
            net = max(entry['sales'] - stock, ZERO) + entry['reorder'] - open_production
            if net <= 0:
                continue
            suggestions += _suggest(actor, run, item, net, entry['due'], production_location, ', '.join(entry['sources'])[:200],
                                    level=0, demand_qty=gross, supply_qty=gross - net, context=context)
        run.summary = {'end_items': len(demand), 'suggestions': suggestions}
        run.save(update_fields=['summary', 'updated_at'])
        audit(actor, 'planning_run', 'PLANNING_RUN', run.run_no, new=run.summary)
    return run


def _open_production(tenant, item):
    return sum((o.remaining_qty for o in ProductionOrder.objects.filter(tenant=tenant, item=item).exclude(
        status__in=ProductionOrder.CLOSED_STATUSES)), ZERO)


def _free(actor, context, item, location):
    """Stock at `location` not yet allocated to an earlier suggestion of this run."""
    key = (item.pk, location.pk)
    return available_qty(actor.tenant, item=item, location=location) - context['allocated'].get(key, ZERO)


def _allocate(context, item, location, qty):
    key = (item.pk, location.pk)
    context['allocated'][key] = context['allocated'].get(key, ZERO) + qty


def _suggest(actor, run, item, qty, due, location, source, *, level, demand_qty, supply_qty, context, parent=None):
    profile = profile_of(item)
    if profile and profile.lot_size and profile.lot_size > 0:
        lots = (qty / profile.lot_size).to_integral_value(rounding='ROUND_CEILING')
        qty = lots * profile.lot_size
    lead = profile.production_lead_days if profile else 7
    suggestion = PlanningSuggestion.objects.create(tenant=actor.tenant, run=run, suggestion_type='PRODUCTION', level=level, item=item,
                                                   location=location, demand_qty=demand_qty, supply_qty=supply_qty, quantity=q3(qty),
                                                   due_date=due, demand_source=source, parent=parent, created_by=actor.user)
    count = 1
    bom = ProductionBOM.objects.filter(tenant=actor.tenant, item=item, blocked=False).first()
    version = bom.active_version() if bom else None
    if version is None or level > 6:
        return count
    component_due = due - timedelta(days=lead)
    parent_weights = _parent_weights(version, item, qty)
    for line in version.lines.exclude(component_type__in=('BY_PRODUCT', 'CO_PRODUCT', 'SCRAP')).select_related('component_item'):
        per_piece = per_piece_requirement(basis=line.consumption_basis, quantity=line.quantity, base_quantity=version.base_quantity,
                                          formula=line.formula, parent=parent_weights)
        need = component_requirement(per_piece=per_piece, production_qty=qty, scrap_percent=line.scrap_percent,
                                     loss_percent=line.expected_loss_percent, fixed_scrap=line.fixed_scrap_qty)['expected']
        component = line.component_item
        here = max(_free(actor, context, component, location), ZERO)
        _allocate(context, component, location, min(here, need))
        net = need - here
        if net <= 0:
            continue
        sub_bom = ProductionBOM.objects.filter(tenant=actor.tenant, item=component, blocked=False).first()
        if sub_bom is not None and sub_bom.active_version():
            count += _suggest(actor, run, component, net, component_due, location, f'Component of {item.item_no}', level=level + 1,
                              demand_qty=need, supply_qty=here, context=context, parent=suggestion)
            continue
        source_row = InventoryBalance.objects.filter(tenant=actor.tenant, item=component).exclude(location=location) \
            .exclude(location__location_type='TRANSIT').values('location').annotate(q=Sum('available_qty')).filter(q__gt=0).order_by('-q').first()
        if source_row is not None:
            take = min(net, source_row['q'])
            PlanningSuggestion.objects.create(tenant=actor.tenant, run=run, suggestion_type='TRANSFER', level=level + 1, item=component,
                                              location=location, source_location_id=source_row['location'], demand_qty=need, supply_qty=here,
                                              quantity=q3(take), due_date=component_due, demand_source=f'Component of {item.item_no}',
                                              parent=suggestion, created_by=actor.user)
            count += 1
            net -= take
        if net > 0:
            PlanningSuggestion.objects.create(tenant=actor.tenant, run=run, suggestion_type='PURCHASE', level=level + 1, item=component,
                                              location=location, demand_qty=need, supply_qty=here, quantity=q3(net), due_date=component_due,
                                              demand_source=f'Component of {item.item_no}', parent=suggestion, created_by=actor.user)
            count += 1
    return count


def carry_out(actor, suggestions, *, firm=False):
    """Accept suggestions: production -> planned / firm planned orders, transfer -> inventory transfer requests,
    purchase -> accepted (handed to purchasing)."""
    require(actor, 'plan')
    results = []
    for suggestion in suggestions:
        if suggestion.status != 'SUGGESTED':
            continue
        with transaction.atomic():
            if suggestion.suggestion_type == 'PRODUCTION':
                order = create_order(actor, item=suggestion.item, quantity=suggestion.quantity, location=suggestion.location,
                                     due_date=suggestion.due_date, source_type='PLANNING', source_no=suggestion.run.run_no,
                                     status='FIRM_PLANNED' if firm else 'PLANNED')
                suggestion.production_order, suggestion.status = order, 'CONVERTED'
                results.append(order.order_no)
            elif suggestion.suggestion_type == 'TRANSFER':
                request = create_transfer_request(actor, to_location=suggestion.location, from_location=suggestion.source_location,
                                                  lines=[{'item': suggestion.item, 'variant': suggestion.variant, 'quantity': suggestion.quantity}],
                                                  reason=f'Planning {suggestion.run.run_no}')
                suggestion.transfer_request, suggestion.status = request, 'CONVERTED'
                results.append(request.request_no)
            else:
                suggestion.status = 'ACCEPTED'
                results.append(f'Purchase {suggestion.item.item_no} {suggestion.quantity}')
            suggestion.save()
    audit(actor, 'carry_out', 'PLANNING_RUN', suggestions[0].run.run_no if suggestions else '', new={'results': '; '.join(results)})
    return results


def capacity_load(tenant, *, start=None, days=14, work_centers=None):
    """Available vs planned vs actual minutes per work centre per day."""
    from .models import RuntimeEntry, WorkCenter
    from .calc import day_capacity
    setup = get_setup(tenant)
    start = start or dj_timezone.localdate()
    end = start + timedelta(days=days - 1)
    centers = work_centers if work_centers is not None else WorkCenter.objects.filter(tenant=tenant, active=True).select_related('calendar')
    open_ops = ProductionOrderRoutingLine.objects.filter(tenant=tenant, order__status__in=ProductionOrder.PLANNING_STATUSES + ProductionOrder.EXECUTION_STATUSES,
                                                         planned_start__isnull=False).exclude(status__in=eng.DONE_OPERATION)
    actuals = RuntimeEntry.objects.filter(tenant=tenant, posting_date__range=(start, end)).values('work_center', 'posting_date') \
        .annotate(m=Sum('setup_minutes') + Sum('run_minutes'))
    actual_map = {(a['work_center'], a['posting_date']): a['m'] for a in actuals}
    rows = []
    for wc in centers:
        calendar = wc.calendar or setup.default_calendar
        days_list = []
        load = {}
        for op in open_ops.filter(work_center=wc):
            remaining = max(op.planned_total_minutes - op.actual_capacity_minutes, ZERO)
            if not remaining:
                continue
            op_start = dj_timezone.localtime(op.planned_start).date()
            op_end = dj_timezone.localtime(op.planned_end).date() if op.planned_end else op_start
            span = [op_start + timedelta(days=i) for i in range((op_end - op_start).days + 1)] or [op_start]
            working = [d for d in span if day_capacity(calendar, d, None, wc.capacity, wc.working_minutes_per_day) > 0] or span
            for day in working:
                load[day] = load.get(day, ZERO) + remaining / len(working)
        total_available = total_load = total_actual = ZERO
        for offset in range(days):
            day = start + timedelta(days=offset)
            available = day_capacity(calendar, day, None, wc.capacity, wc.working_minutes_per_day)
            planned = q3(load.get(day, ZERO))
            actual = actual_map.get((wc.pk, day), ZERO) or ZERO
            days_list.append({'day': day, 'available': available, 'planned': planned, 'actual': actual,
                              'load_percent': int(planned / available * 100) if available else (100 if planned else 0)})
            total_available += available
            total_load += planned
            total_actual += actual
        overdue = sum((max(o.planned_total_minutes - o.actual_capacity_minutes, ZERO) for o in open_ops.filter(work_center=wc)
                       if dj_timezone.localtime(o.planned_start).date() < start), ZERO)
        rows.append({'work_center': wc, 'days': days_list, 'available': total_available, 'planned': q3(total_load), 'actual': total_actual,
                     'backlog': q3(overdue), 'load_percent': int(total_load / total_available * 100) if total_available else 0,
                     'utilisation_percent': int(total_actual / total_available * 100) if total_available else 0})
    return rows
