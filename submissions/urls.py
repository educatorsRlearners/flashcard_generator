from django.urls import path

from . import views

app_name = "submissions"

urlpatterns = [
    path("", views.home, name="home"),
    path("batch/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("batch/<int:pk>/status/", views.batch_status, name="batch_status"),
    path("batch/<int:pk>/events/", views.batch_events, name="batch_events"),
    path(
        "batch/<int:batch_pk>/url/<int:url_pk>/delete/",
        views.delete_url,
        name="delete_url",
    ),
    path("batch/<int:pk>/delete/", views.delete_batch, name="delete_batch"),
    path("clear/", views.clear_batches, name="clear_batches"),
    path("batch/<int:pk>/review/", views.card_review, name="card_review"),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/decision/",
        views.card_review_decision,
        name="card_review_decision",
    ),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/edit/",
        views.card_review_edit,
        name="card_review_edit",
    ),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/revert-edit/",
        views.card_review_revert_edit,
        name="card_review_revert_edit",
    ),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/image/candidates/",
        views.card_review_image_candidates,
        name="card_review_image_candidates",
    ),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/image/select/",
        views.card_review_image_select,
        name="card_review_image_select",
    ),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/image/regenerate/",
        views.card_review_image_regenerate,
        name="card_review_image_regenerate",
    ),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/image/remove/",
        views.card_review_image_remove,
        name="card_review_image_remove",
    ),
    path(
        "batch/<int:batch_pk>/review/card/<int:card_pk>/image/revert/",
        views.card_review_image_revert,
        name="card_review_image_revert",
    ),
    path(
        "batch/<int:pk>/review/finish/",
        views.card_review_finish,
        name="card_review_finish",
    ),
]
