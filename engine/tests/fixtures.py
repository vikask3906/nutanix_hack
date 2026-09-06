"""Shared workflow specs for tests.

LINEAR   preflight -> drain -> upgrade -> verify        (the rolling upgrade shape)
DIAMOND  start -> (branch_a, branch_b) -> join          (fan-out and join)
"""

LINEAR = {
    "name": "rolling_cluster_upgrade",
    "version": 1,
    "steps": [
        {
            "id": "preflight",
            "type": "http",
            "config": {"method": "GET", "url": "http://node/health"},
            "retry": {"max": 3, "backoff": "exponential", "base_ms": 500},
        },
        {
            "id": "drain",
            "type": "http",
            "needs": ["preflight"],
            "config": {"method": "POST", "url": "http://node/drain"},
            "compensate": {
                "type": "http",
                "config": {"method": "POST", "url": "http://node/undrain"},
            },
        },
        {
            "id": "upgrade",
            "type": "shell",
            "needs": ["drain"],
            "config": {"cmd": "./upgrade.sh", "timeout_s": 60},
            "compensate": {"type": "shell", "config": {"cmd": "./rollback.sh"}},
        },
        {
            "id": "verify",
            "type": "http",
            "needs": ["upgrade"],
            "config": {"method": "GET", "url": "http://node/health"},
            "on_error": "compensate",
        },
    ],
}

DIAMOND = {
    "name": "diamond",
    "version": 1,
    "steps": [
        {"id": "start", "type": "noop"},
        {"id": "branch_a", "type": "noop", "needs": ["start"]},
        {"id": "branch_b", "type": "noop", "needs": ["start"]},
        {"id": "join", "type": "noop", "needs": ["branch_a", "branch_b"]},
    ],
}


def ev(seq, type, step_id="", **payload):
    """Build an event as a plain dict - no database required."""
    return {"seq": seq, "type": type, "step_id": step_id, "payload": payload}
