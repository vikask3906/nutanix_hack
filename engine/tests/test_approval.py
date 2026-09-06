import unittest

from engine.replay import (
    RunState,
    StepStatus,
    next_run_state,
    pending_approvals,
    ready_steps,
    replay,
)
from engine.spec import validate_spec
from engine.steps.base import StepError, StepValidationError
from engine.steps.builtin import ApprovalPlugin
from engine.tests.fixtures import ev

SPEC = {
    "name": "needs_sign_off",
    "steps": [
        {"id": "prepare", "type": "noop"},
        {
            "id": "sign_off",
            "type": "approval",
            "needs": ["prepare"],
            "config": {"prompt": "Approve?", "approvers": ["ops"]},
            "on_error": "compensate",
        },
        {
            "id": "apply",
            "type": "noop",
            "needs": ["sign_off"],
            "compensate": {"type": "noop"},
        },
    ],
}

REQUESTED = [
    ev(1, "RUN_STARTED"),
    ev(2, "STEP_SUCCEEDED", "prepare", output={}),
    ev(3, "APPROVAL_REQUESTED", "sign_off", prompt="Approve?", approvers=["ops"]),
]


class ApprovalSpecTests(unittest.TestCase):
    def test_an_approval_step_is_a_valid_spec(self):
        validate_spec(SPEC)

    def test_prompt_is_required(self):
        # Without it, whoever is paged has no idea what they are approving.
        with self.assertRaisesRegex(StepValidationError, "prompt"):
            ApprovalPlugin().validate({})

    def test_timeout_must_be_positive(self):
        with self.assertRaisesRegex(StepValidationError, "positive"):
            ApprovalPlugin().validate({"prompt": "x", "timeout_s": 0})

    def test_approvers_must_be_a_list(self):
        with self.assertRaisesRegex(StepValidationError, "list"):
            ApprovalPlugin().validate({"prompt": "x", "approvers": "ops"})


class ApprovalDeferralTests(unittest.TestCase):
    def test_no_deadline_means_no_scheduled_wake_up(self):
        """The step costs a row in the log and nothing else.

        resume_at of None means nothing is queued at all - no timer, no poll.
        A workflow can sit here for a week without occupying a worker.
        """
        self.assertIsNone(ApprovalPlugin().resume_at({"prompt": "x"}, {}, "sign_off"))

    def test_a_timeout_arms_a_deadline(self):
        resume = ApprovalPlugin().resume_at(
            {"prompt": "x", "timeout_s": 3600}, {}, "sign_off"
        )
        self.assertIsNotNone(resume)

    def test_defers_on_first_visit_only(self):
        plugin = ApprovalPlugin()
        fresh = replay([ev(1, "RUN_STARTED")])
        self.assertTrue(plugin.should_defer({"prompt": "x"}, fresh, "sign_off"))

        requested = replay(REQUESTED)
        self.assertFalse(plugin.should_defer({"prompt": "x"}, requested, "sign_off"))

    def test_does_not_re_park_a_step_a_person_already_approved(self):
        approved = replay(REQUESTED + [ev(4, "APPROVAL_GRANTED", "sign_off", actor="asha")])
        self.assertFalse(
            ApprovalPlugin().should_defer({"prompt": "x"}, approved, "sign_off")
        )


class ApprovalReplayTests(unittest.TestCase):
    def test_request_parks_the_step_and_blocks_downstream(self):
        ctx = replay(REQUESTED)
        self.assertEqual(ctx["steps"]["sign_off"]["status"], StepStatus.WAITING)
        self.assertTrue(ctx["steps"]["sign_off"]["approval_requested"])
        self.assertEqual(ready_steps(SPEC, ctx), [])
        self.assertEqual(next_run_state(SPEC, ctx), RunState.WAITING)

    def test_pending_approvals_reports_what_is_being_asked(self):
        ctx = replay(REQUESTED)
        pending = pending_approvals(SPEC, ctx)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["step_id"], "sign_off")
        self.assertEqual(pending[0]["prompt"], "Approve?")
        self.assertEqual(pending[0]["approvers"], ["ops"])

    def test_granting_records_the_decision_without_completing_the_step(self):
        """Approval is a decision, not an execution.

        The step still has to be run by a worker, so it keeps the lease,
        heartbeat, fencing and retry every other step gets.
        """
        ctx = replay(REQUESTED + [ev(4, "APPROVAL_GRANTED", "sign_off", actor="asha")])
        self.assertTrue(ctx["steps"]["sign_off"]["approved"])
        self.assertEqual(ctx["steps"]["sign_off"]["approved_by"], "asha")
        self.assertEqual(ctx["steps"]["sign_off"]["status"], StepStatus.WAITING)
        # Downstream is still blocked until the step actually succeeds.
        self.assertNotIn("apply", ready_steps(SPEC, ctx))

    def test_the_step_completes_normally_after_approval(self):
        ctx = replay(
            REQUESTED
            + [
                ev(4, "APPROVAL_GRANTED", "sign_off", actor="asha"),
                ev(5, "STEP_STARTED", "sign_off", attempt=2),
                ev(6, "STEP_SUCCEEDED", "sign_off", output={"approved": True}),
            ]
        )
        self.assertEqual(ctx["steps"]["sign_off"]["status"], StepStatus.SUCCEEDED)
        self.assertEqual(ready_steps(SPEC, ctx), ["apply"])

    def test_denial_fails_the_step(self):
        ctx = replay(
            REQUESTED
            + [ev(4, "APPROVAL_DENIED", "sign_off", actor="rahul", comment="too risky")]
        )
        self.assertEqual(ctx["steps"]["sign_off"]["status"], StepStatus.FAILED)
        self.assertIn("rahul", ctx["steps"]["sign_off"]["error"])
        self.assertEqual(next_run_state(SPEC, ctx), RunState.FAILED)
        self.assertEqual(pending_approvals(SPEC, ctx), [])

    def test_a_denied_approval_still_replays_deterministically(self):
        log = REQUESTED + [ev(4, "APPROVAL_DENIED", "sign_off", actor="rahul")]
        self.assertEqual(replay(log), replay(list(reversed(log))))


class ApprovalExecuteTests(unittest.TestCase):
    def test_returns_who_approved(self):
        ctx = replay(
            REQUESTED
            + [ev(4, "APPROVAL_GRANTED", "sign_off", actor="asha", comment="ship it")]
        )
        output = ApprovalPlugin().execute({"prompt": "x"}, ctx, "key", "sign_off")
        self.assertTrue(output["approved"])
        self.assertEqual(output["approved_by"], "asha")
        self.assertEqual(output["comment"], "ship it")

    def test_running_without_an_approval_is_a_timeout(self):
        # Reached only when a deadline woke the step with no decision recorded.
        ctx = replay(REQUESTED)
        with self.assertRaisesRegex(StepError, "timed out"):
            ApprovalPlugin().execute(
                {"prompt": "x", "timeout_s": 60}, ctx, "key", "sign_off"
            )


if __name__ == "__main__":
    unittest.main()
