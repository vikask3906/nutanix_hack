"""The entity graph - Cascade's version of Rippling's Employee Graph.

The idea: hold every real-world thing you care about as a typed node with free-form
attributes, plus the relationships between them. Then let *changes* to that graph be
what starts workflows, instead of someone remembering to call an API.

    Entity        a typed thing: employee, device, cluster, node, vm
    EntityEdge    a relationship: employee -owns-> device
    EntityChange  the transactional outbox: every mutation, recorded
    Trigger       a predicate over changes; when it matches, a workflow runs

Rippling's insight is that a department change should not require the IT system to
poll HR. One graph, one change, and everything downstream reacts. That is the
difference between a pipeline and a platform.
"""

import uuid

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from core.models import TenantScopedModel, TimestampedModel


class Entity(TenantScopedModel, TimestampedModel):
    """A typed node.

    ``attrs`` is deliberately schemaless. An Employee has a department and a manager;
    a Node has a rack and a firmware version. Forcing both into columns means a
    migration every time the business learns something new - which is exactly the
    trap the rigid HR systems fell into.

    The GIN index is what keeps that decision from being a performance disaster:
    ``attrs__department="Engineering"`` stays fast.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=64)          # "employee", "node", "device"
    external_id = models.CharField(max_length=200)  # stable id in the source system
    attrs = models.JSONField(default=dict, blank=True)

    # Bumped on every mutation. Lets a caller do optimistic concurrency against the
    # graph the same way workers do against the run log.
    version = models.PositiveIntegerField(default=1)

    class Meta(TenantScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "type", "external_id"],
                name="uniq_entity_tenant_type_extid",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "type"]),
            GinIndex(fields=["attrs"], name="idx_entity_attrs_gin"),
        ]
        verbose_name_plural = "entities"
        ordering = ["type", "external_id"]

    def __str__(self):
        return f"{self.type}:{self.external_id}"

    @property
    def ref(self):
        """The string used for per-entity serialisation, e.g. 'employee:e_42'."""
        return f"{self.type}:{self.external_id}"


class EntityEdge(TenantScopedModel):
    """A directed, labelled relationship: employee -owns-> device."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    src = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="out_edges")
    rel = models.CharField(max_length=64)
    dst = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="in_edges")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(TenantScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "src", "rel", "dst"], name="uniq_edge"
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "src", "rel"]),
            models.Index(fields=["tenant", "dst", "rel"]),
        ]

    def __str__(self):
        return f"{self.src} -{self.rel}-> {self.dst}"


class EntityChange(TenantScopedModel):
    """Transactional outbox.

    Written in the SAME transaction as the entity mutation. That is the whole point:
    if we instead wrote the entity and then published an event, a crash in between
    would leave the graph changed with nothing downstream ever knowing. One
    transaction means the change and the record of the change cannot disagree.

    The dispatcher claims unprocessed rows with the same FOR UPDATE SKIP LOCKED
    pattern the workers use on tasks.
    """

    id = models.BigAutoField(primary_key=True)
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="changes")
    entity_type = models.CharField(max_length=64)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    changed_keys = ArrayField(models.CharField(max_length=200), default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantScopedModel.Meta):
        indexes = [
            # The dispatcher's claim index: unprocessed rows, oldest first.
            models.Index(fields=["processed_at", "created_at"]),
            models.Index(fields=["tenant", "entity_type"]),
        ]
        ordering = ["id"]

    def __str__(self):
        return f"{self.entity_type} change #{self.id} {self.changed_keys}"


class Trigger(TenantScopedModel, TimestampedModel):
    """A predicate over entity changes. Cascade's answer to a Rippling Supergroup.

    Predicate shape::

        {"changed": ["department"],
         "match":   {"department": "Engineering", "geo": "IN"}}

    Read as: fire when the department field changed, AND the resulting entity has
    department Engineering in India. ``changed`` filters on the transition,
    ``match`` filters on the resulting state - you almost always need both.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    entity_type = models.CharField(max_length=64)
    predicate = models.JSONField(default=dict, blank=True)
    definition = models.ForeignKey(
        "engine.WorkflowDef", on_delete=models.CASCADE, related_name="triggers"
    )
    enabled = models.BooleanField(default=True)

    # How the change becomes the run's input. Rendered with the same expression
    # engine step configs use, against {entity, before, after, changed_keys}:
    #
    #     {"node": "{{ entity.external_id }}"}
    #
    # This is what lets one workflow be driven by several different entity
    # types without the workflow knowing anything about the graph.
    input_template = models.JSONField(default=dict, blank=True)

    class Meta(TenantScopedModel.Meta):
        indexes = [models.Index(fields=["tenant", "entity_type", "enabled"])]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.entity_type})"

