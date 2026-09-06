"""Replay: turning an event log back into workflow state.

Pure Python. No Django, no database, no I/O.

This module is the heart of Cascade. Everything else - the API, the worker loop,
the admin, the UI - is plumbing around these functions.

The contract, and the reason the whole system is crash-proof:

    replay(events) is a PURE FOLD.

    Same events in, same context out. Always. No clock, no randomness, no network,
    no reads of anything outside the list you pass in.

Because of that, any worker on any machine at any time can reconstruct the exact
state of a run from its log alone. There is no handover between workers, no shared
memory, no sticky sessions. A worker that dies mid-step took nothing with it,
because it never held anything the log did not already contain.

If you are ever tempted to put something in the context that cannot be derived
from the events - a timestamp from the clock, a random id, a cached lookup - stop.
That is the moment the crash-recovery guarantee breaks.
"""

from engine.spec import needs_of, steps_by_id


class StepStatus:
    PENDING = "PENDING"                    # never scheduled
    SCHEDULED = "SCHEDULED"                # a task row exists, not yet picked up
    RUNNING = "RUNNING"                    # a worker is executing it now
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"                      # terminally failed, retries exhausted
    RETRY_SCHEDULED = "RETRY_SCHEDULED"    # failed, but will be tried again
    WAITING = "WAITING"                    # blocked on a durable timer
    COMPENSATED = "COMPENSATED"            # succeeded, then rolled back


class RunState:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPENSATING = "COMPENSATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_RUN_STATES = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}

# A step in one of these states must not be scheduled again - either something is
# already working on it, or it is finished.
CLAIMED_STEP_STATES = {
    StepStatus.SCHEDULED,
    StepStatus.RUNNING,
    StepStatus.SUCCEEDED,
    StepStatus.FAILED,
    StepStatus.WAITING,
    StepStatus.RETRY_SCHEDULED,
    StepStatus.COMPENSATED,
}


def _field(event, key, default=None):
    """Read a field from either a RunEvent model instance or a plain dict.

    Tests use dicts (no database needed); the worker passes model instances.
    Supporting both is what keeps this module free of Django.
    """
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def new_context():
    """The zero state: what a run looks like before anything has happened."""
    return {
        "input": {},
        "steps": {},             # step_id -> {status, output, error, attempts, ...}
        "status": RunState.PENDING,
        "compensating": False,
        "completion_order": [],  # step ids in the order they SUCCEEDED
        "error": "",
        "needs_manual_intervention": False,
        "last_seq": 0,
    }


def _step_slot(context, step_id):
    return context["steps"].setdefault(
        step_id,
        {
            "status": StepStatus.PENDING,
            "output": None,
            "error": "",
            "attempts": 0,
            "compensated": False,
            "compensation_failed": False,
        },
    )


def apply_event(context, event):
    """Apply one event to the context, in place. Returns the context.

    This is the only place that knows how each event type changes state. Adding a
    new event type means adding a branch here and nowhere else.
    """
    etype = _field(event, "type")
    step_id = _field(event, "step_id") or ""
    payload = _field(event, "payload") or {}
    seq = _field(event, "seq", 0) or 0

    if etype == "RUN_STARTED":
        context["input"] = payload.get("input", {})
        context["status"] = RunState.RUNNING

    elif etype == "STEP_SCHEDULED":
        slot = _step_slot(context, step_id)
        slot["status"] = StepStatus.SCHEDULED

    elif etype == "STEP_STARTED":
        slot = _step_slot(context, step_id)
        slot["status"] = StepStatus.RUNNING
        slot["attempts"] = payload.get("attempt", slot["attempts"] + 1)

    elif etype == "STEP_SUCCEEDED":
        slot = _step_slot(context, step_id)
        slot["status"] = StepStatus.SUCCEEDED
        slot["output"] = payload.get("output")
        slot["error"] = ""
        # Completion order drives compensation: we unwind in reverse of this.
        if step_id not in context["completion_order"]:
            context["completion_order"].append(step_id)

    elif etype == "STEP_FAILED":
        slot = _step_slot(context, step_id)
        slot["status"] = StepStatus.FAILED
        slot["error"] = payload.get("error", "")

    elif etype == "STEP_RETRY_SCHEDULED":
        slot = _step_slot(context, step_id)
        slot["status"] = StepStatus.RETRY_SCHEDULED
        slot["error"] = payload.get("error", slot["error"])

    elif etype == "TIMER_SET":
        slot = _step_slot(context, step_id)
        slot["status"] = StepStatus.WAITING

    elif etype == "TIMER_FIRED":
        slot = _step_slot(context, step_id)
        slot["status"] = StepStatus.SUCCEEDED
        slot["output"] = payload.get("output", {})
        if step_id not in context["completion_order"]:
            context["completion_order"].append(step_id)

    elif etype == "COMPENSATION_STARTED":
        context["compensating"] = True
        context["status"] = RunState.COMPENSATING
        context["error"] = payload.get("error", context["error"])

    elif etype == "STEP_COMPENSATED":
        slot = _step_slot(context, step_id)
        slot["status"] = StepStatus.COMPENSATED
        slot["compensated"] = True

    elif etype == "COMPENSATION_FAILED":
        # Rollback itself failed. We cannot undo this step and we must not
        # retry it forever, so it is marked and skipped. The run ends FAILED
        # with the system in a partially-unwound state that a human must look
        # at - which is the honest outcome, not a hidden one.
        slot = _step_slot(context, step_id)
        slot["compensation_failed"] = True
        slot["error"] = payload.get("error", slot["error"])
        context["needs_manual_intervention"] = True
        context["error"] = payload.get("error", context["error"])

    elif etype == "RUN_SUCCEEDED":
        context["status"] = RunState.SUCCEEDED

    elif etype == "RUN_FAILED":
        context["status"] = RunState.FAILED
        context["error"] = payload.get("error", context["error"])

    elif etype == "RUN_CANCELLED":
        context["status"] = RunState.CANCELLED

    # Unknown event types are ignored on purpose. An older worker replaying a log
    # written by a newer one should degrade, not crash.

    context["last_seq"] = max(context["last_seq"], seq)
    return context


def replay(events):
    """Fold an event log into a context. The pure function everything rests on.

    Events are sorted by seq, so passing them in any order gives the same result.
    That matters more than it looks: it means a worker never has to care how the
    database returned the rows.
    """
    context = new_context()
    for event in sorted(events, key=lambda e: _field(e, "seq", 0) or 0):
        apply_event(context, event)
    return context


def ready_steps(spec, context):
    """Which steps can be scheduled right now.

    A step is ready when it has never been claimed, and every step it needs has
    SUCCEEDED. Returned in spec order so behaviour is deterministic.

    Note what this does NOT do: it does not walk a plan or track a cursor. It
    recomputes readiness from scratch every time, from state that came from the
    log. That is why two workers can ask this question concurrently and neither
    needs to know the other exists.
    """
    if context["compensating"] or context["status"] in TERMINAL_RUN_STATES:
        return []

    ready = []
    for step in spec.get("steps", []):
        sid = step["id"]
        status = context["steps"].get(sid, {}).get("status", StepStatus.PENDING)

        if status in CLAIMED_STEP_STATES:
            continue

        deps = needs_of(step)
        if all(
            context["steps"].get(dep, {}).get("status") == StepStatus.SUCCEEDED
            for dep in deps
        ):
            ready.append(sid)

    return ready


def compensation_order(spec, context):
    """Steps to roll back, in reverse order of completion.

    Only steps that actually succeeded, declare a compensate block, and have not
    already been compensated. Reverse completion order - not reverse spec order -
    because with parallel branches the order things finished in is the only order
    that is safe to undo.
    """
    by_id = steps_by_id(spec)
    out = []

    for sid in reversed(context["completion_order"]):
        step = by_id.get(sid)
        if not step or not step.get("compensate"):
            continue
        slot = context["steps"].get(sid, {})
        if slot.get("compensated") or slot.get("compensation_failed"):
            continue
        out.append(sid)

    return out


def is_complete(spec, context):
    """True when every step in the spec has SUCCEEDED."""
    return all(
        context["steps"].get(s["id"], {}).get("status") == StepStatus.SUCCEEDED
        for s in spec.get("steps", [])
    )


def failed_steps(spec, context):
    """Steps that are terminally failed AND fatal to the run.

    A step declaring ``on_error: continue`` still records its failure - the log
    must stay honest - but it does not condemn the run. Its dependents are
    blocked anyway, since readiness requires SUCCEEDED, so the branch simply
    stops without taking the whole workflow with it.
    """
    return [
        s["id"]
        for s in spec.get("steps", [])
        if context["steps"].get(s["id"], {}).get("status") == StepStatus.FAILED
        and s.get("on_error", "fail") != "continue"
    ]


def next_run_state(spec, context):
    """What the run's status should be, given the current context.

    Derived, never stored as an independent fact - the same discipline as the
    context itself. If this disagrees with the Run row, the Run row is wrong.
    """
    if context["status"] in TERMINAL_RUN_STATES:
        return context["status"]

    if context["compensating"]:
        if compensation_order(spec, context):
            return RunState.COMPENSATING
        return RunState.FAILED  # nothing left to unwind

    if failed_steps(spec, context):
        return RunState.FAILED

    if is_complete(spec, context):
        return RunState.SUCCEEDED

    return RunState.RUNNING
