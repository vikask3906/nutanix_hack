from django.contrib import admin
from django.urls import include, path

from core.views import dashboard, healthz

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("api/", include("engine.urls")),
    path("api/", include("graph.urls")),
]
