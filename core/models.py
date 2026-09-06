import uuid

from django.db import models

from core.tenancy import TenantScopedManager


class Tenant(models.Model):
    """One customer. Rippling serves thousands of companies off one graph; so do we.

    Every other table in Cascade carries a tenant FK. Isolation is a property of the
    schema, not of remembering to filter.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.slug


class TenantScopedModel(models.Model):
    """Abstract base for everything that belongs to a tenant.

    ``objects`` is scoped and raises without a tenant context; ``all_tenants``
    is the deliberate escape hatch. Concrete subclasses must set
    ``base_manager_name = "all_tenants"`` in their Meta, or Django will use the
    scoped manager for foreign-key traversal and a worker will be unable to
    load the run belonging to the task it is holding.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")

    objects = TenantScopedManager()
    all_tenants = models.Manager()

    class Meta:
        abstract = True
        base_manager_name = "all_tenants"
        default_manager_name = "objects"


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
