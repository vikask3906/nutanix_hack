import random
import unittest

from engine.replay import (
    RunState,
    StepStatus,
    compensation_order,
    failed_steps,
    is_complete,
    new_context,
    next_run_state,
    ready_steps,
    replay,
)
from engine.tests.fixtures import DIAMOND, LINEAR, ev


class ReplayBasicsTests(unittest.TestCase):
    def test_empty_log_is_zero_state(self):
        ctx = replay([])
        self.assertEqual(ctx, new_context())
        self.assertEqual(ctx["status"], RunState.PENDING)

    def test_run_started_captures_input(self):
        ctx = replay([ev(1, "RUN_STARTED", input={"node": "node-3"})])
        self.assertEqual(ctx["input"], {"node": "node-3"})
        self.assertEqual(ctx["status"], RunState.RUNNING)

    def test_step_lifecycle(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED", input={}),
                ev(2, "STEP_SCHEDULED", "preflight"),
                ev(3, "STEP_STARTED", "preflight", attempt=1),
                ev(4, "STEP_SUCCEEDED", "preflight", output={"healthy": True}),
            ]
        )
        slot = ctx["steps"]["preflight"]
        self.assertEqual(slot["status"], StepStatus.SUCCEEDED)
        self.assertEqual(slot["output"], {"healthy": True})
        self.assertEqual(slot["attempts"], 1)
        self.assertEqual(ctx["last_seq"], 4)

    def test_failure_is_recorded_with_error(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_STARTED", "preflight", attempt=1),
                ev(3, "STEP_FAILED", "preflight", error="connection refused"),
            ]
        )
        self.assertEqual(ctx["steps"]["preflight"]["status"], StepStatus.FAILED)
        self.assertEqual(ctx["steps"]["preflight"]["error"], "connection refused")

    def test_retry_then_success_keeps_final_state(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_STARTED", "preflight", attempt=1),
                ev(3, "STEP_FAILED", "preflight", error="timeout"),
                ev(4, "STEP_RETRY_SCHEDULED", "preflight", error="timeout"),
                ev(5, "STEP_STARTED", "preflight", attempt=2),
                ev(6, "STEP_SUCCEEDED", "preflight", output={"healthy": True}),
            ]
        )
        slot = ctx["steps"]["preflight"]
        self.assertEqual(slot["status"], StepStatus.SUCCEEDED)
        self.assertEqual(slot["attempts"], 2)
        self.assertEqual(slot["error"], "")

    def test_unknown_event_types_are_ignored_not_fatal(self):
        # An old worker replaying a log written by a newer one must degrade,
        # not crash.
        ctx = replay([ev(1, "RUN_STARTED"), ev(2, "SOMETHING_FROM_THE_FUTURE", "x")])
        self.assertEqual(ctx["status"], RunState.RUNNING)


class ReplayPurityTests(unittest.TestCase):
    """The guarantees the whole crash-recovery story rests on."""

    LOG = [
        ev(1, "RUN_STARTED", input={"node": "node-3"}),
        ev(2, "STEP_SCHEDULED", "preflight"),
        ev(3, "STEP_STARTED", "preflight", attempt=1),
        ev(4, "STEP_SUCCEEDED", "preflight", output={"healthy": True}),
        ev(5, "STEP_SCHEDULED", "drain"),
        ev(6, "STEP_STARTED", "drain", attempt=1),
        ev(7, "STEP_SUCCEEDED", "drain", output={"drained": True}),
    ]

    def test_replay_is_deterministic(self):
        self.assertEqual(replay(self.LOG), replay(self.LOG))

    def test_replay_is_order_independent(self):
        shuffled = self.LOG[:]
        random.shuffle(shuffled)
        self.assertEqual(replay(shuffled), replay(self.LOG))

    def test_replay_does_not_mutate_the_log(self):
        before = [dict(e) for e in self.LOG]
        replay(self.LOG)
        self.assertEqual(self.LOG, before)


class ReadyStepsTests(unittest.TestCase):
    def test_first_step_ready_at_start(self):
        ctx = replay([ev(1, "RUN_STARTED")])
        self.assertEqual(ready_steps(LINEAR, ctx), ["preflight"])

    def test_scheduled_step_is_not_offered_again(self):
        ctx = replay([ev(1, "RUN_STARTED"), ev(2, "STEP_SCHEDULED", "preflight")])
        self.assertEqual(ready_steps(LINEAR, ctx), [])

    def test_dependent_unlocks_only_after_dependency_succeeds(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_STARTED", "preflight", attempt=1),
            ]
        )
        self.assertEqual(ready_steps(LINEAR, ctx), [])

        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_STARTED", "preflight", attempt=1),
                ev(3, "STEP_SUCCEEDED", "preflight", output={}),
            ]
        )
        self.assertEqual(ready_steps(LINEAR, ctx), ["drain"])

    def test_fan_out_offers_both_branches(self):
        ctx = replay(
            [ev(1, "RUN_STARTED"), ev(2, "STEP_SUCCEEDED", "start", output={})]
        )
        self.assertEqual(ready_steps(DIAMOND, ctx), ["branch_a", "branch_b"])

    def test_join_waits_for_every_branch(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "start", output={}),
                ev(3, "STEP_SUCCEEDED", "branch_a", output={}),
            ]
        )
        self.assertEqual(ready_steps(DIAMOND, ctx), ["branch_b"])

        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "start", output={}),
                ev(3, "STEP_SUCCEEDED", "branch_a", output={}),
                ev(4, "STEP_SUCCEEDED", "branch_b", output={}),
            ]
        )
        self.assertEqual(ready_steps(DIAMOND, ctx), ["join"])

    def test_nothing_is_ready_while_compensating(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "preflight", output={}),
                ev(3, "COMPENSATION_STARTED", error="verify failed"),
            ]
        )
        self.assertEqual(ready_steps(LINEAR, ctx), [])

    def test_nothing_is_ready_once_terminal(self):
        ctx = replay([ev(1, "RUN_STARTED"), ev(2, "RUN_FAILED", error="boom")])
        self.assertEqual(ready_steps(LINEAR, ctx), [])


class CrashRecoveryTests(unittest.TestCase):
    """The scenario the whole design exists for.

    A worker dies after recording that 'drain' succeeded but before anything
    else happens. A different worker, on a different machine, with no knowledge
    of the first, picks up the log and must reach exactly the right conclusion.
    """

    def test_a_fresh_worker_resumes_at_the_right_step(self):
        log_at_crash = [
            ev(1, "RUN_STARTED", input={"node": "node-3"}),
            ev(2, "STEP_SCHEDULED", "preflight"),
            ev(3, "STEP_STARTED", "preflight", attempt=1),
            ev(4, "STEP_SUCCEEDED", "preflight", output={"healthy": True}),
            ev(5, "STEP_SCHEDULED", "drain"),
            ev(6, "STEP_STARTED", "drain", attempt=1),
            ev(7, "STEP_SUCCEEDED", "drain", output={"drained": True}),
        ]

        ctx = replay(log_at_crash)

        # It knows what already happened...
        self.assertEqual(ctx["steps"]["preflight"]["status"], StepStatus.SUCCEEDED)
        self.assertEqual(ctx["steps"]["drain"]["output"], {"drained": True})
        # ...the input it was started with...
        self.assertEqual(ctx["input"], {"node": "node-3"})
        # ...and exactly what to do next.
        self.assertEqual(ready_steps(LINEAR, ctx), ["upgrade"])
        self.assertEqual(ctx["last_seq"], 7)

    def test_crash_mid_step_does_not_lose_earlier_work(self):
        # 'upgrade' was STARTED but never finished - the worker died holding it.
        # Until the reaper returns the task, nothing new is ready.
        log = [
            ev(1, "RUN_STARTED", input={"node": "node-3"}),
            ev(2, "STEP_SUCCEEDED", "preflight", output={}),
            ev(3, "STEP_SUCCEEDED", "drain", output={}),
            ev(4, "STEP_STARTED", "upgrade", attempt=1),
        ]
        ctx = replay(log)
        self.assertEqual(ctx["steps"]["upgrade"]["status"], StepStatus.RUNNING)
        self.assertEqual(ready_steps(LINEAR, ctx), [])
        # Earlier results survived intact.
        self.assertEqual(ctx["steps"]["drain"]["status"], StepStatus.SUCCEEDED)


class CompensationTests(unittest.TestCase):
    def test_unwinds_in_reverse_order_of_completion(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "preflight", output={}),
                ev(3, "STEP_SUCCEEDED", "drain", output={}),
                ev(4, "STEP_SUCCEEDED", "upgrade", output={}),
                ev(5, "STEP_FAILED", "verify", error="node unhealthy"),
                ev(6, "COMPENSATION_STARTED", error="node unhealthy"),
            ]
        )
        # preflight has no compensate block, so it is skipped entirely.
        self.assertEqual(compensation_order(LINEAR, ctx), ["upgrade", "drain"])

    def test_already_compensated_steps_are_not_repeated(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "drain", output={}),
                ev(3, "STEP_SUCCEEDED", "upgrade", output={}),
                ev(4, "STEP_FAILED", "verify", error="bad"),
                ev(5, "COMPENSATION_STARTED", error="bad"),
                ev(6, "STEP_COMPENSATED", "upgrade"),
            ]
        )
        self.assertEqual(compensation_order(LINEAR, ctx), ["drain"])

    def test_completion_order_follows_actual_finish_order(self):
        # branch_b finished before branch_a, so it must be undone after it.
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "start", output={}),
                ev(3, "STEP_SUCCEEDED", "branch_b", output={}),
                ev(4, "STEP_SUCCEEDED", "branch_a", output={}),
            ]
        )
        self.assertEqual(
            ctx["completion_order"], ["start", "branch_b", "branch_a"]
        )


class CompensationFailureTests(unittest.TestCase):
    """When rollback itself fails there is no automated way out."""

    def test_failed_compensation_is_not_retried_forever(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "drain", output={}),
                ev(3, "STEP_SUCCEEDED", "upgrade", output={}),
                ev(4, "STEP_FAILED", "verify", error="unhealthy"),
                ev(5, "COMPENSATION_STARTED", error="unhealthy"),
                ev(6, "COMPENSATION_FAILED", "upgrade", error="rollback endpoint down"),
            ]
        )
        # 'upgrade' is out of the queue - we could not undo it and must not
        # keep trying. 'drain' is still unwound, so we get as far as we can.
        self.assertEqual(compensation_order(LINEAR, ctx), ["drain"])

    def test_failed_compensation_flags_manual_intervention(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "drain", output={}),
                ev(3, "STEP_FAILED", "verify", error="unhealthy"),
                ev(4, "COMPENSATION_STARTED", error="unhealthy"),
                ev(5, "COMPENSATION_FAILED", "drain", error="undrain endpoint down"),
            ]
        )
        self.assertTrue(ctx["needs_manual_intervention"])
        self.assertIn("undrain endpoint down", ctx["error"])
        # Nothing left to try, so the run is terminally failed.
        self.assertEqual(compensation_order(LINEAR, ctx), [])
        self.assertEqual(next_run_state(LINEAR, ctx), RunState.FAILED)


class OnErrorContinueTests(unittest.TestCase):
    """A step marked on_error: continue records its failure without
    condemning the whole run."""

    SPEC = {
        "name": "tolerant",
        "steps": [
            {"id": "core", "type": "noop"},
            {"id": "optional", "type": "noop", "needs": ["core"], "on_error": "continue"},
            {"id": "downstream", "type": "noop", "needs": ["optional"]},
        ],
    }

    def test_continue_failure_does_not_fail_the_run(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "core", output={}),
                ev(3, "STEP_FAILED", "optional", error="best effort"),
            ]
        )
        self.assertEqual(failed_steps(self.SPEC, ctx), [])
        self.assertNotEqual(next_run_state(self.SPEC, ctx), RunState.FAILED)

    def test_the_failure_is_still_recorded(self):
        # The log stays honest even though the run survives.
        ctx = replay(
            [ev(1, "RUN_STARTED"), ev(2, "STEP_FAILED", "optional", error="best effort")]
        )
        self.assertEqual(ctx["steps"]["optional"]["status"], StepStatus.FAILED)
        self.assertEqual(ctx["steps"]["optional"]["error"], "best effort")

    def test_dependents_of_a_continued_failure_still_do_not_run(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "core", output={}),
                ev(3, "STEP_FAILED", "optional", error="best effort"),
            ]
        )
        # Readiness requires SUCCEEDED, so the branch stops without anything
        # having to explicitly cancel it.
        self.assertNotIn("downstream", ready_steps(self.SPEC, ctx))

    def test_a_normal_failure_still_fails_the_run(self):
        ctx = replay(
            [ev(1, "RUN_STARTED"), ev(2, "STEP_FAILED", "core", error="fatal")]
        )
        self.assertEqual(failed_steps(self.SPEC, ctx), ["core"])
        self.assertEqual(next_run_state(self.SPEC, ctx), RunState.FAILED)


class RunStateTests(unittest.TestCase):
    def test_running_while_work_remains(self):
        ctx = replay([ev(1, "RUN_STARTED"), ev(2, "STEP_SUCCEEDED", "preflight", output={})])
        self.assertEqual(next_run_state(LINEAR, ctx), RunState.RUNNING)

    def test_succeeded_when_every_step_is_done(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "preflight", output={}),
                ev(3, "STEP_SUCCEEDED", "drain", output={}),
                ev(4, "STEP_SUCCEEDED", "upgrade", output={}),
                ev(5, "STEP_SUCCEEDED", "verify", output={}),
            ]
        )
        self.assertTrue(is_complete(LINEAR, ctx))
        self.assertEqual(next_run_state(LINEAR, ctx), RunState.SUCCEEDED)

    def test_failed_when_a_step_terminally_failed(self):
        ctx = replay(
            [ev(1, "RUN_STARTED"), ev(2, "STEP_FAILED", "preflight", error="nope")]
        )
        self.assertEqual(failed_steps(LINEAR, ctx), ["preflight"])
        self.assertEqual(next_run_state(LINEAR, ctx), RunState.FAILED)

    def test_compensating_until_nothing_is_left_to_unwind(self):
        base = [
            ev(1, "RUN_STARTED"),
            ev(2, "STEP_SUCCEEDED", "drain", output={}),
            ev(3, "STEP_FAILED", "verify", error="bad"),
            ev(4, "COMPENSATION_STARTED", error="bad"),
        ]
        self.assertEqual(next_run_state(LINEAR, replay(base)), RunState.COMPENSATING)

        done = base + [ev(5, "STEP_COMPENSATED", "drain")]
        self.assertEqual(next_run_state(LINEAR, replay(done)), RunState.FAILED)


if __name__ == "__main__":
    unittest.main()
