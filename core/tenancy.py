"""Tenant scoping.

Rippling runs one Employee Graph for thousands of companies. Cascade does the
same: every row carries a tenant, and the isolation is a property of the schema
and the default manager rather than of everyone remembering to filter.

THE DESIGN
----------
The current tenant lives in a ContextVar, so it is correct under threads and
under async without being passed through every function signature.

    Model.objects      scoped   - raises if no tenant context is set
    Model.all_tenants  unscoped - for the handful of places that legitimately
                                  work across tenants

Making the SCOPED manager the default is the whole point. A forgotten filter
should be a loud error, not a silent cross-tenant read - that class of bug does
not show up in testing, it shows up as one customer seeing another's payroll.

The places that genuinely span tenants are few and each is deliberate:

    claim_task           a worker takes whatever work is due, for anyone
    reap_expired_leases  the reaper does not care whose lease expired
    dispatch_one         the dispatcher drains one shared outbox
    the admin            an operator is explicitly looking across tenants

Each of those uses ``all_tenants`` and then enters the tenant's context before
touching anything else, so the code downstream stays scoped.

Foreign-key traversal (``task.run``) must never be scoped, or a worker holding
a task would be unable to load its own run. Django uses ``_base_manager`` for
that, which is why every scoped model sets ``base_manager_name``.
"""

import contextlib
from contextvars import ContextVar

from django.db import models

_current_tenant = ContextVar("cascade_current_tenant", default=None)


class NoTenantContext(RuntimeError):
    """A scoped query ran with no tenant set.

    Always a bug: either a request that skipped the middleware, or a background
    task that forgot to enter a tenant context. Loud on purpose.
    """


def get_current_tenant():
    return _current_tenant.get()


def set_current_tenant(tenant):
    """Set the tenant and return the token needed to restore the previous one."""
    return _current_tenant.set(tenant)


def reset_current_tenant(token):
    _current_tenant.reset(token)


@contextlib.contextmanager
def tenant_context(tenant):
    """Run a block as one tenant, restoring whatever was set before.

    Restoring rather than clearing matters: workers process tasks for many
    tenants in a loop, and leaking one task's tenant into the next would be the
    exact bug this module exists to prevent.
    """
    token = _current_tenant.set(tenant)
    try:
        yield tenant
    finally:
        _current_tenant.reset(token)


@contextlib.contextmanager
def unscoped():
    """Temporarily disable scoping. For migrations and shell work only."""
    token = _current_tenant.set(_UNSCOPED)
    try:
        yield
    finally:
        _current_tenant.reset(token)


class _Unscoped:
    """Sentinel meaning 'deliberately not scoped', distinct from 'not set'."""

    def __repr__(self):
        return "<unscoped>"


_UNSCOPED = _Unscoped()


class TenantScopedQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        return self.filter(tenant=tenant)


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """Filters every query by the current tenant, or refuses to run."""

    def get_queryset(self):
        tenant = _current_tenant.get()

        if tenant is _UNSCOPED:
            return super().get_queryset()

        if tenant is None:
            raise NoTenantContext(
                f"{self.model.__name__} was queried with no tenant context. "
                "Enter one with tenant_context(tenant), or use "
                f"{self.model.__name__}.all_tenants for a deliberate "
                "cross-tenant query."
            )

        return super().get_queryset().filter(tenant=tenant)
