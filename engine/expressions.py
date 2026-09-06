"""Template expressions in step configs. Pure Python - no Django.

A step config can reference the run's input and earlier steps' outputs::

    {"url": "http://{{ input.node }}/health"}
    {"body": {"version": "{{ steps.upgrade.output.version }}"}}

Deliberately NOT a language. No loops, no conditionals, no function calls, no
arithmetic - just dotted lookups into the context. Every expression language that
starts small grows into a badly-specified programming language that runs inside
your orchestrator, and then you own a compiler you never meant to write. If a
workflow needs real logic, that belongs in a shell step where it is testable.

One useful subtlety: a config value that is EXACTLY one expression keeps its type.

    "{{ input.count }}"        -> 3        (an int)
    "node-{{ input.count }}"   -> "node-3" (a string)

Without that, every value crossing a template boundary silently becomes a string,
and a step that expects a number gets "3" instead of 3.
"""

import re

# Matches {{ path.to.value }} with flexible internal whitespace.
EXPRESSION = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.\-]*)\s*\}\}")

# The same, but only when it is the entire string (so we can preserve the type).
WHOLE_EXPRESSION = re.compile(r"^\s*\{\{\s*([a-zA-Z_][a-zA-Z0-9_.\-]*)\s*\}\}\s*$")


class ExpressionError(ValueError):
    """A template referenced something that does not exist in the context."""


def lookup(path, context):
    """Resolve a dotted path against the context.

    ``steps.preflight.output.healthy`` walks
    ``context["steps"]["preflight"]["output"]["healthy"]``.

    Raises ExpressionError rather than returning None. A silent None here becomes
    a request to ``http://None/health`` twenty minutes into a workflow, and you
    get to work out why from the access logs.
    """
    current = context
    walked = []

    for part in path.split("."):
        walked.append(part)

        if isinstance(current, dict):
            if part not in current:
                raise ExpressionError(
                    f"{{{{ {path} }}}} could not be resolved: "
                    f"no key {part!r} at {'.'.join(walked[:-1]) or '<root>'}"
                )
            current = current[part]

        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ExpressionError(
                    f"{{{{ {path} }}}} could not be resolved: "
                    f"bad list index {part!r}"
                ) from exc

        else:
            raise ExpressionError(
                f"{{{{ {path} }}}} could not be resolved: "
                f"{'.'.join(walked[:-1])} is a {type(current).__name__}, not an object"
            )

    return current


def render_string(value, context):
    """Render a single string. Preserves type for a whole-string expression."""
    whole = WHOLE_EXPRESSION.match(value)
    if whole:
        return lookup(whole.group(1), context)

    def replace(match):
        resolved = lookup(match.group(1), context)
        return str(resolved)

    return EXPRESSION.sub(replace, value)


def render(value, context):
    """Recursively render every string inside a config structure.

    Dict keys are rendered too - occasionally you want a dynamic header name.
    """
    if isinstance(value, str):
        return render_string(value, context)

    if isinstance(value, dict):
        return {
            (render_string(k, context) if isinstance(k, str) else k): render(v, context)
            for k, v in value.items()
        }

    if isinstance(value, list):
        return [render(v, context) for v in value]

    return value


def referenced_steps(value):
    """Every step id a config depends on via ``steps.<id>....``.

    Useful for validating that a config only references steps it actually waits
    for - a step reading ``steps.upgrade.output`` without declaring
    ``needs: [upgrade]`` is a race waiting to happen.
    """
    found = set()

    def walk(node):
        if isinstance(node, str):
            for match in EXPRESSION.finditer(node):
                parts = match.group(1).split(".")
                if len(parts) >= 2 and parts[0] == "steps":
                    found.add(parts[1])
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(value)
    return found
