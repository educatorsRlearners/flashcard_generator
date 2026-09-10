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
    path("batch/<int:pk>/review/", views.card_review, name="card_review"),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/decision/",
        views.card_review_decision,
        name="card_review_decision",
    ),
    path(
        "batch/<int:pk>/review/finish/",
        views.card_review_finish,
        name="card_review_finish",
    ),
]
