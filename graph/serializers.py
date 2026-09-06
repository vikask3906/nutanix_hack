from rest_framework import serializers

from graph.models import Entity, EntityChange, EntityEdge, Trigger
from graph.predicates import PredicateError, describe, validate_predicate


class EntitySerializer(serializers.ModelSerializer):
    ref = serializers.CharField(read_only=True)

    class Meta:
        model = Entity
        fields = ["id", "ref", "type", "external_id", "attrs", "version", "updated_at"]
        read_only_fields = ["id", "ref", "version", "updated_at"]


class UpsertEntitySerializer(serializers.Serializer):
    type = serializers.CharField()
    external_id = serializers.CharField()
    attrs = serializers.JSONField(required=False, default=dict)
    replace = serializers.BooleanField(required=False, default=False)


class PatchEntitySerializer(serializers.Serializer):
    attrs = serializers.JSONField()
    replace = serializers.BooleanField(required=False, default=False)


class EntityEdgeSerializer(serializers.ModelSerializer):
    src = serializers.CharField(source="src.ref", read_only=True)
    dst = serializers.CharField(source="dst.ref", read_only=True)

    class Meta:
        model = EntityEdge
        fields = ["id", "src", "rel", "dst"]


class EntityChangeSerializer(serializers.ModelSerializer):
    entity = serializers.CharField(source="entity.ref", read_only=True)

    class Meta:
        model = EntityChange
        fields = [
            "id", "entity", "entity_type", "before", "after",
            "changed_keys", "created_at", "processed_at",
        ]


class TriggerSerializer(serializers.ModelSerializer):
    workflow = serializers.CharField(source="definition.name", read_only=True)
    reads_as = serializers.SerializerMethodField()

    class Meta:
        model = Trigger
        fields = [
            "id", "name", "entity_type", "predicate", "input_template",
            "workflow", "reads_as", "enabled",
        ]
        read_only_fields = ["id", "workflow", "reads_as"]

    def get_reads_as(self, obj):
        return describe(obj.predicate)


class CreateTriggerSerializer(serializers.Serializer):
    name = serializers.CharField()
    entity_type = serializers.CharField()
    workflow = serializers.CharField()
    predicate = serializers.JSONField()
    input_template = serializers.JSONField(required=False, default=dict)
    enabled = serializers.BooleanField(required=False, default=True)

    def validate_predicate(self, value):
        """Reject an unevaluable predicate when the trigger is saved.

        The dispatcher must never be the place a predicate is found to be
        broken - by then it is on the hot path of every change to that entity
        type.
        """
        try:
            validate_predicate(value)
        except PredicateError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value
