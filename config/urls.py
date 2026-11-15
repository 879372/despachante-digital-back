from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("gestao/", admin.site.urls),
    path("api/", include("core.urls")),
]
