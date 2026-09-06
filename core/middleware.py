"""Resolve the tenant for each request.

Order of preference:

    X-Tenant: acme          an explicit header
    acme.cascade.local      the subdomain
    (otherwise)             the "default" tenant

Real deployments would use the subdomain or a signed token and would never let a
client name its own tenant in a plain header. The header is here because it
makes the isolation demonstrable from curl, and because a hackathon has no
identity provider. That is a deliberate, stated shortcut - not an oversight.

The tenant is set in a ContextVar for the duration of the request and reset
afterwards. Resetting is not optional: a worker thread serving the next request
would otherwise inherit the previous tenant, which is precisely the bug tenant
scoping exists to prevent.
"""

import logging

from core.models import Tenant
from core.tenancy import reset_current_tenant, set_current_tenant

log = logging.getLogger("cascade.tenancy")

HEADER = "HTTP_X_TENANT"
DEFAULT_SLUG = "default"

# Paths that must work without a tenant, or before any tenant exists.
EXEMPT_PREFIXES = ("/healthz", "/admin", "/static")


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(EXEMPT_PREFIXES):
            return self.get_response(request)

        slug = request.META.get(HEADER) or self._from_host(request) or DEFAULT_SLUG

        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            if slug != DEFAULT_SLUG:
                log.warning("unknown tenant %r, falling back to default", slug)
            tenant, _ = Tenant.objects.get_or_create(
                slug=DEFAULT_SLUG, defaults={"name": "Default"}
            )

        token = set_current_tenant(tenant)
        request.tenant = tenant
        try:
            return self.get_response(request)
        finally:
            reset_current_tenant(token)

    @staticmethod
    def _from_host(request):
        host = request.get_host().split(":")[0]
        parts = host.split(".")
        if len(parts) >= 3 and parts[0] not in {"www", "localhost"}:
            return parts[0]
        return None
