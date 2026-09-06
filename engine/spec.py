"""Workflow spec parsing, validation and DAG analysis.

Pure Python. No Django, no database, no I/O. You can import this from a plain REPL.

A spec is the JSON a user submits::

    {
      "name": "rolling_cluster_upgrade",
      "version": 1,
      "steps": [
        {"id": "preflight", "type": "http",  "config": {...}},
        {"id": "drain",     "type": "http",  "needs": ["preflight"], "compensate": {...}},
        {"id": "upgrade",   "type": "shell", "needs": ["drain"]}
      ]
    }

Validation happens once, when the definition is created - never during execution.
Catching a typo at definition time is a 400 response; catching it at execution time
is a half-finished workflow at 3am.
"""

KNOWN_STEP_TYPES = {"http", "shell", "wait", "noop"}

VALID_ON_ERROR = {"fail", "compensate", "continue"}

VALID_BACKOFF = {"exponential", "linear", "fixed"}


class WorkflowSpecError(ValueError):
    """Raised when a spec is structurally invalid. Always mention the step id."""


def needs_of(step):
    """Dependencies of a step, always as a list."""
    needs = step.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    return list(needs)


def steps_by_id(spec):
    """Map of step id -> step dict."""
    return {s["id"]: s for s in spec.get("steps", [])}


def step_ids(spec):
    return [s["id"] for s in spec.get("steps", [])]


def validate_spec(spec):
    """Raise WorkflowSpecError if the spec cannot be executed.

    Checks, in order: overall shape, per-step shape, duplicate ids, dangling
    dependencies, and cycles. Order matters - later checks assume earlier ones
    passed, so a spec with a duplicate id never reaches cycle detection.
    """
    if not isinstance(spec, dict):
        raise WorkflowSpecError("spec must be an object")

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowSpecError("spec.name is required and must be a non-empty string")

    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowSpecError("spec.steps is required and must be a non-empty list")

    seen = set()
    for index, step in enumerate(steps):
        where = f"steps[{index}]"

        if not isinstance(step, dict):
            raise WorkflowSpecError(f"{where} must be an object")

        sid = step.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise WorkflowSpecError(f"{where}.id is required and must be a non-empty string")

        if sid in seen:
            raise WorkflowSpecError(f"duplicate step id {sid!r}")
        seen.add(sid)

        stype = step.get("type")
        if stype not in KNOWN_STEP_TYPES:
            raise WorkflowSpecError(
                f"step {sid!r} has unknown type {stype!r}; "
                f"known types are {sorted(KNOWN_STEP_TYPES)}"
            )

        if "config" in step and not isinstance(step["config"], dict):
            raise WorkflowSpecError(f"step {sid!r}: config must be an object")

        on_error = step.get("on_error", "fail")
        if on_error not in VALID_ON_ERROR:
            raise WorkflowSpecError(
                f"step {sid!r}: on_error must be one of {sorted(VALID_ON_ERROR)}, "
                f"got {on_error!r}"
            )

        _validate_retry(sid, step.get("retry"))
        _validate_compensate(sid, step.get("compensate"))

        for dep in needs_of(step):
            if not isinstance(dep, str):
                raise WorkflowSpecError(f"step {sid!r}: needs entries must be strings")

    # Dangling dependencies - only meaningful once every id is known.
    for step in steps:
        for dep in needs_of(step):
            if dep not in seen:
                raise WorkflowSpecError(
                    f"step {step['id']!r} needs {dep!r}, which is not a step in this workflow"
                )

    detect_cycle(spec)


def _validate_retry(sid, retry):
    if retry is None:
        return
    if not isinstance(retry, dict):
        raise WorkflowSpecError(f"step {sid!r}: retry must be an object")

    max_attempts = retry.get("max", 1)
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise WorkflowSpecError(f"step {sid!r}: retry.max must be an integer >= 1")

    backoff = retry.get("backoff", "exponential")
    if backoff not in VALID_BACKOFF:
        raise WorkflowSpecError(
            f"step {sid!r}: retry.backoff must be one of {sorted(VALID_BACKOFF)}"
        )

    base_ms = retry.get("base_ms", 500)
    if not isinstance(base_ms, int) or base_ms < 0:
        raise WorkflowSpecError(f"step {sid!r}: retry.base_ms must be a non-negative integer")


def _validate_compensate(sid, comp):
    if comp is None:
        return
    if not isinstance(comp, dict):
        raise WorkflowSpecError(f"step {sid!r}: compensate must be an object")
    if comp.get("type") not in KNOWN_STEP_TYPES:
        raise WorkflowSpecError(
            f"step {sid!r}: compensate.type must be one of {sorted(KNOWN_STEP_TYPES)}"
        )


def detect_cycle(spec):
    """Raise WorkflowSpecError if the dependency graph has a cycle.

    Iterative depth-first search with three colours. WHITE = unvisited,
    GREY = on the current path, BLACK = fully explored. Reaching a GREY node
    means we have looped back onto our own path, so we report the actual cycle
    rather than a bare "cycle detected" - that message is useless in a 40-step
    workflow.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    by_id = steps_by_id(spec)
    colour = {sid: WHITE for sid in by_id}

    for root in by_id:
        if colour[root] != WHITE:
            continue

        stack = [(root, iter(needs_of(by_id[root])))]
        path = [root]
        colour[root] = GREY

        while stack:
            node, deps = stack[-1]
            advanced = False

            for dep in deps:
                if colour[dep] == GREY:
                    cycle = path[path.index(dep):] + [dep]
                    raise WorkflowSpecError(
                        "dependency cycle: " + " -> ".join(cycle)
                    )
                if colour[dep] == WHITE:
                    colour[dep] = GREY
                    path.append(dep)
                    stack.append((dep, iter(needs_of(by_id[dep]))))
                    advanced = True
                    break

            if not advanced:
                colour[node] = BLACK
                stack.pop()
                path.pop()


def topological_order(spec):
    """Steps in a valid execution order (dependencies before dependents).

    Kahn's algorithm. Cascade does not actually execute in this order - the
    engine works out readiness dynamically from the event log, so parallel
    branches run concurrently. This exists for visualisation and for tests.
    """
    by_id = steps_by_id(spec)
    indegree = {sid: 0 for sid in by_id}
    dependents = {sid: [] for sid in by_id}

    for sid, step in by_id.items():
        for dep in needs_of(step):
            indegree[sid] += 1
            dependents[dep].append(sid)

    # Sorted for determinism: the same spec always yields the same order.
    queue = sorted([sid for sid, d in indegree.items() if d == 0])
    order = []

    while queue:
        sid = queue.pop(0)
        order.append(sid)
        for child in dependents[sid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
        queue.sort()

    if len(order) != len(by_id):
        raise WorkflowSpecError("dependency cycle detected during topological sort")

    return order
