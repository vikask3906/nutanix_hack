from rest_framework import serializers

from engine.models import Run, RunEvent, Task, WorkflowDef
from engine.spec import WorkflowSpecError, validate_spec


class WorkflowDefSerializer(serializers.ModelSerializer):
    step_count = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowDef
        fields = ["id", "name", "version", "description", "spec", "step_count", "created_at"]
        read_only_fields = ["id", "version", "created_at"]

    def get_step_count(self, obj):
        return len(obj.spec.get("steps", []))


class WorkflowDefCreateSerializer(serializers.Serializer):
    spec = serializers.JSONField()
    description = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_spec(self, value):
        """Reject a bad spec at definition time, with the engine's own message.

        This is the only place spec validation is user-facing. Once a definition
        exists, execution assumes it is sound.
        """
        try:
            validate_spec(value)
        except WorkflowSpecError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class RunEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = RunEvent
        fields = ["seq", "type", "step_id", "payload", "created_at"]


class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = [
            "id", "step_id", "kind", "attempt", "status",
            "run_after", "lease_owner", "lease_expires_at",
        ]


class RunSerializer(serializers.ModelSerializer):
    workflow = serializers.CharField(source="definition.name", read_only=True)
    workflow_version = serializers.IntegerField(source="definition.version", read_only=True)

    class Meta:
        model = Run
        fields = [
            "id", "workflow", "workflow_version", "status", "input",
            "entity_ref", "trigger_source", "error", "last_seq",
            "created_at", "updated_at",
        ]


class RunDetailSerializer(RunSerializer):
    """A run plus everything needed to render it: derived state, log, queue."""

    events = RunEventSerializer(many=True, read_only=True)
    tasks = TaskSerializer(many=True, read_only=True)
    spec = serializers.JSONField(source="definition.spec", read_only=True)

    class Meta(RunSerializer.Meta):
        fields = RunSerializer.Meta.fields + ["context", "spec", "events", "tasks"]


class ApprovalDecisionSerializer(serializers.Serializer):
    step_id = serializers.CharField()
    decision = serializers.ChoiceField(choices=["approve", "deny"])
    actor = serializers.CharField(required=False, allow_blank=True, default="")
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class StartRunSerializer(serializers.Serializer):
    workflow = serializers.CharField()
    version = serializers.IntegerField(required=False)
    input = serializers.JSONField(required=False, default=dict)
    entity_ref = serializers.CharField(required=False, allow_blank=True, default="")
