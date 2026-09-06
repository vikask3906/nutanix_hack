"""A fake five-node cluster, so the rolling-upgrade demo acts on real state.

Standard library only - no extra dependency, and it starts in milliseconds.

    GET  /nodes                  every node's state
    GET  /nodes/<id>/health      200 if healthy, 503 if not
    POST /nodes/<id>/drain       stop scheduling work here
    POST /nodes/<id>/undrain     the compensating action for drain
    POST /nodes/<id>/upgrade     bump version, remembering the previous one
    POST /nodes/<id>/rollback    the compensating action for upgrade
    POST /nodes/<id>/fail-after-upgrade   arm: this node breaks when upgraded
    POST /reset                  back to a clean cluster

``fail-after-upgrade`` is what makes the demo deterministic. Rather than hoping
something breaks on camera, you arm node-3 and know exactly when the rollback
will fire. It deliberately fires only once the node has been upgraded, so the
pre-upgrade preflight check still passes - otherwise preflight consumes the
armed failure and the interesting path never runs.

Every mutating handler records the Idempotency-Key it received. Cascade
guarantees at-least-once delivery, so a step can genuinely run twice; this
service reports duplicates back so the demo can show they were absorbed rather
than applied twice.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()

NODE_COUNT = 5
BASE_VERSION = "7.0"
TARGET_VERSION = "7.1"


def fresh_cluster():
    return {
        f"node-{i}": {
            "id": f"node-{i}",
            "version": BASE_VERSION,
            "previous_version": None,
            "drained": False,
            "healthy": True,
            # Armed by /fail-after-upgrade. Fires only once the node has
            # actually been upgraded, so the PRE-upgrade preflight check still
            # passes and it is the POST-upgrade verify that fails. That is both
            # more realistic ("the upgrade broke it") and deterministic - no
            # arming mid-run and hoping the timing lands.
            "fail_after_upgrade": False,
        }
        for i in range(1, NODE_COUNT + 1)
    }


NODES = fresh_cluster()
SEEN_KEYS = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[nodes] {fmt % args}", flush=True)

    # -- helpers ---------------------------------------------------------

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _node(self, node_id):
        node = NODES.get(node_id)
        if node is None:
            self._send(404, {"error": f"no such node {node_id}"})
        return node

    def _duplicate(self, action, node_id):
        """True if this exact delivery was already applied."""
        key = self.headers.get("Idempotency-Key")
        if not key:
            return False
        marker = f"{action}:{node_id}:{key}"
        if marker in SEEN_KEYS:
            return True
        SEEN_KEYS[marker] = True
        return False

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]

        if parts == ["nodes"]:
            with LOCK:
                self._send(200, {"nodes": list(NODES.values())})
            return

        if len(parts) == 3 and parts[0] == "nodes" and parts[2] == "health":
            with LOCK:
                node = self._node(parts[1])
                if node is None:
                    return

                # Armed, and the node has been upgraded: the upgrade broke it.
                if node["fail_after_upgrade"] and node["previous_version"]:
                    node["healthy"] = False

                if not node["healthy"]:
                    self.log_message("node %s health check FAILED", node["id"])
                    self._send(503, {"node": node["id"], "healthy": False,
                                     "version": node["version"],
                                     "reason": "node reports unhealthy"})
                    return

                self._send(200, {"node": node["id"], "healthy": True,
                                 "version": node["version"],
                                 "drained": node["drained"]})
            return

        if parts == ["healthz"]:
            self._send(200, {"status": "ok"})
            return

        self._send(404, {"error": f"no route for GET {self.path}"})

    def do_POST(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]

        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        if parts == ["reset"]:
            with LOCK:
                NODES.clear()
                NODES.update(fresh_cluster())
                SEEN_KEYS.clear()
            self.log_message("cluster reset")
            self._send(200, {"reset": True, "nodes": list(NODES.values())})
            return

        if len(parts) != 3 or parts[0] != "nodes":
            self._send(404, {"error": f"no route for POST {self.path}"})
            return

        node_id, action = parts[1], parts[2]

        with LOCK:
            node = self._node(node_id)
            if node is None:
                return

            if self._duplicate(action, node_id):
                self.log_message("duplicate %s on %s absorbed", action, node_id)
                self._send(200, {"node": node_id, "action": action,
                                 "duplicate": True, "state": node})
                return

            if action == "drain":
                node["drained"] = True
            elif action == "undrain":
                node["drained"] = False
            elif action == "upgrade":
                node["previous_version"] = node["version"]
                node["version"] = TARGET_VERSION
            elif action == "rollback":
                if node["previous_version"]:
                    node["version"] = node["previous_version"]
                    node["previous_version"] = None
                # Restoring the old version restores health - that is the whole
                # premise of rolling back a bad upgrade.
                node["healthy"] = True
                node["fail_after_upgrade"] = False
            elif action == "fail-after-upgrade":
                node["fail_after_upgrade"] = True
            else:
                self._send(404, {"error": f"unknown action {action}"})
                return

            self.log_message("%s %s -> %s", action, node_id,
                             json.dumps({k: node[k] for k in
                                         ("version", "drained", "healthy")}))
            self._send(200, {"node": node_id, "action": action,
                             "duplicate": False, "state": node})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9000"))
    print(f"[nodes] fake cluster of {NODE_COUNT} nodes on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
