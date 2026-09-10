from django.urls import path

from . import views

app_name = "submissions"

urlpatterns = [
    path("", views.home, name="home"),
    path("batch/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("batch/<int:pk>/status/", views.batch_status, name="batch_status"),
    path(
        "batch/<int:batch_pk>/url/<int:url_pk>/delete/",
        views.delete_url,
        name="delete_url",
    ),
]
