"""Put everything back to its starting state, for another take.

    python manage.py reset_demo

Resets the fake systems (cluster back to 7.0, no accounts, no laptops) and
rewinds the demo entities so the triggers will fire again. Run this between
recordings - a trigger that has already fired will not fire twice, because the
predicate requires a transition, and take two would silently do nothing.

Past runs are left alone. They are the audit trail, and clearing them would
undermine the point.
"""

import requests
from django.core.management.base import BaseCommand

from core.tenancy import set_current_tenant
from engine.service import default_tenant
from graph.service import upsert_entity

SYSTEMS = "http://nodes:9000"

PEOPLE = ["priya.sharma", "rahul.nair"]
NODES = [f"node-{i}" for i in range(1, 6)]


class Command(BaseCommand):
    help = "Reset the demo to its starting state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url", default=SYSTEMS, help="Base URL of the fake systems service."
        )

    def handle(self, *args, **options):
        tenant = default_tenant()
        set_current_tenant(tenant)

        try:
            requests.post(f"{options['url']}/reset", timeout=10)
            self.stdout.write(self.style.SUCCESS("  fake systems reset"))
            self.stdout.write("    cluster : 5 nodes, version 7.0, healthy, in service")
            self.stdout.write("    company : no mailboxes, no Slack, no GitHub, no payroll")
        except requests.RequestException as exc:
            self.stdout.write(self.style.WARNING(f"  could not reach {options['url']}: {exc}"))

        for person in PEOPLE:
            upsert_entity(tenant, "person", person, {"employment_status": "offer_accepted"})
        self.stdout.write(f"  {len(PEOPLE)} people rewound to offer_accepted")

        for node in NODES:
            upsert_entity(tenant, "node", node, {"status": "healthy", "version": "7.0"})
        self.stdout.write(f"  {len(NODES)} nodes rewound to healthy")

        self.stdout.write("")
        self.stdout.write("Ready. The onboarding trigger will fire again on:")
        self.stdout.write(
            "  curl -X PATCH http://localhost:8000/api/entities/person/priya.sharma/ "
            '-H "Content-Type: application/json" '
            '-d \'{"attrs":{"employment_status":"hired"}}\''
        )
