from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

from people.views import register


def health(_request):
    """Проверка, что процесс жив (Render / балансировщик); без обращения к БД."""
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    # До include(auth): иначе «register/» уходит в auth.urls и даёт 404.
    path("accounts/register/", register, name="register"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("trading.urls")),
]
