from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path


def health(_request):
    """Проверка, что процесс жив (Render / балансировщик); без обращения к БД."""
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("trading.urls")),
]
