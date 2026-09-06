"""Trigger predicates. Pure Python - no Django, no database.

A predicate decides whether one change to the entity graph should start a
workflow. This is Cascade's version of a Rippling Supergroup: a saved rule over
the graph rather than a static list of things to act on.

    {
      "changed": ["department"],
      "match":   {"department": "Engineering", "geo": "IN"},
      "was":     {"department": {"ne": "Engineering"}}
    }

Read as: fire when the department attribute changed, AND the entity now has
department Engineering in India, AND it was not in Engineering before.

Three clauses, and you usually need more than one:

    changed   which attributes were touched     (about the transition)
    match     the state AFTER the change        (about the destination)
    was       the state BEFORE the change       (about the origin)

``match`` alone is not enough. "department is Engineering" is true on every
subsequent save of that employee, so an onboarding workflow would fire again
every time anyone edited an unrelated field. ``changed`` is what makes it fire
on the transition rather than on the state.

Values are either a literal (equality) or one operator object::

    "Engineering"                exact match
    {"in": ["Eng", "Ops"]}       membership
    {"ne": "Engineering"}        inequality
    {"gt": 90}                   ordered comparison: gt gte lt lte
    {"exists": true}             attribute present (and not None)
    {"contains": "admin"}        substring, or membership in a list attribute
"""

OPERATORS = {"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "exists", "contains"}


class PredicateError(ValueError):
    """A predicate is malformed. Raised when a trigger is saved, not when it runs."""


def validate_predicate(predicate):
    """Raise PredicateError if this predicate could never be evaluated."""
    if not isinstance(predicate, dict):
        raise PredicateError("predicate must be an object")

    unknown = set(predicate) - {"changed", "match", "was"}
    if unknown:
        raise PredicateError(
            f"unknown predicate clause(s): {sorted(unknown)}; "
            "expected any of changed, match, was"
        )

    if not predicate:
        raise PredicateError(
            "empty predicate would fire on every change to this entity type"
        )

    changed = predicate.get("changed")
    if changed is not None:
        if not isinstance(changed, list) or not all(isinstance(c, str) for c in changed):
            raise PredicateError("predicate.changed must be a list of attribute names")

    for clause in ("match", "was"):
        block = predicate.get(clause)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise PredicateError(f"predicate.{clause} must be an object")
        for attr, expected in block.items():
            if isinstance(expected, dict):
                if len(expected) != 1:
                    raise PredicateError(
                        f"predicate.{clause}.{attr} must contain exactly one operator, "
                        f"got {sorted(expected)}"
                    )
                op = next(iter(expected))
                if op not in OPERATORS:
                    raise PredicateError(
                        f"predicate.{clause}.{attr}: unknown operator {op!r}; "
                        f"expected one of {sorted(OPERATORS)}"
                    )


def _compare(actual, expected):
    """Evaluate one attribute against one expectation."""
    if not isinstance(expected, dict):
        return actual == expected

    op, value = next(iter(expected.items()))

    if op == "eq":
        return actual == value
    if op == "ne":
        return actual != value
    if op == "in":
        return actual in value
    if op == "not_in":
        return actual not in value
    if op == "exists":
        return (actual is not None) == bool(value)
    if op == "contains":
        if actual is None:
            return False
        try:
            return value in actual
        except TypeError:
            return False

    # Ordered comparisons. Attributes are schemaless, so the value may be of a
    # type that cannot be ordered against the expectation - a string where a
    # number was meant. That is a non-match, not a crash: one bad row must not
    # stop the dispatcher processing every other change.
    try:
        if op == "gt":
            return actual > value
        if op == "gte":
            return actual >= value
        if op == "lt":
            return actual < value
        if op == "lte":
            return actual <= value
    except TypeError:
        return False

    return False


def _all_match(block, attrs):
    return all(_compare(attrs.get(attr), expected) for attr, expected in block.items())


def matches(predicate, change):
    """Does this change satisfy this predicate?

    ``change`` is a dict with ``changed_keys``, ``before`` and ``after``.
    Clauses are ANDed; an absent clause is simply not checked.
    """
    changed = predicate.get("changed")
    if changed is not None:
        # ANY of the named attributes having changed is enough. "fire when
        # department or title changes" is the common intent; requiring all of
        # them to change in one write almost never is.
        if not set(changed) & set(change.get("changed_keys") or []):
            return False

    match = predicate.get("match")
    if match is not None and not _all_match(match, change.get("after") or {}):
        return False

    was = predicate.get("was")
    if was is not None and not _all_match(was, change.get("before") or {}):
        return False

    return True


def describe(predicate):
    """A human-readable summary, for the admin and for demo output."""
    parts = []
    if predicate.get("changed"):
        parts.append("when " + " or ".join(predicate["changed"]) + " changes")
    if predicate.get("was"):
        parts.append("from " + ", ".join(f"{k}={v}" for k, v in predicate["was"].items()))
    if predicate.get("match"):
        parts.append("to " + ", ".join(f"{k}={v}" for k, v in predicate["match"].items()))
    return "; ".join(parts) or "always"
