"""Seed the new-joiner demo: the workflow every company runs by hand.

    python manage.py seed_joiner

The scenario, in plain terms:

    Someone accepts a job offer. Five different systems need to know:
    Google Workspace, Slack, GitHub, the payroll provider, and IT.

    Today a person works down a checklist. If they get halfway and the
    payroll provider rejects the tax id, the company is left with someone
    who has WRITE ACCESS TO THE CODEBASE and no employment record. Nobody
    notices, because the checklist has no undo.

    This workflow does the checklist, and it has an undo.
"""

from django.core.management.base import BaseCommand

from core.tenancy import set_current_tenant
from engine.models import WorkflowDef
from engine.service import create_definition, default_tenant
from graph.models import Trigger
from graph.service import upsert_entity

SYS = "http://nodes:9000"

NEW_JOINER = {
    "name": "new_joiner",
    "description": "Everything that has to happen when someone accepts an offer.",
    "steps": [
        {
            "id": "create_email_account",
            "type": "http",
            "config": {
                "method": "POST",
                "url": f"{SYS}/google/create-account",
                "body": {"person": "{{ input.person }}"},
            },
            "retry": {"max": 3, "backoff": "exponential", "base_ms": 300},
            # If we later have to unwind, the mailbox must go too.
            "compensate": {
                "type": "http",
                "config": {
                    "method": "POST",
                    "url": f"{SYS}/google/delete-account",
                    "body": {"person": "{{ input.person }}"},
                },
            },
        },
        {
            # These three have no dependency on each other, so they run at the
            # same time on different workers. A person doing this by hand does
            # them one after another.
            "id": "order_laptop",
            "type": "http",
            "needs": ["create_email_account"],
            "config": {
                "method": "POST",
                "url": f"{SYS}/it/order-laptop",
                "body": {"person": "{{ input.person }}", "model": "MacBook Pro 14"},
            },
            "compensate": {
                "type": "http",
                "config": {
                    "method": "POST",
                    "url": f"{SYS}/it/cancel-laptop",
                    "body": {"person": "{{ input.person }}"},
                },
            },
        },
        {
            "id": "invite_to_slack",
            "type": "http",
            "needs": ["create_email_account"],
            "config": {
                "method": "POST",
                "url": f"{SYS}/slack/invite",
                "body": {"person": "{{ input.person }}"},
            },
            "compensate": {
                "type": "http",
                "config": {
                    "method": "POST",
                    "url": f"{SYS}/slack/deactivate",
                    "body": {"person": "{{ input.person }}"},
                },
            },
        },
        {
            "id": "grant_github_access",
            "type": "http",
            "needs": ["create_email_account"],
            "config": {
                "method": "POST",
                "url": f"{SYS}/github/add-member",
                "body": {"person": "{{ input.person }}", "access": "write"},
            },
            "compensate": {
                "type": "http",
                "config": {
                    "method": "POST",
                    "url": f"{SYS}/github/remove-member",
                    "body": {"person": "{{ input.person }}"},
                },
            },
        },
        {
            # The step that can legitimately refuse. A tax id that will not
            # verify is not a transient error - no amount of retrying fixes it,
            # and the person cannot be employed until it is sorted out.
            #
            # on_error: compensate is what turns a half-onboarded person into
            # no person at all.
            "id": "enrol_in_payroll",
            "type": "http",
            "needs": ["order_laptop", "invite_to_slack", "grant_github_access"],
            "config": {
                "method": "POST",
                "url": f"{SYS}/payroll/enrol",
                "body": {"person": "{{ input.person }}", "band": "{{ input.band }}"},
            },
            "on_error": "compensate",
        },
        {
            "id": "email_the_manager",
            "type": "noop",
            "needs": ["enrol_in_payroll"],
            "config": {
                "output": {
                    "to": "{{ input.manager }}",
                    "subject": "{{ input.person }} is fully set up",
                }
            },
        },
    ],
}

TRIGGER = {
    "name": "onboard anyone who accepts an offer",
    "entity_type": "person",
    # Reads as: when someone's employment status changes to 'hired',
    # from anything that was not already 'hired', start onboarding them.
    "predicate": {
        "changed": ["employment_status"],
        "match": {"employment_status": "hired"},
        "was": {"employment_status": {"ne": "hired"}},
    },
    "input_template": {
        "person": "{{ entity.external_id }}",
        "manager": "{{ entity.attrs.manager }}",
        "band": "{{ entity.attrs.band }}",
    },
}

PEOPLE = [
    ("priya.sharma", {"full_name": "Priya Sharma", "role": "Backend Engineer",
                      "manager": "asha.menon", "band": "IC3",
                      "employment_status": "offer_accepted"}),
    ("rahul.nair", {"full_name": "Rahul Nair", "role": "Data Engineer",
                    "manager": "asha.menon", "band": "IC2",
                    "employment_status": "offer_accepted"}),
]


class Command(BaseCommand):
    help = "Seed the new-joiner workflow, the people, and the trigger."

    def handle(self, *args, **options):
        tenant = default_tenant()
        set_current_tenant(tenant)

        existing = (
            WorkflowDef.objects.filter(name=NEW_JOINER["name"])
            .order_by("-version").first()
        )
        if existing and existing.spec.get("steps") == NEW_JOINER["steps"]:
            definition = existing
            self.stdout.write(f"  {existing} already up to date")
        else:
            definition = create_definition(tenant, NEW_JOINER)
            self.stdout.write(self.style.SUCCESS(f"  created {definition}"))

        self.stdout.write("")
        self.stdout.write("people:")
        for external_id, attrs in PEOPLE:
            entity, _ = upsert_entity(tenant, "person", external_id, attrs)
            self.stdout.write(
                f"  {attrs['full_name']:<16} {attrs['role']:<20} "
                f"{attrs['employment_status']}"
            )

        trigger, created = Trigger.objects.update_or_create(
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
        self.stdout.write("")
        self.stdout.write(f"  {'created' if created else 'updated'} trigger: {trigger.name}")
        self.stdout.write("")
        self.stdout.write("Hire Priya:")
        self.stdout.write(
            "  curl -X PATCH http://localhost:8000/api/entities/person/priya.sharma/ "
            '-H "Content-Type: application/json" '
            '-d \'{"attrs": {"employment_status": "hired"}}\''
        )
