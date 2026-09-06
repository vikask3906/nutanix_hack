from rest_framework.routers import DefaultRouter

from engine.views import RunViewSet, WorkflowDefViewSet

router = DefaultRouter()
router.register("workflows", WorkflowDefViewSet, basename="workflow")
router.register("runs", RunViewSet, basename="run")

urlpatterns = router.urls
