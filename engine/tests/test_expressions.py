import unittest

from engine.expressions import (
    ExpressionError,
    lookup,
    referenced_steps,
    render,
    render_string,
)

CONTEXT = {
    "input": {"node": "node-3", "count": 3, "flags": {"force": True}},
    "steps": {
        "preflight": {"output": {"healthy": True, "load": 0.42}},
        "upgrade": {"output": {"version": "7.1", "hosts": ["h1", "h2"]}},
    },
}


class LookupTests(unittest.TestCase):
    def test_simple_path(self):
        self.assertEqual(lookup("input.node", CONTEXT), "node-3")

    def test_nested_path(self):
        self.assertEqual(lookup("input.flags.force", CONTEXT), True)

    def test_step_output(self):
        self.assertEqual(lookup("steps.upgrade.output.version", CONTEXT), "7.1")

    def test_list_index(self):
        self.assertEqual(lookup("steps.upgrade.output.hosts.1", CONTEXT), "h2")

    def test_missing_key_raises_with_a_useful_message(self):
        # A silent None here becomes a request to http://None/health twenty
        # minutes into a workflow. Fail loudly instead.
        with self.assertRaises(ExpressionError) as ctx:
            lookup("input.missing", CONTEXT)
        self.assertIn("input.missing", str(ctx.exception))
        self.assertIn("missing", str(ctx.exception))

    def test_walking_into_a_scalar_raises(self):
        with self.assertRaisesRegex(ExpressionError, "not an object"):
            lookup("input.node.deeper", CONTEXT)


class RenderStringTests(unittest.TestCase):
    def test_whole_expression_preserves_type(self):
        self.assertEqual(render_string("{{ input.count }}", CONTEXT), 3)
        self.assertIs(render_string("{{ input.flags.force }}", CONTEXT), True)
        self.assertEqual(
            render_string("{{ steps.upgrade.output.hosts }}", CONTEXT), ["h1", "h2"]
        )

    def test_embedded_expression_interpolates_as_string(self):
        self.assertEqual(
            render_string("http://{{ input.node }}/health", CONTEXT),
            "http://node-3/health",
        )

    def test_multiple_expressions_in_one_string(self):
        self.assertEqual(
            render_string("{{ input.node }} v{{ steps.upgrade.output.version }}", CONTEXT),
            "node-3 v7.1",
        )

    def test_whitespace_inside_braces_is_tolerated(self):
        self.assertEqual(render_string("{{input.node}}", CONTEXT), "node-3")
        self.assertEqual(render_string("{{    input.node    }}", CONTEXT), "node-3")

    def test_plain_string_is_untouched(self):
        self.assertEqual(render_string("no templates here", CONTEXT), "no templates here")


class RenderTests(unittest.TestCase):
    def test_renders_nested_structures(self):
        config = {
            "method": "POST",
            "url": "http://{{ input.node }}/upgrade",
            "body": {
                "version": "{{ steps.upgrade.output.version }}",
                "count": "{{ input.count }}",
                "tags": ["{{ input.node }}", "static"],
            },
        }
        self.assertEqual(
            render(config, CONTEXT),
            {
                "method": "POST",
                "url": "http://node-3/upgrade",
                "body": {
                    "version": "7.1",
                    "count": 3,  # type preserved, not "3"
                    "tags": ["node-3", "static"],
                },
            },
        )

    def test_non_strings_pass_through(self):
        self.assertEqual(render({"a": 1, "b": None, "c": True}, CONTEXT),
                         {"a": 1, "b": None, "c": True})


class ReferencedStepsTests(unittest.TestCase):
    def test_finds_step_dependencies_in_a_config(self):
        config = {
            "url": "http://{{ input.node }}/x",
            "body": {"v": "{{ steps.upgrade.output.version }}",
                     "h": "{{ steps.preflight.output.healthy }}"},
        }
        self.assertEqual(referenced_steps(config), {"upgrade", "preflight"})

    def test_input_references_are_not_step_references(self):
        self.assertEqual(referenced_steps({"a": "{{ input.node }}"}), set())


if __name__ == "__main__":
    unittest.main()
