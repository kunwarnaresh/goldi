"""Configuration masters without their own screens (setup, roles, approval matrix, agreements, work centres).
Transactional documents are deliberately not editable here - they change only through the job work services."""
from django.contrib import admin

from .models import ApprovalRule, JobWorkerAgreement, JobWorkerWorkCenter, JobWorkRole, JobWorkSetup


@admin.register(JobWorkSetup)
class JobWorkSetupAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'company', 'enabled', 'require_order_approval', 'require_qc_on_receipt', 'gl_posting_enabled')


@admin.register(JobWorkRole)
class JobWorkRoleAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'user', 'role', 'active')
    list_filter = ('role', 'active')


@admin.register(ApprovalRule)
class ApprovalRuleAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'metric', 'min_value', 'max_value', 'required_role', 'active')
    list_filter = ('metric',)


@admin.register(JobWorkerAgreement)
class JobWorkerAgreementAdmin(admin.ModelAdmin):
    list_display = ('agreement_no', 'job_worker', 'effective_from', 'expires_on', 'expected_loss_percent', 'max_loss_percent', 'status')


@admin.register(JobWorkerWorkCenter)
class JobWorkerWorkCenterAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'job_worker', 'operation_code', 'costing_method', 'unit_cost', 'minimum_charge', 'active')
