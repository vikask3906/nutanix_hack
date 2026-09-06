"""Entity graph writes, and the transactional outbox.

The rule this module exists to enforce:

    An entity mutation and the record of that mutation are written in ONE
    transaction, or neither is written.

The tempting alternative is to save the entity and then publish an event. That
is a dual write, and it is wrong in both directions: crash between the two and
the graph has changed with nothing downstream ever knowing; publish first and
fail to save, and you have triggered workflows for a change that never happened.

Postgres gives us the answer for free - both rows, one COMMIT. The dispatcher
then reads the outbox at its leisure. If it is down for an hour, nothing is
lost; the changes are still sitting there.
"""

import logging

from django.db import transaction
from django.utils import timezone

from graph.models import Entity, EntityChange, EntityEdge

log = logging.getLogger("cascade.graph")


def _diff(before, after):
    """Attribute names whose value actually changed.

    A write that sets department to the value it already had produces no changed
    keys, so predicates keyed on ``changed`` will not fire. That matters more
    than it sounds: systems that sync into a graph re-write unchanged rows
    constantly, and without this every full sync would re-trigger every
    onboarding workflow in the company.
    """
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


@transaction.atomic
def upsert_entity(tenant, entity_type, external_id, attrs, replace=False):
    """Create or update an entity, recording the change in the same transaction.

    ``replace=False`` merges attrs (a PATCH); ``replace=True`` overwrites them.
    Returns ``(entity, change)`` where change is None if nothing actually moved.
    """
    entity = (
        Entity.all_tenants.select_for_update()
        .filter(tenant=tenant, type=entity_type, external_id=external_id)
        .first()
    )

    if entity is None:
        entity = Entity.all_tenants.create(
            tenant=tenant,
            type=entity_type,
            external_id=external_id,
            attrs=dict(attrs),
            version=1,
        )
        change = EntityChange.all_tenants.create(
            tenant=tenant,
            entity=entity,
            entity_type=entity_type,
            before={},
            after=dict(attrs),
            changed_keys=sorted(attrs),
        )
        log.info("created %s with %s", entity.ref, sorted(attrs))
        return entity, change

    before = dict(entity.attrs)
    after = dict(attrs) if replace else {**before, **attrs}
    changed_keys = _diff(before, after)

    if not changed_keys:
        # Nothing moved. Writing an outbox row here would fire triggers for a
        # no-op write, which is exactly the re-sync storm described above.
        return entity, None

    entity.attrs = after
    entity.version += 1
    entity.save(update_fields=["attrs", "version", "updated_at"])

    change = EntityChange.all_tenants.create(
        tenant=tenant,
        entity=entity,
        entity_type=entity_type,
        before=before,
        after=after,
        changed_keys=changed_keys,
    )

    log.info("updated %s: %s", entity.ref, changed_keys)
    return entity, change


@transaction.atomic
def link(tenant, src, rel, dst):
    """Create a relationship, e.g. employee -owns-> device."""
    edge, created = EntityEdge.all_tenants.get_or_create(
        tenant=tenant, src=src, rel=rel, dst=dst
    )
    if created:
        log.info("linked %s -%s-> %s", src.ref, rel, dst.ref)
    return edge


def related(entity, rel=None, direction="out"):
    """Entities reachable from this one."""
    if direction == "out":
        qs = entity.out_edges.select_related("dst")
        if rel:
            qs = qs.filter(rel=rel)
        return [edge.dst for edge in qs]

    qs = entity.in_edges.select_related("src")
    if rel:
        qs = qs.filter(rel=rel)
    return [edge.src for edge in qs]


def change_context(change):
    """The structure a trigger's input_template is rendered against."""
    return {
        "entity": {
            "type": change.entity_type,
            "external_id": change.entity.external_id,
            "attrs": change.after,
            "ref": change.entity.ref,
        },
        "before": change.before,
        "after": change.after,
        "changed_keys": list(change.changed_keys),
    }


def unprocessed_count(tenant=None):
    qs = EntityChange.all_tenants.filter(processed_at__isnull=True)
    if tenant:
        qs = qs.filter(tenant=tenant)
    return qs.count()


def mark_processed(change):
    change.processed_at = timezone.now()
    change.save(update_fields=["processed_at"])
