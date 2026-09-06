"""The worker process.

BLOCK 0 STUB. Right now it only proves the process model works: it boots, claims a
stable identity, reaches the database, and idles. Block 2 replaces the body of the
loop with the real claim-execute-record cycle.

The identity matters and is not cosmetic. ``lease_owner`` on a claimed task is set
to this string, so when a worker dies you can see in the admin exactly which one
was holding the work when the lease expired.
"""

import logging
import os
import signal
import socket
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

log = logging.getLogger("cascade.worker")


class Command(BaseCommand):
    help = "Run a Cascade worker: claim tasks, execute steps, record results."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain whatever is ready, then exit (useful in tests).",
        )

    def handle(self, *args, **options):
        identity = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._running = True

        # Graceful shutdown: finish the current step, then stop. A SIGKILL (kill -9)
        # deliberately skips this - that is the crash path the lease reaper covers.
        def _stop(signum, _frame):
            log.info("signal %s received, finishing current task then exiting", signum)
            self._running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        with connection.cursor() as cur:
            cur.execute("SELECT version()")
            db_version = cur.fetchone()[0].split(",")[0]

        log.info("worker %s online", identity)
        log.info("  database      : %s", db_version)
        log.info("  lease         : %ss", settings.TASK_LEASE_SECONDS)
        log.info("  heartbeat     : %ss", settings.TASK_HEARTBEAT_SECONDS)
        log.info("  poll interval : %ss", settings.WORKER_POLL_SECONDS)
        log.info("BLOCK 0 stub - no task claiming yet; idling.")

        while self._running:
            # Block 2: claim_one_task(identity) -> execute -> record -> enqueue next
            if options["once"]:
                break
            time.sleep(settings.WORKER_POLL_SECONDS)

        log.info("worker %s offline", identity)
