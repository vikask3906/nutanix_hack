"""Create a second tenant with its own graph and its own workflows.

    python manage.py seed_tenant --slug acme --name "Acme Corp"

Deliberately reuses the SAME workflow name and the SAME entity external ids as
the default tenant. That is the interesting case: if isolation is real, the two
sets of rows coexist without colliding, and neither tenant can see the other's.
"""

from django.core.management.base import BaseCommand

from core.models import Tenant
from core.tenancy import set_current_tenant
from engine.models import WorkflowDef
from engine.service import create_definition
from graph.models import Trigger
from graph.service import upsert_entity

# Same names as the default tenant's, on purpose.
WORKFLOW = {
    "name": "employee_onboarding",
    "steps": [
        {
            "id": "welcome",
            "type": "noop",
            "config": {"output": {"greeting": "welcome to {{ input.company }}"}},
        },
        {
            "id": "grant_access",
            "type": "noop",
            "needs": ["welcome"],
            "config": {"output": {"granted": True}},
        },
    ],
}

TRIGGER = {
    "name": "onboard anyone joining engineering",
    "entity_type": "employee",
    "predicate": {
        "changed": ["department"],
        "match": {"department": "Engineering"},
        "was": {"department": {"ne": "Engineering"}},
    },
    "input_template": {"employee": "{{ entity.external_id }}", "company": "acme"},
}

# Same external ids as the default tenant's employees.
EMPLOYEES = [
    ("e_42", {"name": "Someone Else Entirely", "department": "Sales", "geo": "US"}),
    ("e_99", {"name": "Acme Person", "department": "Sales", "geo": "US"}),
]


class Command(BaseCommand):
    help = "Create a second tenant with colliding names, to demonstrate isolation."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="acme")
        parser.add_argument("--name", default="Acme Corp")

    def handle(self, *args, **options):
        tenant, created = Tenant.objects.get_or_create(
            slug=options["slug"], defaults={"name": options["name"]}
        )
        self.stdout.write(
            f"tenant {tenant.slug}: {'created' if created else 'already exists'}"
        )

        set_current_tenant(tenant)

        existing = (
            WorkflowDef.objects.filter(name=WORKFLOW["name"]).order_by("-version").first()
        )
        if existing:
            definition = existing
            self.stdout.write(f"  {existing} already up to date")
        else:
            definition = create_definition(tenant, WORKFLOW)
            self.stdout.write(self.style.SUCCESS(f"  created {definition}"))

        for external_id, attrs in EMPLOYEES:
            entity, _ = upsert_entity(tenant, "employee", external_id, attrs)
            self.stdout.write(f"  {entity.ref:<20} {attrs['department']}")

        trigger, made = Trigger.objects.update_or_create(
            tenant=tenant,
            name=TRIGGER["name"],
            defaults={
                "entity_type": TRIGGER["entity_type"],
                "predicate": TRIGGER["predicate"],
                "input_template": TRIGGER["input_template"],
                "definition": definition,
                "enabled": True,
            },
        )
        self.stdout.write(f"  {'created' if made else 'updated'} trigger {trigger.name!r}")

        self.stdout.write("")
        self.stdout.write("Compare the two tenants:")
        self.stdout.write("  curl -H 'X-Tenant: default' localhost:8000/api/workflows/")
        self.stdout.write(f"  curl -H 'X-Tenant: {tenant.slug}' localhost:8000/api/workflows/")
