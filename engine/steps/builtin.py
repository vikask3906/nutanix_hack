"""Built-in step types: noop, shell, http.

Each is small on purpose. The interesting engineering is in the engine around
them, not inside any one plugin.
"""

import shlex
import subprocess
from datetime import datetime, timedelta, timezone

import requests

from engine.steps.base import StepError, StepPlugin, StepValidationError, register

DEFAULT_HTTP_TIMEOUT = 10
DEFAULT_SHELL_TIMEOUT = 60

# Response bodies and command output go into the event log. Without a cap, one
# chatty step turns every future replay of that run into a multi-megabyte read.
MAX_CAPTURED_CHARS = 8000


def _truncate(text):
    if text is None:
        return ""
    text = str(text)
    if len(text) <= MAX_CAPTURED_CHARS:
        return text
    return text[:MAX_CAPTURED_CHARS] + f"... [truncated, {len(text)} chars total]"


class NoopPlugin(StepPlugin):
    """Does nothing, successfully. Useful for DAG structure and for tests."""

    name = "noop"

    def execute(self, config, context, idem_key, step_id=""):
        return dict(config.get("output", {}))


class ShellPlugin(StepPlugin):
    """Run a command.

    Note ``shell=False`` and ``shlex.split``: the command is exec'd directly
    rather than handed to a shell, so a value interpolated from a template
    cannot inject ``; rm -rf /``. That matters because step configs are rendered
    from run input, which in production comes from outside.

    This is not a sandbox. A real deployment runs each shell step in its own
    container with resource limits and no network by default. Building that is
    out of scope here, but do not describe this as sandboxed - it isn't.
    """

    name = "shell"

    def validate(self, config):
        if not config.get("cmd"):
            raise StepValidationError("shell step requires 'cmd'")

    def execute(self, config, context, idem_key, step_id=""):
        cmd = config["cmd"]
        timeout = config.get("timeout_s", DEFAULT_SHELL_TIMEOUT)

        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            raise StepError(f"could not parse command: {exc}") from exc

        if not argv:
            raise StepError("empty command")

        env = dict(config.get("env", {}))
        env["CASCADE_IDEMPOTENCY_KEY"] = idem_key

        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                cwd=config.get("cwd") or None,
                env={**env} if config.get("env_only") else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise StepError(f"command timed out after {timeout}s", {"cmd": cmd}) from exc
        except FileNotFoundError as exc:
            raise StepError(f"command not found: {argv[0]}") from exc

        output = {
            "exit_code": completed.returncode,
            "stdout": _truncate(completed.stdout),
            "stderr": _truncate(completed.stderr),
        }

        if completed.returncode != 0:
            raise StepError(
                f"command exited {completed.returncode}: {_truncate(completed.stderr).strip()[:300]}",
                output,
            )

        return output


class HttpPlugin(StepPlugin):
    """Call a REST endpoint.

    The idempotency key is sent as an ``Idempotency-Key`` header. Cascade
    guarantees at-least-once delivery, so a step CAN run twice - once when the
    worker died just after the side effect but before recording it. A server that
    honours the header turns that into exactly-once in practice.
    """

    name = "http"

    def validate(self, config):
        if not config.get("url"):
            raise StepValidationError("http step requires 'url'")
        method = config.get("method", "GET")
        if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise StepValidationError(f"unsupported http method {method!r}")

    def execute(self, config, context, idem_key, step_id=""):
        method = config.get("method", "GET").upper()
        url = config["url"]
        timeout = config.get("timeout_s", DEFAULT_HTTP_TIMEOUT)

        headers = dict(config.get("headers", {}))
        headers.setdefault("Idempotency-Key", idem_key)

        try:
            response = requests.request(
                method,
                url,
                json=config.get("body"),
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise StepError(f"{method} {url} failed: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = _truncate(response.text)

        output = {"status_code": response.status_code, "body": body}

        expected = config.get("expect_status")
        if expected is not None:
            if response.status_code != expected:
                raise StepError(
                    f"{method} {url} returned {response.status_code}, expected {expected}",
                    output,
                )
        elif response.status_code >= 400:
            raise StepError(f"{method} {url} returned {response.status_code}", output)

        return output


class WaitPlugin(StepPlugin):
    """Pause the workflow, without pausing anything.

    Nothing sleeps. The step records a timer and queues itself for a future
    moment; the worker moves on to other work immediately. The wait lives in a
    ``tasks`` row with a future ``run_after`` - the same row shape as a ready
    task and a backoff retry.

    That is why Cascade has no scheduler process and no timer service, and why a
    wait survives every worker dying, the whole stack being redeployed, or the
    database being restored from a backup. There is nothing in memory to lose.

    Config is either a duration or an absolute time::

        {"duration_s": 3600}
        {"until": "2026-09-08T09:00:00Z"}
    """

    name = "wait"
    defers = True

    def validate(self, config):
        if "duration_s" not in config and "until" not in config:
            raise StepValidationError("wait step requires 'duration_s' or 'until'")

        if "duration_s" in config:
            duration = config["duration_s"]
            if not isinstance(duration, (int, float)) or duration < 0:
                raise StepValidationError(
                    "wait.duration_s must be a non-negative number"
                )

        if "until" in config:
            try:
                self._parse(config["until"])
            except ValueError as exc:
                raise StepValidationError(
                    f"wait.until must be an ISO 8601 timestamp: {exc}"
                ) from exc

    @staticmethod
    def _parse(value):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # A naive timestamp is ambiguous across deployments. Assume UTC
            # rather than silently adopting whatever the worker's clock says.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def resume_at(self, config, context, step_id=""):
        if "until" in config:
            return self._parse(config["until"])
        return datetime.now(timezone.utc) + timedelta(seconds=config["duration_s"])

    def execute(self, config, context, idem_key, step_id=""):
        # Reached only once the timer has fired. There is nothing to do; the
        # passage of time was the work.
        return {"waited": True}


class ApprovalPlugin(StepPlugin):
    """Pause until a person decides.

    Structurally identical to a wait - the workflow parks and the worker is
    released - but what ends the pause is a human calling::

        POST /api/runs/<id>/approve/  {"step_id": "...", "decision": "approve"}

    ``resume_at`` returns None: nothing is scheduled, because nothing is
    counting down. The step sits as a row in the log and waits, indefinitely,
    at zero cost. A workflow can be parked here for a week without occupying a
    worker, a thread, or a connection.

    An optional ``timeout_s`` arms a deadline, and reaching it without a
    decision fails the step - which is what you want for anything with an SLA.

    Config::

        {"prompt": "Approve production upgrade?",
         "approvers": ["ops-oncall"],
         "timeout_s": 86400}
    """

    name = "approval"
    defers = True

    def validate(self, config):
        if not config.get("prompt"):
            raise StepValidationError("approval step requires 'prompt'")

        timeout = config.get("timeout_s")
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            raise StepValidationError("approval.timeout_s must be a positive number")

        approvers = config.get("approvers", [])
        if not isinstance(approvers, list):
            raise StepValidationError("approval.approvers must be a list")

    def should_defer(self, config, context, step_id):
        """Park only on the first visit.

        Every later visit means something acted: either a decision was recorded
        and the API queued this step, or the timeout came due. Deferring again
        would park a step that a person has already approved.
        """
        return not context["steps"].get(step_id, {}).get("approval_requested")

    def resume_at(self, config, context, step_id=""):
        if config.get("timeout_s"):
            return datetime.now(timezone.utc) + timedelta(seconds=config["timeout_s"])
        return None  # wait indefinitely - a person is the clock

    def execute(self, config, context, idem_key, step_id=""):
        slot = context["steps"].get(step_id, {})

        if slot.get("approved"):
            return {
                "approved": True,
                "approved_by": slot.get("approved_by", ""),
                "comment": slot.get("approval_comment", ""),
            }

        # Reached without an approval, so the deadline is what woke us.
        raise StepError(
            f"approval timed out after {config.get('timeout_s')}s "
            f"with no decision recorded"
        )


register(NoopPlugin())
register(ShellPlugin())
register(HttpPlugin())
register(WaitPlugin())
register(ApprovalPlugin())

