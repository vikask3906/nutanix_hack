from django.contrib import admin

from .models import Entity, EntityChange, EntityEdge, Trigger


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ("type", "external_id", "tenant", "version", "updated_at")
    list_filter = ("type", "tenant")
    search_fields = ("external_id", "type")
    readonly_fields = ("version", "created_at", "updated_at")


@admin.register(EntityEdge)
class EntityEdgeAdmin(admin.ModelAdmin):
    list_display = ("src", "rel", "dst", "tenant")
    list_filter = ("rel", "tenant")


@admin.register(EntityChange)
class EntityChangeAdmin(admin.ModelAdmin):
    list_display = ("id", "entity", "entity_type", "changed_keys", "created_at", "processed_at")
    list_filter = ("entity_type", "tenant", "processed_at")
    readonly_fields = ("entity", "entity_type", "before", "after", "changed_keys", "created_at")


@admin.register(Trigger)
class TriggerAdmin(admin.ModelAdmin):
    list_display = ("name", "entity_type", "definition", "enabled", "tenant")
    list_filter = ("entity_type", "enabled", "tenant")
    search_fields = ("name",)
