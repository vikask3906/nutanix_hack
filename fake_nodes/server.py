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

# Stand-ins for the third-party systems an onboarding workflow touches. The
# point of having them here is that the SAME engine drives cluster operations
# and identity provisioning - the engine knows about neither.
ACCOUNTS = {}   # app name -> {employee id: {...}}
DEVICES = {}    # employee id -> device id

# ---------------------------------------------------------------------------
# A small company's real systems, standing in for Google Workspace, Slack,
# GitHub, the payroll provider and the IT asset system.
#
# The point of naming them properly: the onboarding demo reads like something
# that happens at an actual company, and the failure case - a person with
# commit access to the codebase but no employment record - is a real problem
# that real companies have.
# ---------------------------------------------------------------------------

COMPANY = {
    "email_accounts": {},     # person -> address
    "slack_members": {},      # person -> channel list
    "github_members": {},     # person -> access level
    "payroll_enrolled": {},   # person -> salary band
    "laptop_orders": {},      # person -> order details
}

# Person ids whose payroll enrolment will be rejected, to make the failure
# deterministic on camera.
PAYROLL_WILL_REJECT = set()


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

        if parts == ["apps"]:
            with LOCK:
                self._send(200, {"accounts": ACCOUNTS})
            return

        if parts == ["devices"]:
            with LOCK:
                self._send(200, {"devices": DEVICES})
            return

        if parts == ["company"]:
            with LOCK:
                self._send(200, COMPANY)
            return

        if parts == ["healthz"]:
            self._send(200, {"status": "ok"})
            return

        self._send(404, {"error": f"no route for GET {self.path}"})

    def do_POST(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}

        if parts == ["reset"]:
            with LOCK:
                NODES.clear()
                NODES.update(fresh_cluster())
                SEEN_KEYS.clear()
                ACCOUNTS.clear()
                DEVICES.clear()
                for store in COMPANY.values():
                    store.clear()
                PAYROLL_WILL_REJECT.clear()
            self.log_message("everything reset")
            self._send(200, {"reset": True, "nodes": list(NODES.values())})
            return

        # -- the company's systems ---------------------------------------

        if parts and parts[0] in {"google", "slack", "github", "payroll", "it"}:
            self._company(parts, body)
            return

        # -- identity provisioning ---------------------------------------

        if len(parts) == 3 and parts[0] == "apps" and parts[2] == "accounts":
            app = parts[1]
            employee = body.get("employee")
            if not employee:
                self._send(400, {"error": "employee is required"})
                return
            with LOCK:
                if self._duplicate(f"account:{app}", employee):
                    self.log_message("duplicate account %s/%s absorbed", app, employee)
                    self._send(200, {"app": app, "employee": employee, "duplicate": True})
                    return
                ACCOUNTS.setdefault(app, {})[employee] = {
                    "employee": employee,
                    "role": body.get("role", "member"),
                }
                self.log_message("provisioned %s account for %s", app, employee)
            self._send(201, {"app": app, "employee": employee, "duplicate": False})
            return

        if len(parts) == 2 and parts[0] == "devices" and parts[1] == "assign":
            employee = body.get("employee")
            device = body.get("device", "laptop-standard")
            if not employee:
                self._send(400, {"error": "employee is required"})
                return
            with LOCK:
                if self._duplicate("device", employee):
                    self._send(200, {"employee": employee, "duplicate": True})
                    return
                DEVICES[employee] = device
                self.log_message("assigned %s to %s", device, employee)
            self._send(201, {"employee": employee, "device": device, "duplicate": False})
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


    # -- company system handlers -----------------------------------------

    def _company(self, parts, body):
        system, action = parts[0], parts[1]
        person = body.get("person") or (parts[2] if len(parts) > 2 else None)

        if not person:
            self._send(400, {"error": "person is required"})
            return

        with LOCK:
            # ---- Google Workspace ----
            if system == "google" and action == "create-account":
                address = f"{person}@northwind.example"
                COMPANY["email_accounts"][person] = address
                self.log_message("Google: created mailbox %s", address)
                self._send(201, {"person": person, "email": address})
                return

            if system == "google" and action == "delete-account":
                removed = COMPANY["email_accounts"].pop(person, None)
                self.log_message("Google: DELETED mailbox for %s", person)
                self._send(200, {"person": person, "deleted": removed})
                return

            # ---- Slack ----
            if system == "slack" and action == "invite":
                COMPANY["slack_members"][person] = body.get(
                    "channels", ["#general", "#engineering"]
                )
                self.log_message("Slack: invited %s", person)
                self._send(201, {"person": person})
                return

            if system == "slack" and action == "deactivate":
                COMPANY["slack_members"].pop(person, None)
                self.log_message("Slack: DEACTIVATED %s", person)
                self._send(200, {"person": person})
                return

            # ---- GitHub ----
            if system == "github" and action == "add-member":
                COMPANY["github_members"][person] = body.get("access", "write")
                self.log_message("GitHub: granted %s WRITE access to the org", person)
                self._send(201, {"person": person, "access": "write"})
                return

            if system == "github" and action == "remove-member":
                COMPANY["github_members"].pop(person, None)
                self.log_message("GitHub: REVOKED access for %s", person)
                self._send(200, {"person": person})
                return

            # ---- Payroll (the one that can reject) ----
            if system == "payroll" and action == "enrol":
                if person in PAYROLL_WILL_REJECT:
                    self.log_message("Payroll: REJECTED %s - tax id not verified", person)
                    self._send(422, {
                        "person": person,
                        "error": "tax identification number could not be verified "
                                 "with the authority",
                    })
                    return
                COMPANY["payroll_enrolled"][person] = body.get("band", "IC3")
                self.log_message("Payroll: enrolled %s", person)
                self._send(201, {"person": person})
                return

            if system == "payroll" and action == "reject-next":
                PAYROLL_WILL_REJECT.add(person)
                self.log_message("Payroll: armed to reject %s", person)
                self._send(200, {"person": person, "will_reject": True})
                return

            # ---- IT asset system ----
            if system == "it" and action == "order-laptop":
                COMPANY["laptop_orders"][person] = {
                    "model": body.get("model", "MacBook Pro 14"),
                    "status": "ordered",
                }
                self.log_message("IT: ordered a laptop for %s", person)
                self._send(201, {"person": person})
                return

            if system == "it" and action == "cancel-laptop":
                COMPANY["laptop_orders"].pop(person, None)
                self.log_message("IT: CANCELLED the laptop order for %s", person)
                self._send(200, {"person": person})
                return

        self._send(404, {"error": f"unknown action {system}/{action}"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9000"))
    print(f"[nodes] fake cluster of {NODE_COUNT} nodes on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
