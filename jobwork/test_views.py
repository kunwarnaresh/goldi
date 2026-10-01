"""Smoke tests for the job work screens and API.

Views are called through RequestFactory rather than the test client: the client's template-context copy breaks on
Python 3.14 with Django 5.1.1, while the pages themselves render fine (same approach as manufacturing/test_views.py)."""
import json

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory
from django.urls import resolve, reverse

from jobwork.models import JobWorkOrder
from jobwork.tests import JobWorkFixture

PAGES = ['jobwork_dashboard', 'jobwork_board', 'jobwork_material', 'jobwork_exceptions', 'jobwork_orders', 'jobwork_order_new',
         'jobwork_dispatches', 'jobwork_invoices', 'jobwork_workers', 'jobwork_prices', 'jobwork_tax_rates', 'jobwork_rules',
         'jobwork_worksheet', 'jobwork_register', 'jobwork_compliance', 'jobwork_itc04', 'jobwork_trace']


class JobWorkScreenTests(JobWorkFixture):
    def call(self, url, data=None, user=None, json_body=None, headers=None):
        factory = RequestFactory()
        if json_body is not None:
            request = factory.post(url, json.dumps(json_body), content_type='application/json', headers=headers or {})
        else:
            request = factory.post(url, data) if data is not None else factory.get(url)
        request.user = user or self.owner
        request.session = SessionStore()
        request._messages = FallbackStorage(request)
        request._dont_enforce_csrf_checks = True
        match = resolve(request.path)
        response = match.func(request, *match.args, **match.kwargs)
        if hasattr(response, 'render'):
            response.render()
        return response

    def test_pages_render(self):
        for name in PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.call(reverse(name)).status_code, 200)

    def test_order_screens_drive_the_lifecycle(self):
        order = self.ring_order()
        svc_url = reverse('jobwork_order_detail', args=[order.pk])
        self.assertEqual(self.call(svc_url).status_code, 200)
        self.assertEqual(self.call(svc_url, {'action': 'reserve'}, user=self.operator.user).status_code, 302)
        self.assertEqual(self.call(svc_url, {'action': 'dispatch', 'vehicle_no': 'MH01AB1234'}, user=self.operator.user).status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'IN_TRANSIT')
        dispatch = order.dispatches.get()
        self.assertEqual(self.call(reverse('jobwork_challan', args=[dispatch.challan.pk])).status_code, 200)
        self.call(svc_url, {'action': 'eway', 'eway': dispatch.eway_bills.get().pk, 'ewb_no': '331000000001'}, user=self.operator.user)
        self.call(svc_url, {'action': 'deliver', 'dispatch': dispatch.pk}, user=self.operator.user)
        order.refresh_from_db()
        self.assertEqual(order.status, 'RECEIVED_BY_JOB_WORKER')
        self.assertEqual(self.call(svc_url).status_code, 200)
        self.assertEqual(self.call(reverse('jobwork_worker_detail', args=[self.jw_a.pk])).status_code, 200)
        self.assertEqual(self.call(reverse('jobwork_trace') + '?code=GOLD-22K').status_code, 200)
        # refused actions come back as messages, not errors
        response = self.call(svc_url, {'action': 'complete'}, user=self.operator.user)
        self.assertEqual(response.status_code, 302)

    def test_api_orders_with_idempotency_and_tenant_isolation(self):
        body = {'job_worker': self.jw_a.pk, 'source_location': self.wh.pk, 'operation_code': 'CASTING', 'submit': True,
                'lines': [{'type': 'INPUT', 'item_no': 'GOLD-22K', 'quantity': '20'}, {'type': 'OUTPUT', 'item_no': 'RING-22K', 'quantity': '2'}]}
        first = self.call(reverse('api_job_work_orders'), json_body=body, user=self.operator.user, headers={'Idempotency-Key': 'k-1'})
        self.assertEqual(first.status_code, 201, first.data)
        replay = self.call(reverse('api_job_work_orders'), json_body=body, user=self.operator.user, headers={'Idempotency-Key': 'k-1'})
        self.assertEqual((replay.status_code, replay['Idempotent-Replay']), (201, 'true'))
        self.assertEqual(JobWorkOrder.objects.filter(tenant=self.tenant).count(), 1)
        order_id = first.data['id']
        self.assertEqual(first.data['status'], 'PENDING_APPROVAL')
        approve = self.call(reverse('api_job_work_order_action', args=[order_id, 'approve']), json_body={}, user=self.manager.user)
        self.assertEqual(approve.status_code, 200, approve.data)
        denied = self.call(reverse('api_job_work_order_action', args=[order_id, 'approve']), json_body={}, user=self.operator.user)
        self.assertEqual(denied.status_code, 403)
        detail = self.call(reverse('api_job_work_order_detail', args=[order_id]), user=self.operator.user)
        self.assertEqual(detail.data['lines'][0]['item'], 'GOLD-22K')
        intruder = User.objects.create_user('api-intruder', password='x')
        from inventory.tenancy import create_tenant_for_user
        create_tenant_for_user(intruder, 'Other')
        self.assertEqual(self.call(reverse('api_job_work_order_detail', args=[order_id]), user=intruder).status_code, 404)

    def test_members_without_a_job_work_role_are_refused(self):
        from django.core.exceptions import PermissionDenied
        from inventory.models import TenantMembership
        outsider = User.objects.create_user('shop-staff', password='x')
        TenantMembership.objects.create(tenant=self.tenant, user=outsider, role='member', all_locations=True)
        with self.assertRaises(PermissionDenied):
            self.call(reverse('jobwork_dashboard'), user=outsider)
        self.assertEqual(self.call(reverse('api_job_work_orders'), user=outsider).status_code, 403)
