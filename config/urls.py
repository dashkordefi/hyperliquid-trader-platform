from django.contrib import admin
from django.urls import include, path

from people.views import register

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("accounts/register/", register, name="register"),
    path("", include("trading.urls")),
]
