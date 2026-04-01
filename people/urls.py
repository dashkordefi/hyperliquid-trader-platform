from django.urls import path

from .views import add_record, home

urlpatterns = [
    path("", home, name="home"),
    path("add/", add_record, name="add_record"),
]
