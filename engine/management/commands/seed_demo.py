"""Seed demo workflow definitions.

    python manage.py seed_demo

Two shapes, both runnable with no external services:

    demo_linear   a four-step pipeline, to watch state advance
    demo_diamond  fan-out and join, to watch three workers cooperate
"""

from django.core.management.base import BaseCommand

from engine.models import WorkflowDef
from engine.service import create_definition, default_tenant

LINEAR = {
    "name": "demo_linear",
    "steps": [
        {
            "id": "fetch",
            "type": "noop",
            "config": {"output": {"records": 3, "source": "inventory"}},
        },
        {
            "id": "transform",
            "type": "shell",
            "needs": ["fetch"],
            "config": {
                "cmd": "python -c \"print('transformed {{ steps.fetch.output.records }} records')\""
            },
        },
        {
            "id": "validate",
            "type": "shell",
            "needs": ["transform"],
            "config": {"cmd": "python -c \"print('validation passed')\""},
        },
        {
            "id": "publish",
            "type": "noop",
            "needs": ["validate"],
            "config": {"output": {"published": True}},
        },
    ],
}

DIAMOND = {
    "name": "demo_diamond",
    "steps": [
        {"id": "start", "type": "noop", "config": {"output": {"ok": True}}},
        {
            "id": "branch_a",
            "type": "shell",
            "needs": ["start"],
            "config": {"cmd": "python -c \"import time; time.sleep(2); print('A done')\""},
        },
        {
            "id": "branch_b",
            "type": "shell",
            "needs": ["start"],
            "config": {"cmd": "python -c \"import time; time.sleep(2); print('B done')\""},
        },
        {
            "id": "branch_c",
            "type": "shell",
            "needs": ["start"],
            "config": {"cmd": "python -c \"import time; time.sleep(2); print('C done')\""},
        },
        {
            "id": "join",
            "type": "noop",
            "needs": ["branch_a", "branch_b", "branch_c"],
            "config": {"output": {"merged": True}},
        },
    ],
}

FAILING = {
    "name": "demo_failing",
    "steps": [
        {"id": "setup", "type": "noop", "config": {"output": {"ready": True}}},
        {
            "id": "risky",
            "type": "shell",
            "needs": ["setup"],
            "config": {"cmd": "python -c \"raise SystemExit(1)\""},
        },
        {"id": "never_runs", "type": "noop", "needs": ["risky"]},
    ],
}


SLOW = {
    "name": "demo_slow",
    "steps": [
        {"id": "prepare", "type": "noop", "config": {"output": {"ready": True}}},
        {
            # Long enough to kill the worker holding it, and long enough that the
            # heartbeat has to renew the lease several times to stay alive.
            "id": "long_task",
            "type": "shell",
            "needs": ["prepare"],
            "config": {
                "cmd": "python -c \"import time; [time.sleep(1) for _ in range(25)]; print('long task finished')\"",
                "timeout_s": 120,
            },
        },
        {"id": "finish", "type": "noop", "needs": ["long_task"],
         "config": {"output": {"done": True}}},
    ],
}

FLAKY = {
    "name": "demo_flaky",
    "steps": [
        {"id": "setup", "type": "noop", "config": {"output": {"ready": True}}},
        {
            # Fails roughly two times in three, so the retry policy is what gets
            # it through. Exercises backoff without needing a real flaky service.
            "id": "unreliable",
            "type": "shell",
            "needs": ["setup"],
            "config": {
                "cmd": "python -c \"import random,sys; sys.exit(0 if random.random() < 0.34 else 1)\""
            },
            "retry": {"max": 6, "backoff": "exponential", "base_ms": 400, "max_ms": 5000},
        },
        {"id": "report", "type": "noop", "needs": ["unreliable"],
         "config": {"output": {"reported": True}}},
    ],
}


ROLLING_UPGRADE = {
    "name": "rolling_cluster_upgrade",
    "steps": [
        {
            "id": "preflight",
            "type": "http",
            "config": {
                "method": "GET",
                "url": "http://nodes:9000/nodes/{{ input.node }}/health",
            },
            "retry": {"max": 3, "backoff": "exponential", "base_ms": 300},
        },
        {
            # Stop scheduling work on the node. Undone by 'undrain'.
            "id": "drain",
            "type": "http",
            "needs": ["preflight"],
            "config": {
                "method": "POST",
                "url": "http://nodes:9000/nodes/{{ input.node }}/drain",
            },
            "compensate": {
                "type": "http",
                "config": {
                    "method": "POST",
                    "url": "http://nodes:9000/nodes/{{ input.node }}/undrain",
                },
            },
        },
        {
            # Bump the version. Undone by 'rollback', which restores it.
            "id": "upgrade",
            "type": "http",
            "needs": ["drain"],
            "config": {
                "method": "POST",
                "url": "http://nodes:9000/nodes/{{ input.node }}/upgrade",
            },
            "compensate": {
                "type": "http",
                "config": {
                    "method": "POST",
                    "url": "http://nodes:9000/nodes/{{ input.node }}/rollback",
                },
            },
        },
        {
            # The gate. If the node is unhealthy after upgrading, everything
            # above is unwound in reverse: rollback, then undrain.
            "id": "verify",
            "type": "http",
            "needs": ["upgrade"],
            "config": {
                "method": "GET",
                "url": "http://nodes:9000/nodes/{{ input.node }}/health",
            },
            "on_error": "compensate",
        },
        {
            "id": "return_to_service",
            "type": "http",
            "needs": ["verify"],
            "config": {
                "method": "POST",
                "url": "http://nodes:9000/nodes/{{ input.node }}/undrain",
            },
        },
    ],
}


class Command(BaseCommand):
    help = "Create the demo workflow definitions."

    def handle(self, *args, **options):
        tenant = default_tenant()
        self.stdout.write(f"tenant: {tenant.slug}")

        for spec in (LINEAR, DIAMOND, FAILING, SLOW, FLAKY, ROLLING_UPGRADE):
            existing = WorkflowDef.objects.filter(
                tenant=tenant, name=spec["name"]
            ).order_by("-version").first()

            if existing and existing.spec.get("steps") == spec["steps"]:
                self.stdout.write(f"  {existing} already up to date")
                continue

            definition = create_definition(tenant, spec)
            self.stdout.write(self.style.SUCCESS(f"  created {definition}"))

        self.stdout.write("")
        self.stdout.write("Start a run:")
        self.stdout.write(
            '  curl -X POST http://localhost:8000/api/runs/ '
            '-H "Content-Type: application/json" '
            '-d \'{"workflow": "demo_diamond"}\''
        )
