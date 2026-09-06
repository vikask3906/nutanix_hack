"""Seed the entity graph demo: onboarding workflow, entities and triggers.

    python manage.py seed_graph
"""

from django.core.management.base import BaseCommand

from engine.models import WorkflowDef
from engine.service import create_definition, default_tenant
from graph.models import Trigger
from graph.service import upsert_entity

ONBOARDING = {
    "name": "employee_onboarding",
    "steps": [
        {
            "id": "assign_device",
            "type": "http",
            "config": {
                "method": "POST",
                "url": "http://nodes:9000/devices/assign",
                "body": {
                    "employee": "{{ input.employee }}",
                    "device": "laptop-standard",
                },
            },
            "retry": {"max": 3, "backoff": "exponential", "base_ms": 300},
        },
        {
            # These three have no dependency on each other, so the engine fans
            # them out across workers. Nothing in the spec says "run these in
            # parallel" - the absence of a needs edge is what says it.
            "id": "provision_github",
            "type": "http",
            "needs": ["assign_device"],
            "config": {
                "method": "POST",
                "url": "http://nodes:9000/apps/github/accounts",
                "body": {"employee": "{{ input.employee }}", "role": "developer"},
            },
        },
        {
            "id": "provision_slack",
            "type": "http",
            "needs": ["assign_device"],
            "config": {
                "method": "POST",
                "url": "http://nodes:9000/apps/slack/accounts",
                "body": {"employee": "{{ input.employee }}", "role": "member"},
            },
        },
        {
            "id": "provision_pagerduty",
            "type": "http",
            "needs": ["assign_device"],
            "config": {
                "method": "POST",
                "url": "http://nodes:9000/apps/pagerduty/accounts",
                "body": {"employee": "{{ input.employee }}", "role": "responder"},
            },
        },
        {
            "id": "notify_manager",
            "type": "noop",
            "needs": ["provision_github", "provision_slack", "provision_pagerduty"],
            "config": {
                "output": {
                    "notified": "{{ input.manager }}",
                    "about": "{{ input.employee }}",
                }
            },
        },
    ],
}


TRIGGERS = [
    {
        "name": "onboard engineers in India",
        "entity_type": "employee",
        "workflow": "employee_onboarding",
        # Reads as: when department changes, to Engineering in India, from
        # anything that was not already Engineering.
        #
        # The 'was' clause is what stops a transfer within Engineering (say a
        # title change that also touches department) re-running onboarding for
        # someone who already has all their accounts.
        "predicate": {
            "changed": ["department"],
            "match": {"department": "Engineering", "geo": "IN"},
            "was": {"department": {"ne": "Engineering"}},
        },
        "input_template": {
            "employee": "{{ entity.external_id }}",
            "manager": "{{ entity.attrs.manager }}",
        },
    },
    {
        "name": "repair unhealthy nodes",
        "entity_type": "node",
        "workflow": "rolling_cluster_upgrade",
        # The same engine, driven by a completely different entity type, with
        # the graph change mapped into the workflow's own input shape.
        "predicate": {
            "changed": ["status"],
            "match": {"status": "unhealthy"},
            "was": {"status": "healthy"},
        },
        "input_template": {"node": "{{ entity.external_id }}"},
    },
]


EMPLOYEES = [
    ("e_42", {"name": "Asha Menon", "department": "Support", "geo": "IN",
              "manager": "m_07", "title": "Support Engineer"}),
    ("e_43", {"name": "Rahul Verma", "department": "Engineering", "geo": "IN",
              "manager": "m_07", "title": "Backend Engineer"}),
]

NODES = [
    (f"node-{i}", {"status": "healthy", "version": "7.0", "rack": f"r{(i % 2) + 1}"})
    for i in range(1, 6)
]


class Command(BaseCommand):
    help = "Seed the entity graph, the onboarding workflow and the triggers."

    def handle(self, *args, **options):
        tenant = default_tenant()

        existing = WorkflowDef.objects.filter(
            tenant=tenant, name=ONBOARDING["name"]
        ).order_by("-version").first()

        if existing and existing.spec.get("steps") == ONBOARDING["steps"]:
            definition = existing
            self.stdout.write(f"  {existing} already up to date")
        else:
            definition = create_definition(tenant, ONBOARDING)
            self.stdout.write(self.style.SUCCESS(f"  created {definition}"))

        self.stdout.write("")
        self.stdout.write("entities:")
        for external_id, attrs in EMPLOYEES:
            entity, _ = upsert_entity(tenant, "employee", external_id, attrs)
            self.stdout.write(f"  {entity.ref:<20} {attrs['department']}")
        for external_id, attrs in NODES:
            entity, _ = upsert_entity(tenant, "node", external_id, attrs)
            self.stdout.write(f"  {entity.ref:<20} {attrs['status']}")

        self.stdout.write("")
        self.stdout.write("triggers:")
        for spec in TRIGGERS:
            target = (
                WorkflowDef.objects.filter(tenant=tenant, name=spec["workflow"])
                .order_by("-version")
                .first()
            )
            if target is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"  skipped {spec['name']!r}: no workflow {spec['workflow']!r} "
                        "(run seed_demo first)"
                    )
                )
                continue

            trigger, created = Trigger.objects.update_or_create(
                tenant=tenant,
                name=spec["name"],
                defaults={
                    "entity_type": spec["entity_type"],
                    "predicate": spec["predicate"],
                    "input_template": spec["input_template"],
                    "definition": target,
                    "enabled": True,
                },
            )
            word = "created" if created else "updated"
            self.stdout.write(f"  {word} {trigger.name!r} -> {target.name}")

        self.stdout.write("")
        self.stdout.write("Try it:")
        self.stdout.write(
            '  curl -X PATCH http://localhost:8000/api/entities/employee/e_42/ '
            '-H "Content-Type: application/json" '
            '-d \'{"attrs": {"department": "Engineering"}}\''
        )
