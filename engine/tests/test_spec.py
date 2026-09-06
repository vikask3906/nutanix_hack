import copy
import unittest

from engine.spec import (
    WorkflowSpecError,
    detect_cycle,
    needs_of,
    topological_order,
    validate_spec,
)
from engine.tests.fixtures import DIAMOND, LINEAR


class ValidateSpecTests(unittest.TestCase):
    def test_valid_specs_pass(self):
        validate_spec(LINEAR)
        validate_spec(DIAMOND)

    def test_rejects_non_object(self):
        with self.assertRaises(WorkflowSpecError):
            validate_spec([])

    def test_requires_name(self):
        spec = copy.deepcopy(LINEAR)
        del spec["name"]
        with self.assertRaisesRegex(WorkflowSpecError, "name"):
            validate_spec(spec)

    def test_requires_non_empty_steps(self):
        with self.assertRaisesRegex(WorkflowSpecError, "steps"):
            validate_spec({"name": "x", "steps": []})

    def test_rejects_duplicate_step_ids(self):
        spec = copy.deepcopy(LINEAR)
        spec["steps"].append({"id": "drain", "type": "noop"})
        with self.assertRaisesRegex(WorkflowSpecError, "duplicate step id 'drain'"):
            validate_spec(spec)

    def test_rejects_unknown_step_type(self):
        spec = copy.deepcopy(LINEAR)
        spec["steps"][0]["type"] = "teleport"
        with self.assertRaisesRegex(WorkflowSpecError, "unknown type"):
            validate_spec(spec)

    def test_rejects_dangling_dependency(self):
        spec = copy.deepcopy(LINEAR)
        spec["steps"][1]["needs"] = ["does_not_exist"]
        with self.assertRaisesRegex(WorkflowSpecError, "not a step in this workflow"):
            validate_spec(spec)

    def test_rejects_bad_on_error(self):
        spec = copy.deepcopy(LINEAR)
        spec["steps"][0]["on_error"] = "explode"
        with self.assertRaisesRegex(WorkflowSpecError, "on_error"):
            validate_spec(spec)

    def test_rejects_bad_retry_max(self):
        spec = copy.deepcopy(LINEAR)
        spec["steps"][0]["retry"]["max"] = 0
        with self.assertRaisesRegex(WorkflowSpecError, "retry.max"):
            validate_spec(spec)

    def test_rejects_bad_compensate_type(self):
        spec = copy.deepcopy(LINEAR)
        spec["steps"][1]["compensate"] = {"type": "carrier_pigeon"}
        with self.assertRaisesRegex(WorkflowSpecError, "compensate.type"):
            validate_spec(spec)


class CycleDetectionTests(unittest.TestCase):
    def test_accepts_acyclic_graphs(self):
        detect_cycle(LINEAR)
        detect_cycle(DIAMOND)

    def test_detects_two_step_cycle_and_names_it(self):
        spec = {
            "name": "loop",
            "steps": [
                {"id": "a", "type": "noop", "needs": ["b"]},
                {"id": "b", "type": "noop", "needs": ["a"]},
            ],
        }
        with self.assertRaises(WorkflowSpecError) as ctx:
            validate_spec(spec)
        message = str(ctx.exception)
        self.assertIn("dependency cycle", message)
        # The message must name the steps involved - "cycle detected" alone is
        # useless when debugging a forty-step workflow.
        self.assertIn("a", message)
        self.assertIn("b", message)

    def test_detects_self_dependency(self):
        spec = {"name": "self", "steps": [{"id": "a", "type": "noop", "needs": ["a"]}]}
        with self.assertRaisesRegex(WorkflowSpecError, "dependency cycle"):
            validate_spec(spec)

    def test_detects_longer_cycle(self):
        spec = {
            "name": "long",
            "steps": [
                {"id": "a", "type": "noop", "needs": ["c"]},
                {"id": "b", "type": "noop", "needs": ["a"]},
                {"id": "c", "type": "noop", "needs": ["b"]},
            ],
        }
        with self.assertRaisesRegex(WorkflowSpecError, "dependency cycle"):
            validate_spec(spec)


class TopologicalOrderTests(unittest.TestCase):
    def test_linear_order(self):
        self.assertEqual(
            topological_order(LINEAR), ["preflight", "drain", "upgrade", "verify"]
        )

    def test_diamond_respects_dependencies(self):
        order = topological_order(DIAMOND)
        self.assertEqual(order[0], "start")
        self.assertEqual(order[-1], "join")
        self.assertLess(order.index("branch_a"), order.index("join"))
        self.assertLess(order.index("branch_b"), order.index("join"))

    def test_is_deterministic(self):
        self.assertEqual(topological_order(DIAMOND), topological_order(DIAMOND))


class NeedsOfTests(unittest.TestCase):
    def test_missing_needs_is_empty_list(self):
        self.assertEqual(needs_of({"id": "a", "type": "noop"}), [])

    def test_string_needs_is_wrapped(self):
        self.assertEqual(needs_of({"id": "b", "needs": "a"}), ["a"])

    def test_list_needs_passes_through(self):
        self.assertEqual(needs_of({"id": "c", "needs": ["a", "b"]}), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
