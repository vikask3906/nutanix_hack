"""The worker process.

    python manage.py run_worker

Stateless and horizontally scalable - run as many as you like:

    docker compose up --scale worker=3

The identity below is not cosmetic. It is written to ``Task.lease_owner``, so
when a worker dies you can see in the admin exactly which one was holding the
work when its lease expired.
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

from engine.steps import registered_types
from engine.worker import run_once

log = logging.getLogger("cascade.worker")


class Command(BaseCommand):
    help = "Run a Cascade worker: claim tasks, execute steps, record results."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain everything currently due, then exit. Useful in tests.",
        )
        parser.add_argument(
            "--name",
            default="",
            help="Override the worker identity (default: host:pid:random).",
        )

    def handle(self, *args, **options):
        identity = options["name"] or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self._running = True

        # SIGTERM is the graceful path: finish the step in hand, then stop.
        # SIGKILL (kill -9) deliberately bypasses this - that is the crash path,
        # and the lease reaper is what covers it.
        def _stop(signum, _frame):
            log.info("signal %s received - finishing current task, then exiting", signum)
            self._running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        with connection.cursor() as cur:
            cur.execute("SELECT version()")
            db_version = cur.fetchone()[0].split(",")[0]

        log.info("worker %s online", identity)
        log.info("  database   : %s", db_version)
        log.info("  step types : %s", ", ".join(registered_types()))
        log.info("  lease      : %ss", settings.TASK_LEASE_SECONDS)

        idle_logged = False

        while self._running:
            try:
                did_work = run_once(identity)
            except Exception:  # noqa: BLE001 - one bad task must not kill the worker
                log.exception("unhandled error in worker loop")
                time.sleep(settings.WORKER_POLL_SECONDS)
                continue

            if did_work:
                idle_logged = False
                continue

            if options["once"]:
                break

            if not idle_logged:
                log.info("[%s] queue empty - waiting", identity.split(":")[-1][:8])
                idle_logged = True

            time.sleep(settings.WORKER_POLL_SECONDS)

        log.info("worker %s offline", identity)
