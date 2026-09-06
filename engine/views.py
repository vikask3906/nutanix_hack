from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from engine.models import Run, WorkflowDef
from engine.serializers import (
    RunDetailSerializer,
    RunSerializer,
    StartRunSerializer,
    WorkflowDefCreateSerializer,
    WorkflowDefSerializer,
)
from engine.service import create_definition, default_tenant, start_run
from engine.steps import registered_types


def current_tenant(request):
    """Resolve the tenant for this request.

    Block 6 replaces this with header/subdomain resolution and a scoped manager.
    Keeping it behind a function now means that change touches one place.
    """
    return default_tenant()


class WorkflowDefViewSet(viewsets.ModelViewSet):
    serializer_class = WorkflowDefSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return WorkflowDef.objects.filter(tenant=current_tenant(self.request))

    def create(self, request, *args, **kwargs):
        payload = WorkflowDefCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        definition = create_definition(
            current_tenant(request),
            payload.validated_data["spec"],
            payload.validated_data.get("description", ""),
        )

        return Response(
            WorkflowDefSerializer(definition).data, status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=["get"])
    def step_types(self, request):
        """What this deployment can execute. Reflects the plugin registry."""
        return Response({"step_types": registered_types()})


class RunViewSet(viewsets.ReadOnlyModelViewSet):
    def get_queryset(self):
        qs = Run.objects.filter(tenant=current_tenant(self.request)).select_related(
            "definition"
        )
        workflow = self.request.query_params.get("workflow")
        if workflow:
            qs = qs.filter(definition__name=workflow)
        run_status = self.request.query_params.get("status")
        if run_status:
            qs = qs.filter(status=run_status.upper())
        return qs

    def get_serializer_class(self):
        return RunDetailSerializer if self.action == "retrieve" else RunSerializer

    def create(self, request, *args, **kwargs):
        """Start a run.

        Returns as soon as the rows exist. Nothing is executed on this thread -
        a worker picks the first task up within its poll interval. That is why
        a three-second workflow and a three-day workflow are the same request.
        """
        payload = StartRunSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        tenant = current_tenant(request)
        definitions = WorkflowDef.objects.filter(tenant=tenant, name=data["workflow"])

        if data.get("version"):
            definition = get_object_or_404(definitions, version=data["version"])
        else:
            definition = definitions.order_by("-version").first()
            if definition is None:
                return Response(
                    {"workflow": f"no workflow named {data['workflow']!r}"},
                    status=status.HTTP_404_NOT_FOUND,
                )

        run = start_run(
            definition,
            run_input=data.get("input") or {},
            entity_ref=data.get("entity_ref", ""),
            trigger_source="api",
        )

        return Response(RunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        run = self.get_object()
        from engine.serializers import RunEventSerializer

        return Response(RunEventSerializer(run.events.all(), many=True).data)
