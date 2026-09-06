"""The step plugin contract.

Adding a new kind of work to Cascade means writing one class and registering it.
The engine never learns about step types - it only knows this interface:

    name                                   the string used as "type" in a spec
    validate(config)                       raise on a bad spec, at DEFINITION time
    execute(config, context, idem_key)     do the work, return a JSON-able dict

That is the entire extensibility story, and it is the part of the design most
worth pointing at: a Slack step, a Kubernetes step, an SQL step are each about
twenty lines, and none of them require touching the worker, the queue, or the
event log.

Two rules for implementers:

1. ``execute`` must raise StepError on failure. Returning a dict means success,
   and the engine will move on to the next step.
2. ``execute`` receives ``idem_key``, a value stable across retries of the same
   attempt. Any side effect that could be applied twice should carry it, so the
   downstream system can deduplicate.
"""


class StepError(Exception):
    """A step failed. The message lands in the event log and the run's error."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class StepValidationError(ValueError):
    """A step config is invalid. Raised at definition time, never at runtime."""


class StepPlugin:
    name = ""

    # A deferring step does no work when it is first reached. Instead the engine
    # asks it WHEN to come back, records a timer, and queues the step again for
    # that moment. The worker is released immediately.
    #
    # This is the difference between a workflow that can wait three days and one
    # that cannot. A step that slept in-process would pin a worker for the whole
    # duration and lose the wait entirely on the next deploy.
    #
    # `wait` uses this today. A human-approval step is the same shape: defer
    # with no fixed resume time, and let an API call queue it.
    defers = False

    def validate(self, config):
        """Raise StepValidationError if this config could never execute."""

    def resume_at(self, config, context):
        """For deferring steps: an aware datetime to continue at.

        Returning None means "indefinitely" - nothing will pick this step up
        until something external queues it.
        """
        return None

    def execute(self, config, context, idem_key):
        """Do the work. Return a JSON-serialisable dict, or raise StepError."""
        raise NotImplementedError


_REGISTRY = {}


def register(plugin):
    """Register a plugin instance. Later registrations replace earlier ones."""
    if not plugin.name:
        raise ValueError("step plugin must declare a name")
    _REGISTRY[plugin.name] = plugin
    return plugin


def get_plugin(step_type):
    plugin = _REGISTRY.get(step_type)
    if plugin is None:
        raise StepError(
            f"no plugin registered for step type {step_type!r}; "
            f"registered types are {sorted(_REGISTRY)}"
        )
    return plugin


def registered_types():
    return sorted(_REGISTRY)
