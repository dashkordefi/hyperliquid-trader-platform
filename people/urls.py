from django.urls import path

from .views import add_record, home, register

urlpatterns = [
    path("", home, name="home"),
    path("add/", add_record, name="add_record"),
    path("accounts/register/", register, name="register"),
]
