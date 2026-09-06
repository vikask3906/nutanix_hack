"""Admin registrations.

Every ModelAdmin here uses ``all_tenants``: an operator opening the admin is
explicitly looking across tenants, and the scoped default manager would refuse
to run without a request-level tenant context.
"""

from django.contrib import admin


class AllTenantsAdmin(admin.ModelAdmin):
    """Base for scoped models - deliberately unscoped in the admin."""

    def get_queryset(self, request):
        return self.model.all_tenants.get_queryset()


from .models import Entity, EntityChange, EntityEdge, Trigger


@admin.register(Entity)
class EntityAdmin(AllTenantsAdmin):
    list_display = ("type", "external_id", "tenant", "version", "updated_at")
    list_filter = ("type", "tenant")
    search_fields = ("external_id", "type")
    readonly_fields = ("version", "created_at", "updated_at")


@admin.register(EntityEdge)
class EntityEdgeAdmin(AllTenantsAdmin):
    list_display = ("src", "rel", "dst", "tenant")
    list_filter = ("rel", "tenant")


@admin.register(EntityChange)
class EntityChangeAdmin(AllTenantsAdmin):
    list_display = ("id", "entity", "entity_type", "changed_keys", "created_at", "processed_at")
    list_filter = ("entity_type", "tenant", "processed_at")
    readonly_fields = ("entity", "entity_type", "before", "after", "changed_keys", "created_at")


@admin.register(Trigger)
class TriggerAdmin(AllTenantsAdmin):
    list_display = ("name", "entity_type", "definition", "enabled", "tenant")
    list_filter = ("entity_type", "enabled", "tenant")
    search_fields = ("name",)
