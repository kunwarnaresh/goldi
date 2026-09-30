from datetime import date

from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.test import SimpleTestCase, RequestFactory, TestCase

from inventory.models import InventoryBalance, Item, Location, Tenant, TenantMembership, UnitOfMeasure
from .semantic import DATASETS, dataset_catalog, execute_query
from .views import _date_filters, _period_range


class SemanticCatalogTests(SimpleTestCase):
    def test_catalog_exposes_reusable_dimensions_and_measures(self):
        catalog = {item['key']: item for item in dataset_catalog()}
        self.assertEqual(set(catalog), set(DATASETS))
        self.assertIn('store', {item['key'] for item in catalog['sales']['dimensions']})
        self.assertIn('metal_weight', {item['key'] for item in catalog['sales']['measures']})
        self.assertIn('cost_value', {item['key'] for item in catalog['inventory']['measures']})

    def test_unregistered_dataset_and_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            execute_query(dataset='raw_sql', tenant=None, user=None)
        with self.assertRaises(ValidationError):
            execute_query(dataset='sales', tenant=None, user=None, dimensions=['invoice__customer__name'])

    def test_query_complexity_is_bounded(self):
        with self.assertRaises(ValidationError):
            execute_query(dataset='sales', tenant=None, user=None,
                          dimensions=['invoice', 'date', 'store', 'customer'])
        with self.assertRaises(ValidationError):
            execute_query(dataset='sales', tenant=None, user=None,
                          measures=list(DATASETS['sales'].measures) + ['unknown'])


class DateRangeTests(SimpleTestCase):
    def test_relative_ranges_use_local_calendar_dates(self):
        self.assertEqual(_period_range('today', date(2026, 9, 30)), ('2026-09-30', '2026-09-30'))
        self.assertEqual(_period_range('previous_month', date(2026, 3, 4)), ('2026-02-01', '2026-02-28'))
        self.assertEqual(_period_range('fytd', date(2026, 2, 3)), ('2025-04-01', '2026-02-03'))
        self.assertEqual(_period_range('previous_fy', date(2026, 9, 30)), ('2025-04-01', '2026-03-31'))

    def test_custom_range_rejects_reversed_dates(self):
        request = RequestFactory().get('/analytics/reports/', {'date_from': '2026-09-30', 'date_to': '2026-09-01'})
        with self.assertRaises(ValidationError):
            _date_filters(request)


class InventorySemanticQueryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='analytics-owner', password='test-pass')
        self.tenant = Tenant.objects.create(code='analytics-one', name='Analytics One')
        TenantMembership.objects.create(tenant=self.tenant, user=self.user, role='owner', all_locations=True)
        self.uom = UnitOfMeasure.objects.create(tenant=self.tenant, code='PCS', name='Pieces')
        self.location = Location.objects.create(tenant=self.tenant, code='SHOP', name='Main shop')
        self.item = Item.objects.create(tenant=self.tenant, item_no='RING-1', description='Gold ring',
                                        base_uom=self.uom, category='Rings', metal='GOLD', purity='22K')
        InventoryBalance.objects.create(tenant=self.tenant, bucket_key='one', item=self.item, location=self.location,
                                        on_hand_qty=5, available_qty=4, reserved_qty=1, net_weight='12.500',
                                        gross_weight='13.000', cost_value='2500.00')
        other_tenant = Tenant.objects.create(code='analytics-two', name='Analytics Two')
        other_uom = UnitOfMeasure.objects.create(tenant=other_tenant, code='PCS', name='Pieces')
        other_location = Location.objects.create(tenant=other_tenant, code='SHOP', name='Other shop')
        other_item = Item.objects.create(tenant=other_tenant, item_no='RING-1', description='Other ring', base_uom=other_uom)
        InventoryBalance.objects.create(tenant=other_tenant, bucket_key='other', item=other_item, location=other_location,
                                        on_hand_qty=100, available_qty=100, cost_value='99999.00')

    def test_inventory_aggregation_is_tenant_scoped(self):
        result = execute_query(dataset='inventory', tenant=self.tenant, user=self.user,
                               dimensions=['location', 'metal'], measures=['quantity', 'cost_value'])
        self.assertEqual(result['rows'], [{'location': 'Main shop', 'metal': 'GOLD',
                                           'quantity': 5, 'cost_value': 2500}])
        self.assertEqual(result['totals'], {'quantity': 5, 'cost_value': 2500})
