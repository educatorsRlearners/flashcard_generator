from django.urls import path

from . import views

app_name = "submissions"

urlpatterns = [
    path("", views.home, name="home"),
    path("batch/<int:pk>/", views.batch_detail, name="batch_detail"),
]
