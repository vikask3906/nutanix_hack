"""Server-sent events for a run.

    GET /api/runs/<id>/stream/

Each RunEvent becomes one SSE frame as it is appended, so a browser watches a
workflow execute rather than polling for it.

WHY POLLING, NOT LISTEN/NOTIFY
------------------------------
Postgres has LISTEN/NOTIFY and it would push events with no polling at all. It
is the better answer at scale, and worth saying so out loud rather than
pretending this is optimal.

It is not what is here, for one honest reason: LISTEN needs a dedicated
connection held open outside Django's connection pooling for the life of the
stream, and getting that wrong leaks connections until Postgres refuses new
ones. A 300ms indexed query on ``(run_id, seq)`` costs almost nothing at demo
scale and cannot leak. The seam is `_events_since` - swapping it for a
notification listener touches one function.

The event log makes this easy in a way a mutable-state design would not: a
client only ever needs "everything after seq N", so a reconnecting browser
resumes exactly where it left off with no snapshot and no missed updates.
"""

import json
import time

from django.db import close_old_connections
from django.http import StreamingHttpResponse

from engine.models import Run, RunEvent

STREAM_TIMEOUT_SECONDS = 600
POLL_SECONDS = 0.3
HEARTBEAT_SECONDS = 15

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _frame(event_type, payload):
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def _events_since(run_id, last_seq):
    return list(
        RunEvent.objects.filter(run_id=run_id, seq__gt=last_seq)
        .order_by("seq")
        .values("seq", "type", "step_id", "payload", "created_at")
    )


def stream_run(request, pk):
    """Stream one run's events until it reaches a terminal state."""

    def generate():
        last_seq = int(request.GET.get("since", 0))
        last_status = None
        last_beat = time.time()
        deadline = time.time() + STREAM_TIMEOUT_SECONDS

        try:
            while time.time() < deadline:
                for event in _events_since(pk, last_seq):
                    last_seq = event["seq"]
                    event["created_at"] = event["created_at"].isoformat()
                    yield _frame("run_event", event)
                    last_beat = time.time()

                run = Run.objects.filter(pk=pk).values("status", "context").first()
                if run is None:
                    yield _frame("error", {"error": "run not found"})
                    return

                if run["status"] != last_status:
                    last_status = run["status"]
                    yield _frame("status", {"status": run["status"]})
                    last_beat = time.time()

                if run["status"] in TERMINAL:
                    yield _frame("done", {"status": run["status"], "last_seq": last_seq})
                    return

                if time.time() - last_beat > HEARTBEAT_SECONDS:
                    # A comment frame. Keeps proxies and browsers from deciding
                    # a quiet stream - a run parked on a three-day timer, say -
                    # is a dead one.
                    yield ": keepalive\n\n"
                    last_beat = time.time()

                time.sleep(POLL_SECONDS)

            yield _frame("timeout", {"last_seq": last_seq})
        finally:
            # This generator outlives the normal request cycle, so Django's
            # usual connection cleanup has already run. Without this, every
            # stream leaks a connection until Postgres starts refusing them.
            close_old_connections()

    response = StreamingHttpResponse(generate(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # tell nginx not to buffer the stream
    return response
