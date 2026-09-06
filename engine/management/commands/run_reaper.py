"""The lease reaper.

    python manage.py run_reaper

Returns tasks whose holder stopped heartbeating to the ready queue. This is the
whole of Cascade's failure detection: a worker that stops renewing its lease is
gone, and its work goes back in the queue.

Runs as its own process so you can watch it act during a demo. It is safe to run
several - the claim uses FOR UPDATE SKIP LOCKED like everything else - and it is
safe for it to be down for a while, since recovery simply happens later.
"""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from engine.service import reap_expired_leases

log = logging.getLogger("cascade.reaper")


class Command(BaseCommand):
    help = "Return expired-lease tasks to the ready queue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true", help="Sweep once, then exit."
        )

    def handle(self, *args, **options):
        self._running = True

        def _stop(signum, _frame):
            log.info("signal %s received - stopping", signum)
            self._running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        log.info(
            "reaper online (lease %ss, sweep every %ss, max %s attempts per task)",
            settings.TASK_LEASE_SECONDS,
            settings.REAPER_POLL_SECONDS,
            settings.MAX_TASK_ATTEMPTS,
        )

        while self._running:
            try:
                recovered = reap_expired_leases()
            except Exception:  # noqa: BLE001 - the reaper must not die
                log.exception("reaper sweep failed")
                recovered = []

            for task, outcome in recovered:
                if outcome == "poisoned":
                    log.error(
                        "gave up on step=%s after %s attempts",
                        task.step_id, settings.MAX_TASK_ATTEMPTS,
                    )
                else:
                    log.warning(
                        "requeued step=%s (attempt %s)", task.step_id, task.attempt
                    )

            if options["once"]:
                break

            time.sleep(settings.REAPER_POLL_SECONDS)

        log.info("reaper offline")
