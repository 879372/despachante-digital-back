from django.contrib import admin
from django.urls import include, path
from core.health import health

urlpatterns = [
    path("api/health/", health),
    path("gestao/", admin.site.urls),
    path("api/", include("core.urls")),
]
