"""Lease heartbeat.

A worker holds a task for ``TASK_LEASE_SECONDS``. Steps routinely take longer
than that - a slow HTTP call, a long shell command - so while a step runs, a
background thread renews the lease.

The alternative is a lease long enough for the slowest imaginable step, which
means a crashed worker's task sits unrecoverable for that long. Heartbeating lets
the lease be short (fast recovery) while still supporting slow steps.

The distinction the whole design turns on:

    Heartbeat still arriving  -> worker is alive, however slow the step is
    Heartbeat stopped         -> worker is gone, recover its work

There is no failure detector beyond that. No health checks, no membership
protocol, no consensus about who is up. A process that stops renewing is gone by
definition, and ``finish_task_if_owned`` stops it doing damage if it was merely
paused and comes back.

Threading note: each thread gets its own database connection, so the heartbeat
closes its connection on the way out. Leaking one per task would exhaust
Postgres' connection limit within a few hundred tasks.
"""

import logging
import threading

from django.conf import settings
from django.db import connection

from engine.service import renew_lease

log = logging.getLogger("cascade.heartbeat")


class LeaseHeartbeat:
    """Context manager that renews a task's lease until the block exits.

    Use ``lost`` after the block to find out whether the lease was taken from
    us mid-step::

        with LeaseHeartbeat(task, worker_id) as hb:
            output = plugin.execute(...)
        if hb.lost:
            ...  # discard the result, someone else owns this now
    """

    def __init__(self, task, worker_id, interval=None):
        self.task = task
        self.worker_id = worker_id
        self.interval = interval or settings.TASK_HEARTBEAT_SECONDS
        self.lost = False
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self.task.step_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            # Bounded join: a heartbeat thread must never hold up the worker.
            self._thread.join(timeout=self.interval + 5)
        return False

    def _loop(self):
        try:
            # Event.wait doubles as the sleep and the shutdown signal, so exit
            # is immediate rather than waiting out the current interval.
            while not self._stop.wait(self.interval):
                if not renew_lease(self.task, self.worker_id):
                    self.lost = True
                    log.warning(
                        "lost lease on step=%s (task %s) - another worker owns it now",
                        self.task.step_id, self.task.id,
                    )
                    return
        except Exception:  # noqa: BLE001 - never let this kill the worker
            log.exception("heartbeat thread failed for task %s", self.task.id)
        finally:
            connection.close()
