from django.conf import settings
from django.db import models


class SavedAnalysisView(models.Model):
    """A tenant-scoped, versioned configuration over a registered semantic dataset."""
    tenant = models.ForeignKey('inventory.Tenant', on_delete=models.CASCADE, related_name='analysis_views')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='analysis_views')
    name = models.CharField(max_length=120)
    dataset = models.CharField(max_length=40)
    configuration = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    shared = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('name', 'id')
        constraints = [models.UniqueConstraint(fields=('tenant', 'owner', 'name'), name='analytics_unique_saved_view')]
        indexes = [models.Index(fields=('tenant', 'dataset', 'shared'))]

    def __str__(self):
        return self.name


class SavedFilterGroup(models.Model):
    tenant = models.ForeignKey('inventory.Tenant', on_delete=models.CASCADE, related_name='analysis_filter_groups')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='analysis_filter_groups')
    name = models.CharField(max_length=120)
    dataset = models.CharField(max_length=40)
    conditions = models.JSONField(default=list)
    shared = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('name',)
        constraints = [models.UniqueConstraint(fields=('tenant', 'owner', 'name'), name='analytics_unique_filter_group')]
