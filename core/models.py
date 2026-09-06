import uuid

from django.db import models


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
    """Abstract base for everything that belongs to a tenant."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")

    class Meta:
        abstract = True


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
