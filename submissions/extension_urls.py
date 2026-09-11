from django.urls import path

from . import extension_api

app_name = "extension"

urlpatterns = [
    path("submit/", extension_api.submit, name="submit"),
    path(
        "submit/<int:submitted_url_id>/status/",
        extension_api.submission_status,
        name="submission_status",
    ),
]
