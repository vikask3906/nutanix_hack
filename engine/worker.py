"""The worker loop.

    claim a task -> replay the log -> execute one step -> record the result
                 -> enqueue whatever is now ready -> repeat

That is the entire engine at runtime. Everything clever already happened in
replay.py; this module is the plumbing that connects it to a database and to the
outside world.

The worker is deliberately stateless. Between two iterations it remembers
nothing. Kill it at any point and the only consequence is a lease that expires.
"""

import logging

from django.db import transaction

from engine import retry
from engine.expressions import ExpressionError, render
from engine.heartbeat import LeaseHeartbeat
from engine.models import EventType, TaskKind
from engine.replay import StepStatus
from engine.service import (
    advance_run,
    append_event,
    claim_task,
    enqueue_step,
    finish_task,
    finish_task_if_owned,
    load_context,
    schedule_retry,
    start_compensation,
    sync_run_projection,
)
from engine.spec import steps_by_id
from engine.steps import StepError, get_plugin

log = logging.getLogger("cascade.worker")


def run_once(worker_id):
    """Claim and process a single task. Returns True if there was work."""
    task = claim_task(worker_id)
    if task is None:
        return False

    execute_task(task, worker_id)
    return True


def execute_task(task, worker_id):
    run = task.run
    spec = run.definition.spec
    step = steps_by_id(spec).get(task.step_id)

    if step is None:
        # Definitions are immutable and validated, so this should be
        # unreachable. Fail loudly rather than silently dropping the task.
        _fail_terminally(
            task, run, spec, f"step {task.step_id!r} is not in this workflow definition"
        )
        return

    context = load_context(run)
    compensating = task.kind == TaskKind.COMPENSATE

    log.info(
        "[%s] %s %-14s run=%s attempt=%s",
        _short(worker_id),
        "UNDO  " if compensating else "claim ",
        task.step_id,
        _short(str(run.id)),
        task.attempt,
    )

    if compensating:
        _run_compensation(task, run, spec, step, context, worker_id)
        return

    # A deferring step (wait, and later approval) does no work on first reach.
    # It records a timer, queues itself for later, and frees this worker now.
    plugin = _plugin_for(step)
    if plugin is not None and plugin.defers:
        try:
            config = render(step.get("config", {}), context)
        except ExpressionError as exc:
            _record_terminal_failure(task, run, spec, str(exc))
            return

        if plugin.should_defer(config, context, task.step_id):
            _handle_deferral(task, run, spec, step, plugin, config, context, worker_id)
            return

        # Whatever it was waiting for has happened. Mark the wait as over and
        # fall through - the step now runs like any other, with the same lease,
        # heartbeat, fencing and retry as everything else.
        if step["type"] == "wait":
            append_event(run, EventType.TIMER_FIRED, task.step_id, {})
            context = load_context(run)

    append_event(run, EventType.STEP_STARTED, task.step_id, {"attempt": task.attempt})

    # ---- execute OUTSIDE any transaction ---------------------------------
    # Slow: subprocesses, network calls, arbitrary latency. Holding a row lock
    # across this is how you deadlock a worker pool. The heartbeat keeps our
    # lease alive for as long as we are genuinely working.
    with LeaseHeartbeat(task, worker_id) as beat:
        ok, result = _execute_step(step, context, task.idempotency_key, task.step_id)
    # ----------------------------------------------------------------------

    # Fencing. If our lease expired mid-step the reaper has already handed this
    # work to someone else. Recording our result now would write a second,
    # stale outcome for a step another worker may have already completed.
    if beat.lost or not finish_task_if_owned(task, worker_id):
        log.warning(
            "[%s] ABANDON %-13s lease lost mid-step; discarding result",
            _short(worker_id), task.step_id,
        )
        return

    if ok:
        log.info("[%s] done   %-14s", _short(worker_id), task.step_id)
        _record_success(task, run, spec, result)
        return

    error = result.get("error", "")

    if retry.should_retry(step, task.attempt):
        log.warning(
            "[%s] retry  %-14s attempt %s/%s: %s",
            _short(worker_id), task.step_id, task.attempt,
            retry.max_attempts(step), error,
        )
        with transaction.atomic():
            schedule_retry(run, step, task, error)
            sync_run_projection(run, spec, load_context(run))
        return

    log.warning("[%s] FAILED %-14s %s", _short(worker_id), task.step_id, error)
    _record_terminal_failure(task, run, spec, error, result.get("details"))


def _plugin_for(step):
    try:
        return get_plugin(step["type"])
    except StepError:
        return None


def _handle_deferral(task, run, spec, step, plugin, config, context, worker_id):
    """Park a step: record why it is waiting, queue its wake-up, free the worker."""
    if not finish_task_if_owned(task, worker_id):
        log.warning("[%s] ABANDON %-13s lease lost", _short(worker_id), task.step_id)
        return

    resume_at = plugin.resume_at(config, context, task.step_id)
    is_approval = step["type"] == "approval"

    log.info(
        "[%s] park   %-14s %s",
        _short(worker_id), task.step_id,
        f"awaiting approval, deadline {resume_at.isoformat()}" if is_approval and resume_at
        else "awaiting approval, no deadline" if is_approval
        else f"until {resume_at.isoformat()}",
    )

    with transaction.atomic():
        if is_approval:
            append_event(
                run,
                EventType.APPROVAL_REQUESTED,
                task.step_id,
                {
                    "prompt": config.get("prompt", ""),
                    "approvers": config.get("approvers", []),
                    "deadline": resume_at.isoformat() if resume_at else None,
                },
            )
        else:
            append_event(
                run,
                EventType.TIMER_SET,
                task.step_id,
                {"resume_at": resume_at.isoformat() if resume_at else None},
            )

        if resume_at is not None:
            # The wait IS this row. Nothing is held in memory, so it survives
            # the worker dying, the stack being redeployed, or a restore.
            #
            # An approval with no deadline queues nothing at all: it costs a
            # row in the log and not one byte more until a person decides.
            enqueue_step(
                run,
                task.step_id,
                attempt=task.attempt + 1,
                run_after=resume_at,
            )
        sync_run_projection(run, spec, load_context(run))


def _run_compensation(task, run, spec, step, context, worker_id):
    """Undo one completed step by running its ``compensate`` block."""
    comp = step.get("compensate") or {}

    with LeaseHeartbeat(task, worker_id) as beat:
        ok, result = _execute_step(
            {"type": comp.get("type", "noop"), "config": comp.get("config", {})},
            context,
            task.idempotency_key,
            task.step_id,
        )

    if beat.lost or not finish_task_if_owned(task, worker_id):
        log.warning(
            "[%s] ABANDON %-13s compensation lease lost", _short(worker_id), task.step_id
        )
        return

    if ok:
        log.info("[%s] undone %-14s", _short(worker_id), task.step_id)
        with transaction.atomic():
            append_event(
                run, EventType.STEP_COMPENSATED, task.step_id, {"output": result}
            )
            advance_run(run, spec)
        return

    error = result.get("error", "")

    # Rollback can be retried like anything else - a transient failure while
    # undoing is no different from a transient failure while doing.
    if retry.should_retry(comp, task.attempt):
        log.warning(
            "[%s] retry undo %-9s attempt %s/%s: %s",
            _short(worker_id), task.step_id, task.attempt,
            retry.max_attempts(comp), error,
        )
        with transaction.atomic():
            schedule_retry(run, comp, task, error)
            sync_run_projection(run, spec, load_context(run))
        return

    # Rollback itself has failed for good. This is the genuinely bad case: the
    # system is now partially unwound and no automated action can fix it. Say so
    # loudly, stop trying, and leave the log as the record of exactly how far
    # the unwind got.
    log.error(
        "[%s] UNDO FAILED %-8s %s - manual intervention required",
        _short(worker_id), task.step_id, error,
    )
    with transaction.atomic():
        append_event(
            run,
            EventType.COMPENSATION_FAILED,
            task.step_id,
            {
                "error": f"could not undo {task.step_id}: {error}",
                "attempts": task.attempt,
            },
        )
        advance_run(run, spec)


def _execute_step(step, context, idem_key, step_id=""):
    """Run one step. Returns (ok, result). Never raises."""
    try:
        config = render(step.get("config", {}), context)
    except ExpressionError as exc:
        return False, {"error": str(exc)}

    try:
        plugin = get_plugin(step["type"])
        output = plugin.execute(config, context, idem_key, step_id)
        return True, output
    except StepError as exc:
        return False, {"error": str(exc), "details": exc.details}
    except Exception as exc:  # noqa: BLE001 - a plugin bug must not kill the worker
        log.exception("plugin %r raised an unexpected error", step.get("type"))
        return False, {"error": f"{type(exc).__name__}: {exc}"}


def _record_success(task, run, spec, output):
    with transaction.atomic():
        append_event(run, EventType.STEP_SUCCEEDED, task.step_id, {"output": output})
        advance_run(run, spec)


def _record_terminal_failure(task, run, spec, error, details=None):
    step = steps_by_id(spec).get(task.step_id, {})

    with transaction.atomic():
        append_event(
            run,
            EventType.STEP_FAILED,
            task.step_id,
            {"error": error, "details": details or {}, "attempt": task.attempt},
        )

        if step.get("on_error") == "compensate":
            context = load_context(run)
            start_compensation(run, spec, context, task.step_id, error)

        advance_run(run, spec)


def _fail_terminally(task, run, spec, error):
    """Failure paths that bypass the retry logic entirely."""
    with transaction.atomic():
        append_event(run, EventType.STEP_FAILED, task.step_id, {"error": error})
        finish_task(task)
        advance_run(run, spec)


def _short(value):
    """Trim an identifier for readable logs."""
    return value.split(":")[-1][:8] if ":" in value else value[:8]
