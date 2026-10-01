"""Screen tests for Jewellery Savings: main-menu entry, scheme approval, enrolling a new member (new or existing customer),
collection, customer ledger and the enrolment / pending / collection reports.

Views are called through RequestFactory - see manufacturing/test_views.py for why.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from erp.models import Customer
from inventory.tenancy import create_tenant_for_user
from savings.models import JewellerySavingsScheme, SchemeEnrollment, SchemeLedgerEntry

PAGES = ['savings_dashboard', 'savings_members', 'savings_member_new', 'savings_report_enrollment', 'savings_report_pending',
         'savings_report_collection', 'savings_schemes', 'savings_setup']


class SavingsScreenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('sav-owner', password='x')
        self.tenant = create_tenant_for_user(self.user, 'Goldio Test')
        self.factory = RequestFactory()

    def call(self, url, data=None, user=None):
        request = self.factory.post(url, data) if data is not None else self.factory.get(url)
        request.user = user or self.user
        request.session = SessionStore()
        request._messages = FallbackStorage(request)
        match = resolve(request.path)
        return match.func(request, *match.args, **match.kwargs), request

    def notes(self, request):
        return ' '.join(str(m) for m in request._messages)

    def active_scheme(self):
        """Single-owner shop: allow self-approval, create the 11+1 template, submit and approve it."""
        self.call(reverse('savings_setup'), {'display_name': 'Gold Savings Plan', 'reminder_days': '5', 'allow_self_approval': 'on',
                                             'require_enrollment_approval': 'on', 'allow_multiple_active_schemes': 'on',
                                             'max_active_schemes_per_customer': '3', 'max_monthly_contribution': '0'})
        self.call(reverse('savings_dashboard'), {'action': 'create_template'})
        scheme = JewellerySavingsScheme.objects.get(tenant=self.tenant)
        version = scheme.versions.get()
        url = reverse('savings_scheme_detail', args=[scheme.pk])
        for action in ('submit', 'approve'):
            _, request = self.call(url, {'action': action, 'version': version.pk})
            self.assertNotIn('cannot', self.notes(request))
        scheme.refresh_from_db()
        self.assertEqual(scheme.status, 'ACTIVE')
        return scheme

    def enrol_new(self, scheme, **extra):
        data = {'customer_mode': 'new', 'new_name': 'Rahul Sharma', 'new_phone': '9876543210', 'scheme': scheme.pk,
                'installment_amount': '10000', 'start_date': timezone.localdate().isoformat(), 'acceptance_method': 'PHYSICAL',
                'signature_reference': 'FORM-001', 'nominee_name': 'Priya Sharma', 'nominee_relationship': 'Spouse', **extra}
        return self.call(reverse('savings_member_new'), data)

    def test_pages_render_with_main_menu_link(self):
        for name in PAGES:
            with self.subTest(page=name):
                response, _ = self.call(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'href="{reverse("savings_dashboard")}" class="nav-item">Jewellery Savings</a>')

    def test_enrol_new_customer_collect_and_see_ledger_and_reports(self):
        scheme = self.active_scheme()
        response, request = self.enrol_new(scheme)
        self.assertEqual(response.status_code, 302, self.notes(request))
        customer = Customer.objects.get(phone='9876543210')
        self.assertTrue(customer.customer_no)
        e = SchemeEnrollment.objects.get(customer=customer)
        self.assertEqual((e.account_no, e.status, e.installments.count()), ('GSP-000001', 'PENDING_APPROVAL', 11))
        self.assertEqual(e.nominees.get().name, 'Priya Sharma')

        detail = reverse('savings_member_detail', args=[e.pk])
        self.call(detail, {'action': 'approve'})
        _, request = self.call(detail, {'action': 'collect', 'amount': '10000', 'method_type': 'upi', 'reference_no': 'UPI-1'})
        self.assertIn('collected', self.notes(request))
        e.refresh_from_db()
        self.assertEqual(e.contribution_paid, Decimal('10000'))
        self.assertTrue(SchemeLedgerEntry.objects.filter(enrollment=e, entry_type='PAYMENT').exists())

        response, _ = self.call(detail)
        self.assertContains(response, 'Customer scheme ledger')
        self.assertContains(response, '1 / 11')
        response, _ = self.call(reverse('savings_customer_detail', args=[customer.pk]))
        self.assertContains(response, 'GSP-000001')

        response, _ = self.call(reverse('savings_report_enrollment'))
        self.assertContains(response, 'Distinct customers')
        # Two months on, installment 2 (and 3 if due) are pending for Rahul.
        as_of = (timezone.localdate() + timedelta(days=62)).isoformat()
        response, _ = self.call(reverse('savings_report_pending') + f'?as_of={as_of}')
        self.assertContains(response, 'Rahul Sharma')
        self.assertContains(response, 'GSP-000001')
        response, _ = self.call(reverse('savings_report_collection') + '?group_by=method')
        self.assertContains(response, 'upi')

    def test_existing_customer_duplicate_needs_confirmation(self):
        scheme = self.active_scheme()
        self.enrol_new(scheme)
        customer = Customer.objects.get(phone='9876543210')
        data = {'customer_mode': 'existing', 'customer': customer.pk, 'scheme': scheme.pk, 'acceptance_method': 'OTP'}
        response, _ = self.call(reverse('savings_member_new'), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Possible duplicate')
        self.assertEqual(SchemeEnrollment.objects.filter(customer=customer).count(), 1)
        response, request = self.call(reverse('savings_member_new'), {**data, 'confirm': 'on'})
        self.assertEqual(response.status_code, 302, self.notes(request))
        self.assertEqual(SchemeEnrollment.objects.filter(customer=customer).count(), 2)

    def test_failed_enrolment_does_not_leave_a_customer_behind(self):
        scheme = self.active_scheme()
        response, request = self.enrol_new(scheme, installment_amount='750')   # below the ₹1,000 minimum
        self.assertEqual(response.status_code, 200)
        self.assertIn('Minimum monthly installment', self.notes(request))
        self.assertFalse(Customer.objects.filter(phone='9876543210').exists())

    def test_login_required(self):
        response, _ = self.call(reverse('savings_dashboard'), user=AnonymousUser())
        self.assertEqual(response.status_code, 302)
