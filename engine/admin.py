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


from .models import Run, RunEvent, Task, WorkflowDef


class RunEventInline(admin.TabularInline):
    """The event log, inline on the run. This is your free debugging UI - when a
    run misbehaves, read the log top to bottom and the cause is always visible."""

    model = RunEvent
    extra = 0
    can_delete = False
    readonly_fields = ("seq", "type", "step_id", "payload", "created_at")
    fields = readonly_fields
    ordering = ("seq",)

    def has_add_permission(self, request, obj=None):
        return False


class TaskInline(admin.TabularInline):
    model = Task
    extra = 0
    readonly_fields = (
        "step_id",
        "kind",
        "attempt",
        "status",
        "run_after",
        "lease_owner",
        "lease_expires_at",
    )
    fields = readonly_fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WorkflowDef)
class WorkflowDefAdmin(AllTenantsAdmin):
    list_display = ("name", "version", "tenant", "created_at")
    list_filter = ("tenant", "name")
    search_fields = ("name",)


@admin.register(Run)
class RunAdmin(AllTenantsAdmin):
    list_display = ("id", "definition", "status", "entity_ref", "last_seq", "created_at")
    list_filter = ("status", "tenant", "definition__name")
    search_fields = ("id", "entity_ref")
    readonly_fields = ("id", "context", "last_seq", "created_at", "updated_at")
    inlines = [RunEventInline, TaskInline]


@admin.register(RunEvent)
class RunEventAdmin(AllTenantsAdmin):
    list_display = ("run", "seq", "type", "step_id", "created_at")
    list_filter = ("type", "tenant")
    search_fields = ("run__id", "step_id")


@admin.register(Task)
class TaskAdmin(AllTenantsAdmin):
    list_display = (
        "step_id",
        "run",
        "kind",
        "status",
        "attempt",
        "run_after",
        "lease_owner",
        "lease_expires_at",
    )
    list_filter = ("status", "kind", "tenant")
    search_fields = ("run__id", "step_id", "lease_owner")
