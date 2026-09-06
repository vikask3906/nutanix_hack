"""The dispatcher process.

    python manage.py run_dispatcher

Reads the entity-change outbox and starts the workflows whose triggers match.
Its own process, so you can watch it fire during a demo.
"""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from graph.dispatcher import dispatch_pending

log = logging.getLogger("cascade.dispatcher")


class Command(BaseCommand):
    help = "Turn entity-graph changes into workflow runs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Drain once, then exit.")

    def handle(self, *args, **options):
        self._running = True

        def _stop(signum, _frame):
            log.info("signal %s received - stopping", signum)
            self._running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        log.info("dispatcher online (polling every %ss)", settings.DISPATCHER_POLL_SECONDS)

        while self._running:
            try:
                processed = dispatch_pending()
            except Exception:  # noqa: BLE001 - the dispatcher must not die
                log.exception("dispatch sweep failed")
                processed = []

            for change, started in processed:
                if started:
                    names = ", ".join(t.name for t, _ in started)
                    log.info(
                        "change #%s on %s fired: %s",
                        change.id, change.entity_type, names,
                    )

            if options["once"]:
                break

            time.sleep(settings.DISPATCHER_POLL_SECONDS)

        log.info("dispatcher offline")
