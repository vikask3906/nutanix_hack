"""Built-in step types: noop, shell, http.

Each is small on purpose. The interesting engineering is in the engine around
them, not inside any one plugin.
"""

import shlex
import subprocess

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

    def execute(self, config, context, idem_key):
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

    def execute(self, config, context, idem_key):
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

    def execute(self, config, context, idem_key):
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


register(NoopPlugin())
register(ShellPlugin())
register(HttpPlugin())
