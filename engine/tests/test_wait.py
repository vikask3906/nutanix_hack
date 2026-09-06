import unittest
from datetime import datetime, timedelta, timezone

from engine.replay import (
    RunState,
    StepStatus,
    next_run_state,
    ready_steps,
    replay,
    running_steps,
    waiting_steps,
)
from engine.spec import WorkflowSpecError, validate_spec
from engine.steps.base import StepValidationError
from engine.steps.builtin import WaitPlugin
from engine.tests.fixtures import ev

SPEC = {
    "name": "with_a_wait",
    "steps": [
        {"id": "before", "type": "noop"},
        {"id": "settle", "type": "wait", "needs": ["before"], "config": {"duration_s": 45}},
        {"id": "after", "type": "noop", "needs": ["settle"]},
    ],
}


class WaitSpecTests(unittest.TestCase):
    def test_a_wait_step_is_a_valid_spec(self):
        validate_spec(SPEC)

    def test_wait_requires_a_duration_or_an_until(self):
        with self.assertRaisesRegex(StepValidationError, "duration_s"):
            WaitPlugin().validate({})

    def test_negative_durations_are_rejected(self):
        with self.assertRaisesRegex(StepValidationError, "non-negative"):
            WaitPlugin().validate({"duration_s": -5})

    def test_until_must_be_a_timestamp(self):
        with self.assertRaisesRegex(StepValidationError, "ISO 8601"):
            WaitPlugin().validate({"until": "next tuesday"})


class ResumeAtTests(unittest.TestCase):
    def test_duration_is_measured_from_now(self):
        before = datetime.now(timezone.utc)
        resume = WaitPlugin().resume_at({"duration_s": 60}, {})
        delta = resume - before
        self.assertGreaterEqual(delta, timedelta(seconds=59))
        self.assertLessEqual(delta, timedelta(seconds=61))

    def test_until_is_used_verbatim(self):
        resume = WaitPlugin().resume_at({"until": "2026-09-08T09:00:00Z"}, {})
        self.assertEqual(
            resume, datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
        )

    def test_naive_timestamps_are_treated_as_utc(self):
        # Adopting the worker's local clock here would make the same workflow
        # resume at different moments depending on which host ran it.
        resume = WaitPlugin().resume_at({"until": "2026-09-08T09:00:00"}, {})
        self.assertEqual(resume.tzinfo, timezone.utc)

    def test_the_plugin_declares_that_it_defers(self):
        self.assertTrue(WaitPlugin().defers)


class WaitReplayTests(unittest.TestCase):
    """A wait is two visits to the same step, and which visit we are on is
    read from the log rather than held anywhere."""

    def test_timer_set_parks_the_step(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "before", output={}),
                ev(3, "TIMER_SET", "settle", resume_at="2026-09-08T09:00:00+00:00"),
            ]
        )
        self.assertEqual(ctx["steps"]["settle"]["status"], StepStatus.WAITING)
        self.assertEqual(waiting_steps(SPEC, ctx), ["settle"])

    def test_a_parked_step_is_not_offered_again(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "before", output={}),
                ev(3, "TIMER_SET", "settle", resume_at="2026-09-08T09:00:00+00:00"),
            ]
        )
        # It is claimed by a future task row, not by readiness.
        self.assertEqual(ready_steps(SPEC, ctx), [])

    def test_downstream_is_blocked_while_waiting(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "before", output={}),
                ev(3, "TIMER_SET", "settle"),
            ]
        )
        self.assertNotIn("after", ready_steps(SPEC, ctx))

    def test_timer_fired_completes_the_step_and_unblocks_downstream(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "before", output={}),
                ev(3, "TIMER_SET", "settle"),
                ev(4, "TIMER_FIRED", "settle", output={"waited": True}),
            ]
        )
        self.assertEqual(ctx["steps"]["settle"]["status"], StepStatus.SUCCEEDED)
        self.assertEqual(ready_steps(SPEC, ctx), ["after"])
        self.assertIn("settle", ctx["completion_order"])

    def test_a_wait_survives_replay_from_scratch(self):
        # The point of the whole design: the parked state is reconstructed from
        # the log alone, so restarting every process loses nothing.
        log = [
            ev(1, "RUN_STARTED"),
            ev(2, "STEP_SUCCEEDED", "before", output={}),
            ev(3, "TIMER_SET", "settle", resume_at="2026-09-08T09:00:00+00:00"),
        ]
        self.assertEqual(replay(log), replay(list(reversed(log))))


class WaitRunStateTests(unittest.TestCase):
    def test_run_reports_waiting_while_parked(self):
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "before", output={}),
                ev(3, "TIMER_SET", "settle"),
            ]
        )
        self.assertEqual(next_run_state(SPEC, ctx), RunState.WAITING)

    def test_waiting_is_not_terminal(self):
        ctx = replay([ev(1, "RUN_STARTED"), ev(2, "TIMER_SET", "settle")])
        self.assertNotIn(
            next_run_state(SPEC, ctx),
            {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED},
        )

    def test_a_branch_still_executing_keeps_the_run_running(self):
        """One branch parked while another works is RUNNING, not WAITING."""
        parallel = {
            "name": "parallel_with_wait",
            "steps": [
                {"id": "start", "type": "noop"},
                {"id": "pause", "type": "wait", "needs": ["start"],
                 "config": {"duration_s": 10}},
                {"id": "work", "type": "noop", "needs": ["start"]},
            ],
        }
        ctx = replay(
            [
                ev(1, "RUN_STARTED"),
                ev(2, "STEP_SUCCEEDED", "start", output={}),
                ev(3, "TIMER_SET", "pause"),
                ev(4, "STEP_STARTED", "work", attempt=1),
            ]
        )
        self.assertEqual(waiting_steps(parallel, ctx), ["pause"])
        self.assertEqual(running_steps(parallel, ctx), ["work"])
        self.assertEqual(next_run_state(parallel, ctx), RunState.RUNNING)


if __name__ == "__main__":
    unittest.main()
