"""Smoke tests for the manufacturing screens and their main-menu entry.

Views are called through RequestFactory rather than the test client: the client's template-context copy
breaks on Python 3.14 with Django 5.1.1, while the pages themselves render fine.
"""
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse

from django.core.exceptions import PermissionDenied

from inventory.models import Item, Location, TenantMembership, UnitOfMeasure
from inventory.tenancy import create_tenant_for_user
from manufacturing.models import ProductionBOM, ProductionOrder

PAGES = ['manufacturing_dashboard', 'manufacturing_capacity', 'manufacturing_orders', 'manufacturing_order_new', 'manufacturing_boms',
         'manufacturing_routings', 'manufacturing_work_centers', 'manufacturing_subcontracts', 'manufacturing_planning']


class ManufacturingScreenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('mfg-owner', password='x')
        self.tenant = create_tenant_for_user(self.user, 'Goldio Test')
        self.factory = RequestFactory()

    def call(self, url, data=None, user=None):
        request = self.factory.post(url, data) if data is not None else self.factory.get(url)
        request.user = user or self.user
        request.session = SessionStore()
        request._messages = FallbackStorage(request)
        match = resolve(request.path)
        return match.func(request, *match.args, **match.kwargs), request

    def test_pages_render_with_main_menu_link(self):
        for name in PAGES:
            with self.subTest(page=name):
                response, _ = self.call(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'href="{reverse("manufacturing_dashboard")}" class="nav-item"')

    def test_order_and_bom_detail_render(self):
        tenant = self.tenant
        location = Location.objects.create(tenant=tenant, code='FACT', name='Karkhana', location_type='MANUFACTURING', allow_sales=False)
        item = Item.objects.create(tenant=tenant, item_no='RING-1', description='Gold ring', purity='22K',
                                   base_uom=UnitOfMeasure.objects.filter(tenant=tenant).first())
        bom = ProductionBOM.objects.create(tenant=tenant, bom_no='BOM-1', bom_name='Ring BOM', item=item)
        order = ProductionOrder.objects.create(tenant=tenant, order_no='MO-1', location=location, item=item, bom=bom, planned_qty=5)
        for url in (reverse('manufacturing_order_detail', args=[order.pk]), reverse('manufacturing_bom_detail', args=[bom.pk])):
            with self.subTest(url=url):
                response, _ = self.call(url)
                self.assertEqual(response.status_code, 200)

    def test_rejected_action_becomes_message(self):
        response, request = self.call(reverse('manufacturing_planning'), {'action': 'run', 'horizon_days': '30'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('default production location', ' '.join(str(m) for m in request._messages))

    def test_account_without_workspace_is_provisioned(self):
        legacy = User.objects.create_user('legacy@goldio.test', password='x')
        response, _ = self.call(reverse('manufacturing_dashboard'), user=legacy)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(TenantMembership.objects.filter(user=legacy, role='owner', active=True).exists())

    def test_deactivated_member_is_not_reprovisioned(self):
        TenantMembership.objects.filter(user=self.user).update(active=False)
        with self.assertRaises(PermissionDenied):
            self.call(reverse('manufacturing_dashboard'))
        self.assertEqual(TenantMembership.objects.filter(user=self.user).count(), 1)

    def test_login_required(self):
        response, _ = self.call(reverse('manufacturing_dashboard'), user=AnonymousUser())
        self.assertEqual(response.status_code, 302)
