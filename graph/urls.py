from django.urls import path
from rest_framework.routers import DefaultRouter

from graph.views import (
    ChangeListView,
    EntityDetailView,
    EntityLinkView,
    EntityListView,
    TriggerViewSet,
)

router = DefaultRouter()
router.register("triggers", TriggerViewSet, basename="trigger")

urlpatterns = [
    path("entities/", EntityListView.as_view(), name="entity-list"),
    path(
        "entities/<str:entity_type>/<str:external_id>/",
        EntityDetailView.as_view(),
        name="entity-detail",
    ),
    path(
        "entities/<str:entity_type>/<str:external_id>/link/",
        EntityLinkView.as_view(),
        name="entity-link",
    ),
    path("changes/", ChangeListView.as_view(), name="change-list"),
] + router.urls
