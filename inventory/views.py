import csv
from decimal import Decimal, InvalidOperation
from functools import wraps
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date

from . import imports, reports, services
from .engine import D, InventoryError, availability_by_location, availability_totals
from .forms import MASTERS, JewelleryUnitForm, LocationForm
from .models import (
    Bin, ImportBatch, InventoryAdjustment, InventoryAuditLog, InventoryBalance, InventoryLedgerEntry, InventoryReservation, Item,
    JewelleryUnit, Location, PhysicalCount, ReplenishmentLine, SKU, TransferOrder, TransferReceiptLine, TransferRequest, ZERO,
)
from .tenancy import allowed_locations, require_location

MENU = [
    ('Overview', [('inventory_dashboard', 'Inventory dashboard'), ('inventory_transfer_dashboard', 'Transfer dashboard')]),
    ('Items', [('inventory_items', 'Items'), ('inventory_skus', 'SKUs'), ('inventory_units', 'Jewellery units'),
               ('inventory_trace', 'Where is my jewellery?')]),
    ('Locations', [('inventory_locations', 'Locations'), ('inventory_bins', 'Bins & bin contents'),
                   ('inventory_setup:zones', 'Zones'), ('inventory_setup:routes', 'Transfer routes'),
                   ('inventory_setup:location-users', 'Location permissions')]),
    ('Stock', [('inventory_availability', 'Availability'), ('inventory_ledger', 'Inventory ledger'),
               ('inventory_reservations', 'Reservations')]),
    ('Transfers', [('inventory_requests', 'Transfer requests'), ('inventory_transfers', 'Transfer orders'),
                   ('inventory_transit', 'Inventory in transit')]),
    ('Journals', [('inventory_adjustments', 'Adjustments'), ('inventory_reclass', 'Reclassification'),
                  ('inventory_counts', 'Physical inventory'), ('inventory_imports', 'Opening inventory & imports')]),
    ('Planning', [('inventory_replenishment', 'Replenishment worksheet'), ('inventory_setup:approval-rules', 'Approval rules'),
                  ('inventory_setup:periods', 'Inventory periods'), ('inventory_setup:channel-allocations', 'Channel allocation')]),
    ('Reports', [('inventory_stock_report', 'Stock by location'), ('inventory_valuation', 'Inventory valuation'),
                 ('inventory_transfer_register', 'Transfer register'), ('inventory_transit', 'Transit aging')]),
]


def menu_links():
    links = []
    for group, entries in MENU:
        items = []
        for name, label in entries:
            if ':' in name:
                url_name, kind = name.split(':')
                items.append((reverse(url_name, args=[kind]), label))
            else:
                items.append((reverse(name), label))
        links.append((group, items))
    return links


def inventory_view(view):
    """Login + tenant resolution. Posting errors become messages and bounce back to the page."""
    @login_required(login_url='login')
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        actor = services.actor_from_request(request)
        try:
            return view(request, actor, *args, **kwargs)
        except (InventoryError, PermissionDenied) as exc:
            if request.method != 'POST':
                raise PermissionDenied(str(exc)) if isinstance(exc, PermissionDenied) else Http404(str(exc))
            messages.error(request, str(exc) or 'You do not have permission to do that.')
            return redirect(request.get_full_path())
    return wrapper


def page(request, template, **context):
    context.setdefault('inventory_menu', menu_links())
    return render(request, f'inventory/{template}', context)


def get_obj(model, actor, pk):
    obj = model.objects.filter(tenant=actor.tenant, pk=pk).first()
    if obj is None:
        raise Http404
    return obj


def permitted(actor, action='view'):
    return allowed_locations(actor.tenant, actor.user, action)


def selected_locations(request, actor):
    locations = permitted(actor)
    chosen = request.GET.get('location')
    if chosen:
        location = get_obj(Location, actor, chosen)
        require_location(actor.tenant, actor.user, location, 'view')
        return [location], location
    return locations, None


def export(rows, headers, filename, fmt):
    if fmt == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
        writer = csv.writer(response)
        writer.writerow(headers)
        writer.writerows(rows)
        return response
    if fmt == 'pdf':
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
        buffer = BytesIO()
        table = Table([headers] + [[str(c) for c in row] for row in rows], repeatRows=1)
        table.setStyle(TableStyle([('FONTSIZE', (0, 0), (-1, -1), 7), ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8eff1')),
                                   ('GRID', (0, 0), (-1, -1), .25, colors.HexColor('#c6d3d7'))]))
        SimpleDocTemplate(buffer, pagesize=landscape(A4)).build([table])
        response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}.pdf"'
        return response
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append([float(c) if isinstance(c, Decimal) else (str(c) if c is not None and not isinstance(c, (int, float)) else c) for c in row])
    buffer = BytesIO()
    workbook.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
    return response


def parse_lines(text):
    """'CODE, qty' per line (qty optional) -> [(code, Decimal)]; used by scan/paste boxes."""
    result = []
    for raw in (text or '').splitlines():
        parts = [p.strip() for p in raw.replace('\t', ',').split(',') if p.strip()]
        if not parts:
            continue
        try:
            result.append((parts[0], Decimal(parts[1]) if len(parts) > 1 else None))
        except InvalidOperation:
            raise InventoryError(f'"{raw}" - quantity is not a number.')
    return result


def resolve_stock_lines(actor, text, location):
    """Turn pasted/scanned lines into engine line specs: jewellery barcode, SKU code/barcode or item number."""
    lines = []
    for code, qty in parse_lines(text):
        unit = services.find_unit(actor.tenant, code)
        if unit is not None:
            lines.append({'unit': unit})
            continue
        sku = SKU.objects.filter(tenant=actor.tenant, location=location).filter(Q(code=code) | Q(barcode=code)).first() if location else None
        item = sku.item if sku else Item.objects.filter(tenant=actor.tenant, item_no=code).first()
        if item is None:
            raise InventoryError(f'{code}: no jewellery unit, SKU or item with that code.')
        lines.append({'sku': sku, 'item': item, 'variant': sku.variant if sku else None, 'quantity': qty or Decimal('1')})
    if not lines:
        raise InventoryError('Add at least one line.')
    return lines


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

@inventory_view
def dashboard(request, actor):
    locations, selected = selected_locations(request, actor)
    kpis = reports.inventory_kpis(actor.tenant, locations)
    valuation = reports.valuation_by_location(actor.tenant, locations)
    return page(request, 'dashboard.html', kpis=kpis, valuation=valuation, selected=selected, locations=permitted(actor),
                transfer_kpis=reports.transfer_kpis(actor.tenant, locations), low_stock=reports.low_stock(actor.tenant, locations)[:10],
                totals={k: sum((r[k] for r in valuation), ZERO) for k in ('cost_value', 'current_value', 'retail_value')},
                recent=TransferOrder.objects.filter(tenant=actor.tenant).filter(Q(from_location__in=locations) | Q(to_location__in=locations))
                .select_related('from_location', 'to_location').distinct()[:8])


@inventory_view
def transfer_dashboard(request, actor):
    locations = permitted(actor)
    orders = TransferOrder.objects.filter(tenant=actor.tenant).filter(Q(from_location__in=locations) | Q(to_location__in=locations)).distinct()
    by_location = orders.values('from_location__code').annotate(n=Count('id', distinct=True), qty=Sum('lines__quantity'),
                                                                weight=Sum('lines__gross_weight')).order_by('-n')
    by_metal = orders.values('lines__item__metal').annotate(weight=Sum('lines__gross_weight'), qty=Sum('lines__quantity')).order_by('-weight')
    done = orders.filter(actual_receipt_date__isnull=False, actual_shipment_date__isnull=False)
    days = [(o.actual_receipt_date - o.actual_shipment_date).days for o in done]
    return page(request, 'transfer_dashboard.html', kpis=reports.transfer_kpis(actor.tenant, locations), by_location=by_location,
                by_metal=by_metal, average_days=(sum(days) / len(days)) if days else None,
                overdue=[r for r in reports.transit_rows(actor.tenant, locations) if r['overdue']])


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

@inventory_view
def location_list(request, actor):
    locations = permitted(actor).annotate(bin_count=Count('bins', distinct=True))
    if request.GET.get('q'):
        locations = locations.filter(Q(code__icontains=request.GET['q']) | Q(name__icontains=request.GET['q']) | Q(city__icontains=request.GET['q']))
    if request.GET.get('type'):
        locations = locations.filter(location_type=request.GET['type'])
    stock = {r['location']: r for r in InventoryBalance.objects.filter(tenant=actor.tenant).values('location')
             .annotate(qty=Sum('on_hand_qty'), transit=Sum('in_transit_qty'), value=Sum('cost_value'))}
    rows = [{'location': l, 'qty': (stock.get(l.pk, {}).get('qty') or ZERO) + (stock.get(l.pk, {}).get('transit') or ZERO),
             'value': stock.get(l.pk, {}).get('value') or ZERO} for l in locations]
    if request.GET.get('export'):
        return export([[r['location'].code, r['location'].name, r['location'].location_type, r['location'].city, r['location'].gstin,
                        r['location'].is_warehouse, r['location'].allow_pos, r['location'].bin_mandatory, r['qty'], r['value'],
                        r['location'].active] for r in rows],
                      ['Code', 'Name', 'Type', 'City', 'GSTIN', 'Warehouse', 'POS', 'Bin enabled', 'Inventory qty', 'Inventory value', 'Active'],
                      'locations', request.GET['export'])
    return page(request, 'location_list.html', rows=rows, types=Location.TYPES)


@inventory_view
def location_card(request, actor, pk=None):
    location = get_obj(Location, actor, pk) if pk else None
    if location is not None:
        require_location(actor.tenant, actor.user, location, 'view')
    admin = services.is_tenant_admin(actor.tenant, actor.user)
    form = LocationForm(request.POST or None, instance=location, tenant=actor.tenant)
    if request.method == 'POST':
        if not admin:
            raise PermissionDenied('Only workspace administrators can change locations.')
        if request.POST.get('action') in ('block', 'unblock') and location:
            location.blocked = request.POST['action'] == 'block'
            location.save(update_fields=['blocked', 'updated_at'])
            services.audit(actor, request.POST['action'], 'LOCATION', location.code, location=location)
            return redirect('inventory_location_card', location.pk)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = actor.user
            saved.created_by = saved.created_by or actor.user
            saved.save()
            services.audit(actor, 'update' if location else 'create', 'LOCATION', saved.code, location=saved,
                           new={k: str(v) for k, v in form.cleaned_data.items() if k in form.changed_data})
            messages.success(request, f'Location {saved.code} saved.')
            return redirect('inventory_location_card', saved.pk)
    sections = [(title, [form[name] for name in names]) for title, names in form.sections]
    context = {'form': form, 'sections': sections, 'location': location, 'admin': admin}
    if location is not None:
        context.update(
            bins=location.bins.select_related('zone')[:50], zones=location.zones.all(), sku_count=location.skus.count(),
            availability=availability_totals(availability_by_location(actor.tenant, locations=[location])),
            valuation=reports.valuation_by_location(actor.tenant, [location]),
            transfers=TransferOrder.objects.filter(tenant=actor.tenant).filter(Q(from_location=location) | Q(to_location=location))
            .select_related('from_location', 'to_location')[:10],
        )
    return page(request, 'location_card.html', **context)


@inventory_view
def master_list(request, actor, kind):
    if kind not in MASTERS:
        raise Http404
    title, form_class, ordering = MASTERS[kind]
    model = form_class._meta.model
    rows = model.objects.filter(tenant=actor.tenant).order_by(ordering)
    if hasattr(model, 'location') and kind != 'locations':
        rows = rows.filter(location__in=permitted(actor))
    q = request.GET.get('q')
    if q and hasattr(model, 'code'):
        rows = rows.filter(code__icontains=q)
    fields = [f for f in form_class._meta.fields][:8]
    table = [(row, [getattr(row, f'get_{f}_display')() if hasattr(row, f'get_{f}_display') else getattr(row, f) for f in fields]) for row in rows[:500]]
    return page(request, 'master_list.html', title=title, kind=kind, headers=[model._meta.get_field(f).verbose_name for f in fields], table=table)


@inventory_view
def master_edit(request, actor, kind, pk=None):
    if kind not in MASTERS:
        raise Http404
    if kind == 'locations':
        return redirect('inventory_location_card', pk) if pk else redirect('inventory_location_new')
    title, form_class, _ = MASTERS[kind]
    model = form_class._meta.model
    instance = get_obj(model, actor, pk) if pk else None
    admin = services.is_tenant_admin(actor.tenant, actor.user)
    form = form_class(request.POST or None, instance=instance, tenant=actor.tenant)
    if request.method == 'POST':
        location = form.data.get('location') and Location.objects.filter(tenant=actor.tenant, pk=form.data.get('location')).first()
        if not admin and not (kind in ('skus', 'bins', 'zones') and location
                              and permitted(actor, 'adjust').filter(pk=location.pk).exists()):
            raise PermissionDenied('Only administrators (or users who can adjust this location) can change this setup.')
        if form.is_valid():
            saved = form.save(commit=False)
            saved.created_by = saved.created_by or actor.user
            saved.updated_by = actor.user
            saved.save()
            services.audit(actor, 'update' if instance else 'create', kind.upper(), str(saved.pk),
                           new={k: str(v) for k, v in form.cleaned_data.items() if k in form.changed_data})
            messages.success(request, f'{title[:-1] if title.endswith("s") else title} saved.')
            return redirect('inventory_setup', kind)
    return page(request, 'master_edit.html', title=title, kind=kind, form=form, instance=instance)


@inventory_view
def item_list(request, actor):
    items = Item.objects.filter(tenant=actor.tenant).select_related('base_uom').annotate(sku_count=Count('skus', distinct=True))
    q = request.GET.get('q')
    if q:
        items = items.filter(Q(item_no__icontains=q) | Q(description__icontains=q) | Q(category__icontains=q))
    stock = {r['item']: r for r in InventoryBalance.objects.filter(tenant=actor.tenant, location__in=permitted(actor))
             .values('item').annotate(qty=Sum('on_hand_qty'), available=Sum('available_qty'), gross=Sum('gross_weight'))}
    return page(request, 'item_list.html', rows=[(i, stock.get(i.pk, {})) for i in items[:500]])


@inventory_view
def item_card(request, actor, pk):
    item = get_obj(Item, actor, pk)
    locations = permitted(actor)
    rows = availability_by_location(actor.tenant, item=item, locations=locations)
    return page(request, 'item_card.html', item=item, rows=rows, totals=availability_totals(rows),
                skus=item.skus.filter(location__in=locations).select_related('location', 'variant'), variants=item.variants.all(),
                units=item.units.filter(current_location__in=locations).select_related('current_location', 'current_bin')[:50],
                ledger=InventoryLedgerEntry.objects.filter(tenant=actor.tenant, item=item, location__in=locations)
                .select_related('location', 'user').order_by('-id')[:25],
                reservations=InventoryReservation.objects.filter(tenant=actor.tenant, item=item, status='ACTIVE', location__in=locations)
                .select_related('location'))


@inventory_view
def sku_list(request, actor):
    skus = SKU.objects.filter(tenant=actor.tenant, location__in=permitted(actor)).select_related('item', 'location', 'variant')
    q = request.GET.get('q')
    if q:
        skus = skus.filter(Q(code__icontains=q) | Q(item__item_no__icontains=q) | Q(item__description__icontains=q) | Q(barcode=q))
    if request.GET.get('location'):
        skus = skus.filter(location_id=request.GET['location'])
    if request.GET.get('metal'):
        skus = skus.filter(metal=request.GET['metal'])
    stock = {(r['item'], r['variant'], r['location']): r for r in InventoryBalance.objects.filter(tenant=actor.tenant)
             .values('item', 'variant', 'location').annotate(on_hand=Sum('on_hand_qty'), available=Sum('available_qty'), reserved=Sum('reserved_qty'))}
    rows = [(s, stock.get((s.item_id, s.variant_id, s.location_id), {})) for s in skus[:500]]
    return page(request, 'sku_list.html', rows=rows, locations=permitted(actor))


@inventory_view
def sku_card(request, actor, pk):
    sku = get_obj(SKU, actor, pk)
    require_location(actor.tenant, actor.user, sku.location, 'view')
    if request.method == 'POST' and request.POST.get('action') == 'opening':
        bin_ = get_obj(Bin, actor, request.POST['bin']) if request.POST.get('bin') else None
        services.post_receipt(actor, location=sku.location, sku=sku, quantity=D(request.POST.get('quantity')),
                              unit_cost=D(request.POST['unit_cost']) if request.POST.get('unit_cost') else None, bin=bin_)
        messages.success(request, 'Opening stock posted.')
        return redirect('inventory_sku_card', sku.pk)
    locations = permitted(actor)
    return page(request, 'sku_card.html', sku=sku,
                grid=availability_by_location(actor.tenant, item=sku.item, variant=sku.variant, locations=locations),
                own=availability_totals(availability_by_location(actor.tenant, sku=sku)),
                bins=InventoryBalance.objects.filter(tenant=actor.tenant, item=sku.item, variant=sku.variant, location=sku.location)
                .exclude(on_hand_qty=0).select_related('bin', 'jewellery_unit'),
                transfers=sku.transfer_lines.select_related('transfer', 'transfer__to_location')[:10],
                ledger=InventoryLedgerEntry.objects.filter(tenant=actor.tenant, item=sku.item, variant=sku.variant, location=sku.location)
                .select_related('user').order_by('-id')[:15],
                investigation=reports.stock_investigation(actor.tenant, item=sku.item, variant=sku.variant, location=sku.location),
                location_bins=sku.location.bins.filter(active=True, blocked=False))


@inventory_view
def bin_list(request, actor):
    locations, selected = selected_locations(request, actor)
    bins = Bin.objects.filter(tenant=actor.tenant, location__in=locations).select_related('location', 'zone')
    contents = InventoryBalance.objects.filter(tenant=actor.tenant, location__in=locations, bin__isnull=False).exclude(on_hand_qty=0) \
        .select_related('bin', 'item', 'sku', 'location', 'jewellery_unit').order_by('location__code', 'bin__code')
    if request.GET.get('bin'):
        contents = contents.filter(bin_id=request.GET['bin'])
    return page(request, 'bin_list.html', bins=bins, contents=contents[:500], selected=selected, locations=permitted(actor))


# ---------------------------------------------------------------------------
# Availability, units, trace, ledger
# ---------------------------------------------------------------------------

@inventory_view
def availability(request, actor):
    q = (request.GET.get('q') or '').strip()
    item = variant = unit = None
    if q:
        unit = services.find_unit(actor.tenant, q)
        sku = SKU.objects.filter(tenant=actor.tenant).filter(Q(code=q) | Q(barcode=q)).select_related('item', 'variant').first()
        item = (unit.item if unit else None) or (sku.item if sku else None) or Item.objects.filter(tenant=actor.tenant, item_no__iexact=q).first()
        variant = (sku.variant if sku else None) if not unit else unit.variant
        if item is None:
            messages.warning(request, f'Nothing matches "{q}".')
    locations = permitted(actor)
    if request.GET.get('type'):
        locations = locations.filter(location_type=request.GET['type'])
    if request.GET.get('city'):
        locations = locations.filter(city__iexact=request.GET['city'])
    if request.GET.get('region'):
        locations = locations.filter(region__iexact=request.GET['region'])
    rows = availability_by_location(actor.tenant, item=item, variant=variant, locations=locations) if item else []
    return page(request, 'availability.html', q=q, item=item, unit=unit, rows=rows, totals=availability_totals(rows), types=Location.TYPES)


@inventory_view
def unit_list(request, actor):
    form = JewelleryUnitForm(request.POST or None, tenant=actor.tenant)
    if request.method == 'POST':
        if form.is_valid():
            data = form.cleaned_data
            sku = data.pop('sku')
            unit = services.register_unit(actor, sku=sku, barcode=data.pop('barcode'), serial_no=data.pop('serial_no'),
                                          huid=data.pop('huid'), **data)
            if request.POST.get('receive'):
                services.post_receipt(actor, location=sku.location, sku=sku, unit=unit)
                messages.success(request, f'{unit.unit_no} registered and received into {sku.location.code}.')
            else:
                messages.success(request, f'{unit.unit_no} registered (not yet in stock).')
            return redirect('inventory_units')
    units = JewelleryUnit.objects.filter(tenant=actor.tenant).filter(Q(current_location__in=permitted(actor)) | Q(current_location__isnull=True)) \
        .select_related('item', 'sku', 'current_location', 'current_bin')
    for key, lookup in (('status', 'status'), ('location', 'current_location_id'), ('metal', 'metal'), ('purity', 'purity')):
        if request.GET.get(key):
            units = units.filter(**{lookup: request.GET[key]})
    if request.GET.get('q'):
        q = request.GET['q']
        units = units.filter(Q(barcode__icontains=q) | Q(serial_no__icontains=q) | Q(huid__iexact=q) | Q(unit_no__icontains=q) | Q(item__item_no__icontains=q))
    totals = units.aggregate(n=Count('id'), gross=Sum('gross_weight'), net=Sum('net_metal_weight'), carat=Sum('diamond_carat'), cost=Sum('purchase_cost'))
    return page(request, 'unit_list.html', units=units.order_by('-id')[:500], form=form, statuses=JewelleryUnit.STATUSES,
                locations=permitted(actor), totals=totals)


@inventory_view
def trace(request, actor):
    q = (request.GET.get('q') or '').strip()
    result = reports.trace(actor.tenant, q) if q else None
    if result is not None and result['unit'].current_location is not None:
        require_location(actor.tenant, actor.user, result['unit'].current_location, 'view')
    documents = []
    if q and result is None:
        documents = InventoryLedgerEntry.objects.filter(tenant=actor.tenant, document_no__iexact=q, location__in=permitted(actor)) \
            .select_related('item', 'location', 'jewellery_unit')
    return page(request, 'trace.html', q=q, result=result, documents=documents)


@inventory_view
def ledger(request, actor):
    entries = reports.movement_entries(actor.tenant, permitted(actor), request.GET)
    if request.GET.get('export'):
        return export([[e.posting_date, e.document_type, e.document_no, e.get_transaction_type_display(), e.item.item_no,
                        e.sku.code if e.sku else '', e.location.code, e.from_location.code if e.from_location else '',
                        e.to_location.code if e.to_location else '', e.barcode, e.huid, e.quantity if e.quantity > 0 else '',
                        -e.quantity if e.quantity < 0 else '', e.gross_weight, e.net_weight, e.cost_amount,
                        e.user.username if e.user else ''] for e in entries[:20000]],
                      ['Date', 'Doc type', 'Document', 'Transaction', 'Item', 'SKU', 'Location', 'From', 'To', 'Barcode', 'HUID',
                       'Qty in', 'Qty out', 'Gross wt', 'Net wt', 'Value', 'User'], 'inventory-movement', request.GET['export'])
    totals = entries.aggregate(qty_in=Sum('quantity', filter=Q(quantity__gt=0)), qty_out=Sum('quantity', filter=Q(quantity__lt=0)),
                               weight=Sum('gross_weight'), value=Sum('cost_amount'))
    return page(request, 'ledger.html', entries=entries[:300], totals=totals, locations=permitted(actor),
                transaction_types=InventoryLedgerEntry.TRANSACTION_TYPES, filters=request.GET)


@inventory_view
def stock_card(request, actor, pk):
    sku = get_obj(SKU, actor, pk)
    require_location(actor.tenant, actor.user, sku.location, 'view')
    date_from, date_to = parse_date(request.GET.get('from') or ''), parse_date(request.GET.get('to') or '')
    card = reports.stock_card(actor.tenant, item=sku.item, variant=sku.variant, location=sku.location, date_from=date_from, date_to=date_to)
    return page(request, 'stock_card.html', sku=sku, card=card, date_from=date_from, date_to=date_to,
                investigation=reports.stock_investigation(actor.tenant, item=sku.item, variant=sku.variant, location=sku.location))


@inventory_view
def reservation_list(request, actor):
    if request.method == 'POST':
        reservation = get_obj(InventoryReservation, actor, request.POST.get('reservation'))
        require_location(actor.tenant, actor.user, reservation.location, 'adjust')
        engine = services.InventoryPostingEngine(actor.tenant, actor.user, document_type=reservation.source_type, document_no=reservation.source_no)
        engine.release(reservation)
        services.audit(actor, 'release', 'RESERVATION', reservation.source_no, location=reservation.location)
        messages.success(request, 'Reservation released.')
        return redirect('inventory_reservations')
    reservations = InventoryReservation.objects.filter(tenant=actor.tenant, location__in=permitted(actor), status='ACTIVE') \
        .select_related('item', 'location', 'bin', 'jewellery_unit', 'sku').order_by('-id')
    return page(request, 'reservation_list.html', reservations=reservations[:500])


# ---------------------------------------------------------------------------
# Transfer requests and orders
# ---------------------------------------------------------------------------

@inventory_view
def request_list(request, actor):
    if request.method == 'POST':
        to_location = get_obj(Location, actor, request.POST.get('to_location'))
        lines = [{'item': spec['item'], 'variant': spec.get('variant'), 'quantity': spec.get('quantity', 1)}
                 for spec in resolve_stock_lines(actor, request.POST.get('lines'), None) if 'item' in spec]
        req = services.create_transfer_request(actor, to_location=to_location, lines=lines, priority=request.POST.get('priority', 'NORMAL'),
                                               from_location=get_obj(Location, actor, request.POST['from_location']) if request.POST.get('from_location') else None,
                                               reason=request.POST.get('reason', ''))
        messages.success(request, f'Request {req.request_no} created.')
        return redirect('inventory_request_detail', req.pk)
    locations = permitted(actor)
    requests_ = TransferRequest.objects.filter(tenant=actor.tenant).filter(Q(to_location__in=locations) | Q(from_location__in=locations)) \
        .distinct().select_related('to_location', 'from_location', 'requested_by') \
        .annotate(item_count=Count('lines'), qty=Sum('lines__requested_qty'))
    if request.GET.get('status'):
        requests_ = requests_.filter(status=request.GET['status'])
    if request.GET.get('export'):
        return export([[r.request_no, r.request_date, r.from_location.code if r.from_location else '', r.to_location.code,
                        r.requested_by.username, r.priority, r.item_count, r.qty, r.status] for r in requests_],
                      ['Request', 'Date', 'From', 'To', 'Requested by', 'Priority', 'Items', 'Qty', 'Status'], 'transfer-requests', request.GET['export'])
    return page(request, 'request_list.html', requests=requests_[:300], locations=Location.objects.filter(tenant=actor.tenant, active=True).exclude(location_type='TRANSIT'),
                my_locations=permitted(actor, 'create_transfer'), statuses=TransferRequest.STATUSES)


@inventory_view
def request_detail(request, actor, pk):
    req = get_obj(TransferRequest, actor, pk)
    if not permitted(actor).filter(pk__in=[req.to_location_id, req.from_location_id]).exists():
        raise Http404
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'approve':
            quantities = {line.pk: request.POST.get(f'approve_{line.pk}', line.requested_qty) for line in req.lines.all()}
            source = get_obj(Location, actor, request.POST['from_location']) if request.POST.get('from_location') else None
            services.approve_transfer_request(actor, req, approved_quantities=quantities, from_location=source)
            req.refresh_from_db()
            messages.success(request, f'Approved and converted to transfer {req.transfer_order.transfer_no}.')
        elif action == 'reject':
            services.reject_transfer_request(actor, req, request.POST.get('reason', ''))
            messages.success(request, 'Request rejected.')
        elif action == 'cancel':
            require_location(actor.tenant, actor.user, req.to_location, 'create_transfer')
            if req.status != 'REQUESTED':
                raise InventoryError('Only open requests can be cancelled.')
            req.status = 'CANCELLED'
            req.save(update_fields=['status', 'updated_at'])
            services.audit(actor, 'cancel', 'TRANSFER_REQUEST', req.request_no)
        return redirect('inventory_request_detail', req.pk)
    lines = []
    for line in req.lines.select_related('item', 'variant'):
        stock = availability_by_location(actor.tenant, item=line.item, variant=line.variant, locations=permitted(actor))
        lines.append((line, stock))
    return page(request, 'request_detail.html', req=req, lines=lines,
                sources=permitted(actor, 'approve_transfer').exclude(pk=req.to_location_id).exclude(location_type='TRANSIT'))


@inventory_view
def transfer_list(request, actor):
    locations = permitted(actor)
    orders = TransferOrder.objects.filter(tenant=actor.tenant).filter(Q(from_location__in=locations) | Q(to_location__in=locations)) \
        .distinct().select_related('from_location', 'to_location')
    tab = request.GET.get('tab', 'open')
    if tab == 'open':
        orders = orders.exclude(status__in=('CLOSED', 'CANCELLED'))
    elif tab == 'closed':
        orders = orders.filter(status__in=('CLOSED', 'CANCELLED'))
    for key, lookup in (('status', 'status'), ('from', 'from_location_id'), ('to', 'to_location_id')):
        if request.GET.get(key):
            orders = orders.filter(**{lookup: request.GET[key]})
    if request.GET.get('q'):
        orders = orders.filter(transfer_no__icontains=request.GET['q'])
    return page(request, 'transfer_list.html', orders=orders[:300], tab=tab, statuses=TransferOrder.STATUSES, locations=locations)


@inventory_view
def transfer_new(request, actor):
    if request.method == 'POST':
        from_location = get_obj(Location, actor, request.POST.get('from_location'))
        to_location = get_obj(Location, actor, request.POST.get('to_location'))
        order = services.create_transfer_order(
            actor, from_location=from_location, to_location=to_location, lines=resolve_stock_lines(actor, request.POST.get('lines'), from_location),
            direct=bool(request.POST.get('direct')), priority=request.POST.get('priority', 'NORMAL'), reason=request.POST.get('reason', ''),
            return_reason=request.POST.get('return_reason', ''), source_type='RETURN' if request.POST.get('return_reason') else 'MANUAL',
            remarks=request.POST.get('remarks', ''))
        messages.success(request, f'Transfer {order.transfer_no} created.')
        return redirect('inventory_transfer_detail', order.pk)
    return page(request, 'transfer_new.html', sources=permitted(actor, 'create_transfer').exclude(location_type='TRANSIT'),
                destinations=Location.objects.filter(tenant=actor.tenant, active=True, blocked=False).exclude(location_type='TRANSIT'),
                return_reasons=TransferOrder.RETURN_REASONS, priorities=TransferOrder._meta.get_field('priority').choices)


def _quantities(post, prefix, lines):
    result = {}
    for line in lines:
        value = post.get(f'{prefix}_{line.pk}')
        if value not in (None, ''):
            result[line.pk] = D(value)
    return result


@inventory_view
def transfer_detail(request, actor, pk):
    order = get_obj(TransferOrder, actor, pk)
    if not permitted(actor).filter(pk__in=[order.from_location_id, order.to_location_id]).exists():
        raise Http404
    lines = list(order.lines.select_related('item', 'sku', 'jewellery_unit', 'from_bin', 'to_bin'))
    if request.method == 'POST':
        action, post = request.POST.get('action'), request.POST
        barcodes = [code for code, _ in parse_lines(post.get('ship_barcodes' if action == 'ship' else 'receive_barcodes'))]
        if action == 'submit':
            services.submit_transfer(actor, order)
        elif action == 'approve':
            services.approve_transfer(actor, order, _quantities(post, 'approve', lines))
        elif action == 'release':
            services.release_transfer(actor, order)
        elif action == 'ship':
            shipment = services.ship_transfer(actor, order, barcodes=barcodes or None, quantities=_quantities(post, 'ship', lines) or None)
            messages.success(request, f'Shipment {shipment.shipment_no} posted.')
        elif action == 'receive':
            damaged = [code for code, _ in parse_lines(post.get('damaged_barcodes'))]
            specs = []
            if not barcodes and not damaged:
                for line in lines:
                    qty, damaged_qty, excess = post.get(f'receive_{line.pk}'), post.get(f'damaged_{line.pk}'), post.get(f'excess_{line.pk}')
                    if any(v not in (None, '', '0') for v in (qty, damaged_qty, excess)):
                        specs.append({'line': line, 'quantity': D(qty), 'damaged_qty': D(damaged_qty), 'excess_qty': D(excess),
                                      'reason_code': post.get(f'reason_{line.pk}', '')})
            receipt = services.receive_transfer(actor, order, lines=specs or None, barcodes=barcodes or None, damaged_barcodes=damaged or None)
            messages.success(request, f'Receipt {receipt.receipt_no} posted.')
        elif action == 'cancel_remaining':
            services.cancel_remaining(actor, order, post.get('reason', ''))
        elif action == 'short_close':
            services.short_close_transfer(actor, order, post.get('reason_code'), post.get('remarks', ''))
        elif action == 'close':
            services.close_transfer(actor, order)
        elif action == 'cancel':
            services.cancel_transfer(actor, order, post.get('reason', ''))
        else:
            raise InventoryError('Unknown action.')
        return redirect('inventory_transfer_detail', order.pk)
    to_ship = sum((l.qty_to_ship for l in lines), ZERO)
    in_transit = sum((l.qty_in_transit for l in lines), ZERO)
    shippable = order.status in ('APPROVED', 'RELEASED', 'PARTIALLY_SHIPPED', 'PARTIALLY_RECEIVED')
    return page(request, 'transfer_detail.html', order=order, lines=lines,
                can_approve=order.status in ('DRAFT', 'PENDING_APPROVAL'), can_ship=shippable and to_ship > 0,
                can_receive=in_transit > 0 and not order.direct_transfer,
                can_exceptions=order.status not in ('CLOSED', 'CANCELLED', 'RECEIVED'),
                can_cancel=in_transit == 0 and not any(l.qty_shipped for l in lines) and order.status not in ('CLOSED', 'CANCELLED'),
                shipments=order.shipments.prefetch_related('lines__transfer_line').select_related('shipped_by'),
                receipts=order.receipts.prefetch_related('lines__transfer_line').select_related('received_by'),
                audit_log=InventoryAuditLog.objects.filter(tenant=actor.tenant, document_type='TRANSFER_ORDER', document_no=order.transfer_no).select_related('user'),
                reasons=TransferReceiptLine.REASONS[1:],
                total_value=sum((l.line_value for l in lines), ZERO), total_weight=sum((l.gross_weight for l in lines), ZERO),
                total_qty=sum((l.quantity for l in lines), ZERO), in_transit=in_transit, to_ship=to_ship)


@inventory_view
def transit(request, actor):
    rows = reports.transit_rows(actor.tenant, permitted(actor))
    if request.GET.get('export'):
        return export([[r['order'].transfer_no, r['order'].from_location.code, r['order'].to_location.code, r['line'].item.item_no,
                        r['shipment_date'], r['order'].expected_receipt_date, r['days'], r['qty'], r['weight'], r['value'],
                        'Overdue' if r['overdue'] else r['order'].status] for r in rows],
                      ['Transfer', 'From', 'To', 'Item', 'Shipment date', 'Expected receipt', 'Days in transit', 'Qty', 'Weight', 'Value', 'Status'],
                      'inventory-in-transit', request.GET['export'])
    buckets = {b: {'count': 0, 'value': ZERO} for b in ('0-1', '2-3', '4-7', '>7')}
    for row in rows:
        buckets[row['bucket']]['count'] += 1
        buckets[row['bucket']]['value'] += row['value']
    return page(request, 'transit.html', rows=rows, buckets=buckets, kpis=reports.transfer_kpis(actor.tenant, permitted(actor)))


@inventory_view
def transfer_register(request, actor):
    locations = permitted(actor)
    orders = TransferOrder.objects.filter(tenant=actor.tenant).filter(Q(from_location__in=locations) | Q(to_location__in=locations)) \
        .distinct().select_related('from_location', 'to_location').prefetch_related('lines')
    date_from, date_to = parse_date(request.GET.get('from') or ''), parse_date(request.GET.get('to') or '')
    if date_from:
        orders = orders.filter(transfer_date__gte=date_from)
    if date_to:
        orders = orders.filter(transfer_date__lte=date_to)
    rows = []
    for order in orders[:1000]:
        lines = list(order.lines.all())
        rows.append({'order': order, 'items': len(lines), 'qty': sum((l.quantity for l in lines), ZERO),
                     'weight': sum((l.gross_weight for l in lines), ZERO), 'value': sum((l.line_value for l in lines), ZERO),
                     'shipped': sum((l.qty_shipped for l in lines), ZERO), 'received': sum((l.qty_received for l in lines), ZERO),
                     'outstanding': sum((l.qty_outstanding for l in lines), ZERO)})
    if request.GET.get('export'):
        return export([[r['order'].transfer_no, r['order'].transfer_date, r['order'].from_location.code, r['order'].to_location.code,
                        r['items'], r['qty'], r['weight'], r['value'], r['shipped'], r['received'], r['outstanding'], r['order'].status] for r in rows],
                      ['Transfer', 'Date', 'From', 'To', 'Items', 'Quantity', 'Weight', 'Value', 'Shipped', 'Received', 'Outstanding', 'Status'],
                      'transfer-register', request.GET['export'])
    return page(request, 'transfer_register.html', rows=rows, date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------------------
# Journals
# ---------------------------------------------------------------------------

@inventory_view
def adjustment_list(request, actor):
    if request.method == 'POST':
        location = get_obj(Location, actor, request.POST.get('location'))
        kind = request.POST.get('adjustment_type', 'POSITIVE')
        lines = []
        for spec in resolve_stock_lines(actor, request.POST.get('lines'), location):
            spec.update(adjustment_type=kind, unit_cost=request.POST.get('unit_cost') or None,
                        to_status=request.POST.get('to_status', '') if kind == 'STATUS' else '',
                        from_status=request.POST.get('from_status', '') if kind == 'STATUS' else '')
            if kind == 'WEIGHT':
                spec.update(gross_weight=request.POST.get('gross_weight'), net_weight=request.POST.get('net_weight') or request.POST.get('gross_weight'))
            lines.append(spec)
        adjustment = services.create_adjustment(actor, location=location, reason_code=request.POST.get('reason_code'), lines=lines,
                                                reference=request.POST.get('reference', ''), remarks=request.POST.get('remarks', ''))
        messages.success(request, f'Adjustment {adjustment.adjustment_no} created as draft.')
        return redirect('inventory_adjustment_detail', adjustment.pk)
    adjustments = InventoryAdjustment.objects.filter(tenant=actor.tenant, location__in=permitted(actor)).select_related('location', 'created_by') \
        .annotate(line_count=Count('lines'))
    return page(request, 'adjustment_list.html', adjustments=adjustments[:300], my_locations=permitted(actor, 'adjust'),
                reasons=InventoryAdjustment.REASONS, statuses=['QC', 'DAMAGED', 'REPAIR', 'BLOCKED', 'AVAILABLE'])


@inventory_view
def adjustment_detail(request, actor, pk):
    adjustment = get_obj(InventoryAdjustment, actor, pk)
    require_location(actor.tenant, actor.user, adjustment.location, 'view')
    if request.method == 'POST':
        action = request.POST.get('action')
        handler = {'submit': services.submit_adjustment, 'approve': services.approve_adjustment, 'post': services.post_adjustment}.get(action)
        if action == 'cancel':
            require_location(actor.tenant, actor.user, adjustment.location, 'adjust')
            if adjustment.status == 'POSTED':
                raise InventoryError('Posted adjustments cannot be cancelled - post a reversing adjustment.')
            adjustment.status = 'CANCELLED'
            adjustment.save(update_fields=['status', 'updated_at'])
        elif handler:
            handler(actor, adjustment)
        messages.success(request, f'Adjustment {action}ed.' if action != 'post' else 'Adjustment posted.')
        return redirect('inventory_adjustment_detail', adjustment.pk)
    return page(request, 'adjustment_detail.html', adjustment=adjustment,
                lines=adjustment.lines.select_related('item', 'sku', 'bin', 'jewellery_unit'),
                audit_log=InventoryAuditLog.objects.filter(tenant=actor.tenant, document_type='ADJUSTMENT', document_no=adjustment.adjustment_no).select_related('user'))


@inventory_view
def reclass(request, actor):
    if request.method == 'POST':
        from_location = get_obj(Location, actor, request.POST.get('from_location')) if request.POST.get('from_location') else None
        to_location = get_obj(Location, actor, request.POST.get('to_location')) if request.POST.get('to_location') else None
        to_bin = get_obj(Bin, actor, request.POST.get('to_bin')) if request.POST.get('to_bin') else None
        from_bin = get_obj(Bin, actor, request.POST.get('from_bin')) if request.POST.get('from_bin') else None
        lines = []
        for spec in resolve_stock_lines(actor, request.POST.get('lines'), from_location):
            line = {'unit': spec.get('unit'), 'item': spec.get('item'), 'variant': spec.get('variant'), 'quantity': spec.get('quantity'),
                    'from_location': from_location, 'from_bin': from_bin, 'to_status': request.POST.get('to_status') or None}
            if to_location:
                line['to_location'] = to_location
            if to_bin or request.POST.get('clear_bin'):
                line['to_bin'] = to_bin
            lines.append(line)
        result = services.post_reclassification(actor, lines=lines, reason=request.POST.get('reason', ''))
        messages.success(request, f'Reclassification {result.reclass_no} posted.')
        return redirect('inventory_reclass')
    from .models import Reclassification
    return page(request, 'reclass.html', locations=permitted(actor), bins=Bin.objects.filter(tenant=actor.tenant, location__in=permitted(actor), active=True).select_related('location'),
                recent=Reclassification.objects.filter(tenant=actor.tenant).prefetch_related('lines__item', 'lines__jewellery_unit', 'lines__from_location', 'lines__to_location', 'lines__from_bin', 'lines__to_bin')[:20],
                statuses=['AVAILABLE', 'QC', 'DAMAGED', 'REPAIR', 'BLOCKED'])


@inventory_view
def count_list(request, actor):
    if request.method == 'POST':
        count = services.create_count(actor, location=get_obj(Location, actor, request.POST.get('location')), blind=bool(request.POST.get('blind')),
                                      remarks=request.POST.get('remarks', ''))
        services.snapshot_count(actor, count)
        messages.success(request, f'Count {count.count_no} created and snapshot taken.')
        return redirect('inventory_count_detail', count.pk)
    counts = PhysicalCount.objects.filter(tenant=actor.tenant, location__in=permitted(actor)).select_related('location').annotate(line_count=Count('lines'))
    return page(request, 'count_list.html', counts=counts[:200], my_locations=permitted(actor, 'count'))


@inventory_view
def count_detail(request, actor, pk):
    count = get_obj(PhysicalCount, actor, pk)
    require_location(actor.tenant, actor.user, count.location, 'view')
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'scan':
            for code, _ in parse_lines(request.POST.get('codes')):
                services.scan_count(actor, count, code, request.POST.get('gross_weight'))
        elif action == 'record':
            for line in count.lines.all():
                value = request.POST.get(f'qty_{line.pk}')
                if value not in (None, ''):
                    services.record_count(actor, count, line=line, counted_qty=value, counted_gross_weight=request.POST.get(f'weight_{line.pk}'))
        elif action == 'submit':
            adjustment = services.submit_count(actor, count)
            if adjustment:
                messages.success(request, f'Variance adjustment {adjustment.adjustment_no} created - it must be approved before posting.')
                return redirect('inventory_adjustment_detail', adjustment.pk)
            messages.success(request, 'No variances - count closed.')
        return redirect('inventory_count_detail', count.pk)
    lines = count.lines.select_related('item', 'sku', 'bin', 'jewellery_unit').order_by('bin__code', 'item__item_no', 'id')
    totals = lines.aggregate(system=Sum('system_qty'), counted=Sum('counted_qty'), system_weight=Sum('system_gross_weight'),
                             counted_weight=Sum('counted_gross_weight'))
    hide_system = count.blind_count and count.status == 'COUNTING'
    return page(request, 'count_detail.html', count=count, lines=lines, totals=totals, hide_system=hide_system,
                adjustment=count.adjustments.first())


@inventory_view
def replenishment(request, actor):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'generate':
            lines = services.generate_replenishment(actor, locations=permitted(actor, 'create_transfer') if not services.is_tenant_admin(actor.tenant, actor.user) else None)
            messages.success(request, f'{len(lines)} suggestion(s) calculated.')
        else:
            chosen = ReplenishmentLine.objects.filter(tenant=actor.tenant, pk__in=request.POST.getlist('line'))
            for line in chosen:
                if request.POST.get(f'qty_{line.pk}'):
                    line.suggested_qty = D(request.POST[f'qty_{line.pk}'])
                if request.POST.get(f'source_{line.pk}'):
                    line.source_location = get_obj(Location, actor, request.POST[f'source_{line.pk}'])
                line.save()
            if action == 'ignore':
                chosen.update(status='IGNORED')
            elif action == 'approve':
                chosen.filter(status='SUGGESTED').update(status='APPROVED')
            elif action == 'create':
                orders = services.create_transfers_from_worksheet(actor, list(chosen))
                messages.success(request, f'Created {", ".join(o.transfer_no for o in orders) or "no"} transfer order(s).')
        return redirect('inventory_replenishment')
    lines = ReplenishmentLine.objects.filter(tenant=actor.tenant, location__in=permitted(actor), status__in=('SUGGESTED', 'APPROVED')) \
        .select_related('sku', 'sku__item', 'location', 'source_location')
    return page(request, 'replenishment.html', lines=lines,
                sources=Location.objects.filter(tenant=actor.tenant, active=True).exclude(location_type='TRANSIT'))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@inventory_view
def valuation(request, actor):
    locations, selected = selected_locations(request, actor)
    rows = reports.valuation_by_location(actor.tenant, locations)
    totals = {k: sum((r[k] for r in rows), ZERO) for k in ('qty', 'gross_weight', 'net_weight', 'cost_value', 'metal_value', 'current_value', 'retail_value')}
    if request.GET.get('export'):
        return export([[r['location'].code, r['qty'], r['gross_weight'], r['net_weight'], r['cost_value'], r['metal_value'],
                        r['current_value'], r['retail_value']] for r in rows],
                      ['Location', 'Quantity', 'Gross weight', 'Net metal weight', 'Historical cost', 'Current metal value',
                       'Current value', 'Retail value'], 'inventory-valuation', request.GET['export'])
    return page(request, 'valuation.html', rows=rows, totals=totals, selected=selected, locations=permitted(actor),
                unrated=any(r['unrated_weight'] for r in rows))


@inventory_view
def stock_report(request, actor):
    locations, selected = selected_locations(request, actor)
    rates = reports.RateBook()
    balances = InventoryBalance.objects.filter(tenant=actor.tenant, location__in=locations).exclude(on_hand_qty=0, in_transit_qty=0) \
        .values('location__code', 'location', 'item__item_no', 'item__description', 'item__metal', 'item__purity', 'item__category', 'sku__code') \
        .annotate(qty=Sum('on_hand_qty'), transit=Sum('in_transit_qty'), gross=Sum('gross_weight'), net=Sum('net_weight'),
                  reserved=Sum('reserved_qty'), available=Sum('available_qty'), value=Sum('cost_value')).order_by('location__code', 'item__item_no')
    for key, lookup in (('metal', 'item__metal'), ('purity', 'item__purity'), ('category', 'item__category__iexact')):
        if request.GET.get(key):
            balances = balances.filter(**{lookup: request.GET[key]})
    location_by_id = {l.pk: l for l in Location.objects.filter(tenant=actor.tenant)}
    rows = []
    for b in balances:
        rate = rates.rate(b['item__metal'], b['item__purity'], location_by_id.get(b['location']))
        rows.append({**b, 'total_qty': b['qty'] + b['transit'],
                     'current': (b['net'] * rate).quantize(Decimal('0.01')) if rate is not None else None})
    if request.GET.get('export'):
        return export([[r['location__code'], r['item__item_no'], r['sku__code'] or '', r['total_qty'], r['gross'], r['net'],
                        r['reserved'], r['available'], r['value'], r['current'] if r['current'] is not None else ''] for r in rows],
                      ['Location', 'Item', 'SKU', 'Qty', 'Gross weight', 'Net weight', 'Reserved', 'Available', 'Inventory value', 'Current value'],
                      'location-stock', request.GET['export'])
    metal_summary = {}
    for r in rows:
        key = (r['location__code'], r['item__metal'], r['item__purity'])
        agg = metal_summary.setdefault(key, {'qty': ZERO, 'gross': ZERO, 'net': ZERO})
        agg['qty'] += r['total_qty']
        agg['gross'] += r['gross']
        agg['net'] += r['net']
    return page(request, 'stock_report.html', rows=rows, selected=selected, locations=permitted(actor),
                metal_summary=sorted(metal_summary.items()))


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

@inventory_view
def import_center(request, actor):
    if request.method == 'POST':
        if not request.FILES.get('file'):
            raise InventoryError('Choose an .xlsx file to upload.')
        batch = imports.upload(actor, request.POST.get('import_type'), request.FILES['file'])
        messages.info(request, f'Uploaded {len(batch.rows)} row(s): {batch.get_status_display()}.')
        return redirect('inventory_import_detail', batch.pk)
    return page(request, 'import_center.html', types=[(code, label, imports.TEMPLATES[code]) for code, label in ImportBatch.TYPES],
                batches=ImportBatch.objects.filter(tenant=actor.tenant).select_related('created_by')[:50])


@inventory_view
def import_detail(request, actor, pk):
    batch = get_obj(ImportBatch, actor, pk)
    if request.method == 'POST':
        if request.POST.get('action') == 'revalidate':
            imports.validate(actor, batch)
        else:
            imports.run_import(actor, batch)
            messages.success(request, 'Import completed.')
        return redirect('inventory_import_detail', batch.pk)
    if request.GET.get('errors'):
        response = HttpResponse(imports.error_workbook(batch), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="import-{batch.pk}-errors.xlsx"'
        return response
    errors_by_row = {}
    for error in batch.errors:
        errors_by_row.setdefault(error['row'], []).append(error)
    columns = imports.TEMPLATES[batch.import_type]
    preview = [(n, [row.get(c, '') for c in columns], errors_by_row.get(n, [])) for n, row in enumerate(batch.rows[:500], 2)]
    return page(request, 'import_detail.html', batch=batch, columns=columns, preview=preview)


@inventory_view
def import_template(request, actor, import_type):
    if import_type not in imports.TEMPLATES:
        raise Http404
    response = HttpResponse(imports.template_workbook(import_type), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{import_type}-template.xlsx"'
    return response
