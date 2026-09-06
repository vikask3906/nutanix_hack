from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from engine.models import WorkflowDef
from engine.service import default_tenant
from graph.models import Entity, EntityChange, Trigger
from graph.serializers import (
    CreateTriggerSerializer,
    EntityChangeSerializer,
    EntityEdgeSerializer,
    EntitySerializer,
    PatchEntitySerializer,
    TriggerSerializer,
    UpsertEntitySerializer,
)
from graph.service import link, upsert_entity


def current_tenant(request):
    return default_tenant()


class EntityListView(APIView):
    """List entities, or upsert one.

    Upsert rather than create: a graph is fed by systems that re-send state,
    and making the caller know whether a row already exists is the wrong
    burden. Writing the same attrs twice is a no-op that fires no triggers.
    """

    def get(self, request):
        qs = Entity.objects.filter(tenant=current_tenant(request))
        entity_type = request.query_params.get("type")
        if entity_type:
            qs = qs.filter(type=entity_type)
        return Response(EntitySerializer(qs, many=True).data)

    def post(self, request):
        payload = UpsertEntitySerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        entity, change = upsert_entity(
            current_tenant(request),
            data["type"],
            data["external_id"],
            data.get("attrs") or {},
            replace=data.get("replace", False),
        )

        return Response(
            {
                **EntitySerializer(entity).data,
                "changed": bool(change),
                "changed_keys": list(change.changed_keys) if change else [],
            },
            status=status.HTTP_201_CREATED if change else status.HTTP_200_OK,
        )


class EntityDetailView(APIView):
    """Address an entity the way the outside world thinks of it: type + id."""

    def _get(self, request, entity_type, external_id):
        return get_object_or_404(
            Entity,
            tenant=current_tenant(request),
            type=entity_type,
            external_id=external_id,
        )

    def get(self, request, entity_type, external_id):
        entity = self._get(request, entity_type, external_id)
        return Response(
            {
                **EntitySerializer(entity).data,
                "out_edges": EntityEdgeSerializer(
                    entity.out_edges.select_related("dst"), many=True
                ).data,
                "in_edges": EntityEdgeSerializer(
                    entity.in_edges.select_related("src"), many=True
                ).data,
            }
        )

    def patch(self, request, entity_type, external_id):
        self._get(request, entity_type, external_id)
        payload = PatchEntitySerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        entity, change = upsert_entity(
            current_tenant(request),
            entity_type,
            external_id,
            payload.validated_data["attrs"],
            replace=payload.validated_data.get("replace", False),
        )

        return Response(
            {
                **EntitySerializer(entity).data,
                "changed": bool(change),
                "changed_keys": list(change.changed_keys) if change else [],
            }
        )


class EntityLinkView(APIView):
    def post(self, request, entity_type, external_id):
        tenant = current_tenant(request)
        src = get_object_or_404(
            Entity, tenant=tenant, type=entity_type, external_id=external_id
        )
        dst = get_object_or_404(
            Entity,
            tenant=tenant,
            type=request.data["to_type"],
            external_id=request.data["to_external_id"],
        )
        edge = link(tenant, src, request.data["rel"], dst)
        return Response(EntityEdgeSerializer(edge).data, status=status.HTTP_201_CREATED)


class ChangeListView(APIView):
    """The outbox. Exposed because watching it drain is the demo."""

    def get(self, request):
        qs = EntityChange.objects.filter(
            tenant=current_tenant(request)
        ).select_related("entity").order_by("-id")

        if request.query_params.get("unprocessed") == "true":
            qs = qs.filter(processed_at__isnull=True)

        return Response(EntityChangeSerializer(qs[:50], many=True).data)


class TriggerViewSet(viewsets.ModelViewSet):
    serializer_class = TriggerSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return Trigger.objects.filter(
            tenant=current_tenant(self.request)
        ).select_related("definition")

    def create(self, request, *args, **kwargs):
        payload = CreateTriggerSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        tenant = current_tenant(request)
        definition = (
            WorkflowDef.objects.filter(tenant=tenant, name=data["workflow"])
            .order_by("-version")
            .first()
        )
        if definition is None:
            return Response(
                {"workflow": f"no workflow named {data['workflow']!r}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        trigger = Trigger.objects.create(
            tenant=tenant,
            name=data["name"],
            entity_type=data["entity_type"],
            predicate=data["predicate"],
            input_template=data.get("input_template") or {},
            definition=definition,
            enabled=data.get("enabled", True),
        )

        return Response(TriggerSerializer(trigger).data, status=status.HTTP_201_CREATED)
