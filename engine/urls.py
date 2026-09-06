from django.urls import path
from rest_framework.routers import DefaultRouter

from engine.streaming import stream_run
from engine.views import RunViewSet, WorkflowDefViewSet

router = DefaultRouter()
router.register("workflows", WorkflowDefViewSet, basename="workflow")
router.register("runs", RunViewSet, basename="run")

urlpatterns = [
    # Declared before the router so it is not shadowed by runs/<pk>/.
    path("runs/<uuid:pk>/stream/", stream_run, name="run-stream"),
] + router.urls
