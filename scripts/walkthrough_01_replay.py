"""Walkthrough 1: how an event log becomes workflow state.

Run it:

    .venv\\Scripts\\python.exe scripts/walkthrough_01_replay.py

No database, no Docker, no Django. This is the engine's brain in isolation.

Three scenes:
    1. The happy path      - watch state accumulate one event at a time
    2. Crash and resume    - a second worker picks up a half-finished log
    3. Failure and rollback - compensation order is reverse of completion
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.replay import (  # noqa: E402
    compensation_order,
    new_context,
    next_run_state,
    apply_event,
    ready_steps,
    replay,
)
from engine.spec import topological_order, validate_spec  # noqa: E402
from engine.tests.fixtures import LINEAR, ev  # noqa: E402

WIDTH = 78


def rule(char="-"):
    print(char * WIDTH)


def scene(number, title):
    print()
    rule("=")
    print(f"SCENE {number}: {title}")
    rule("=")


def show_state(context, spec):
    """Print step statuses and what the engine would do next."""
    cells = []
    for step in spec["steps"]:
        sid = step["id"]
        status = context["steps"].get(sid, {}).get("status", "PENDING")
        cells.append(f"{sid}={status}")
    print("    state : " + "  ".join(cells))

    nxt = ready_steps(spec, context)
    print("    ready : " + (", ".join(nxt) if nxt else "(nothing)"))
    print("    run   : " + next_run_state(spec, context))


# ---------------------------------------------------------------------------

print()
rule("=")
print("CASCADE WALKTHROUGH 1 - REPLAY")
rule("=")
print()
print("The workflow under test:", LINEAR["name"])
validate_spec(LINEAR)
print("  spec validates OK")
print("  execution order:", " -> ".join(topological_order(LINEAR)))
print()
print("Steps that declare a compensating action (can be rolled back):")
for step in LINEAR["steps"]:
    mark = "yes" if step.get("compensate") else "no"
    print(f"    {step['id']:<12} compensate: {mark}")


# ---------------------------------------------------------------------------
scene(1, "The happy path - state accumulating event by event")

log = [
    ev(1, "RUN_STARTED", input={"node": "node-3"}),
    ev(2, "STEP_SCHEDULED", "preflight"),
    ev(3, "STEP_STARTED", "preflight", attempt=1),
    ev(4, "STEP_SUCCEEDED", "preflight", output={"healthy": True}),
    ev(5, "STEP_SCHEDULED", "drain"),
    ev(6, "STEP_STARTED", "drain", attempt=1),
    ev(7, "STEP_SUCCEEDED", "drain", output={"drained": True}),
]

context = new_context()
for event in log:
    print()
    print(f"  [{event['seq']:>2}] {event['type']:<22} {event['step_id']}")
    apply_event(context, event)
    show_state(context, LINEAR)

print()
print("  Nothing above consulted a clock, a network, or a database.")
print("  The context is a pure function of the events. That is the whole trick.")


# ---------------------------------------------------------------------------
scene(2, "Crash and resume - a different worker, zero handover")

print()
print("  Worker A was executing 'upgrade' when the machine lost power.")
print("  It told nobody. It handed nothing over. It simply stopped.")
print()
print("  Worker B - different process, different host - reads the log:")
print()

for event in log:
    print(f"    [{event['seq']:>2}] {event['type']:<22} {event['step_id']}")

resumed = replay(log)
print()
print("  and reconstructs, from those rows alone:")
print()
print(f"    input it was started with : {resumed['input']}")
print(f"    preflight output          : {resumed['steps']['preflight']['output']}")
print(f"    drain output              : {resumed['steps']['drain']['output']}")
print(f"    highest applied seq       : {resumed['last_seq']}")
print(f"    next step to run          : {ready_steps(LINEAR, resumed)}")
print()
print("  Identical to what Worker A knew. No handover was needed, because")
print("  Worker A never held anything the log did not already contain.")

shuffled = list(reversed(log))
assert replay(shuffled) == resumed
print()
print("  (Replaying the same events in reverse order gives an identical")
print("   result - the fold sorts by seq, so row ordering never matters.)")


# ---------------------------------------------------------------------------
scene(3, "Failure and rollback - unwinding in reverse")

failure_log = log + [
    ev(8, "STEP_STARTED", "upgrade", attempt=1),
    ev(9, "STEP_SUCCEEDED", "upgrade", output={"version": "7.1"}),
    ev(10, "STEP_STARTED", "verify", attempt=1),
    ev(11, "STEP_FAILED", "verify", error="node-3 reports unhealthy after upgrade"),
    ev(12, "COMPENSATION_STARTED", error="node-3 reports unhealthy after upgrade"),
]

state = replay(failure_log)

print()
print("  Steps completed, in the order they actually finished:")
for i, sid in enumerate(state["completion_order"], 1):
    print(f"    {i}. {sid}")

print()
print("  'verify' failed. Its on_error is 'compensate', so we unwind.")
print()
print("  Rollback order (reverse of completion, skipping steps with no")
print("  compensating action defined):")
for i, sid in enumerate(compensation_order(LINEAR, state), 1):
    action = next(s for s in LINEAR["steps"] if s["id"] == sid)["compensate"]
    print(f"    {i}. {sid:<12} -> {action['type']} {action.get('config', {})}")

print()
print(f"  run state: {next_run_state(LINEAR, state)}")
print()
print("  'preflight' is absent from the rollback: it succeeded, but it")
print("  declares no compensating action, so there is nothing to undo.")
print("  Reverse *completion* order matters rather than reverse spec order -")
print("  with parallel branches, finish order is the only safe unwind order.")

print()
rule("=")
print("Next: Block 2 wraps a worker loop around exactly these functions.")
rule("=")
print()
