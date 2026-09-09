from django.urls import path

from . import views

app_name = "submissions"

urlpatterns = [
    path("", views.home, name="home"),
]
