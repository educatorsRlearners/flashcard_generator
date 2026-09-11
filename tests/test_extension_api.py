"""Tests for the browser-extension JSON API (issue #35).

Caveat (documented here per the issue's constraints, mirroring how #33/#34
flag their own manual-only pieces): Django's test ``Client`` does not
enforce browser-side CORS at all. These tests can only assert that the
right ``Access-Control-*`` response headers are present/absent/correctly
valued - never that a real extension's preflight would actually succeed
against a real browser. Live, actual-browser CORS verification is a
separate manual QA step.
"""

import json

import pytest
from django.urls import reverse

from submissions.extension_auth import mint_token
from submissions.models import Batch, BatchRequest, SubmittedURL

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def token_file(tmp_path, settings):
    settings.EXTENSION_TOKEN_FILE = tmp_path / ".extension_token"


@pytest.fixture
def token(token_file):
    return mint_token()


@pytest.fixture(autouse=True)
def extension_id(settings):
    settings.EXTENSION_ID = "abcdefghijklmnop"


@pytest.fixture
def submit_url():
    return reverse("extension:submit")


def status_url(submitted_url_id):
    return reverse("extension:submission_status", kwargs={"submitted_url_id": submitted_url_id})


def auth_header(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


LONG_TEXT = "word " * 60  # well over MIN_CONTENT_CHARS (200) non-whitespace chars


# --- POST /api/extension/submit/ ---------------------------------------


def test_valid_submit_creates_rows_and_enqueues(client, submit_url, token):
    body = {"url": "https://example.com/page", "title": "A Title", "text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 202
    payload = resp.json()
    assert set(payload.keys()) == {"batch_id", "submitted_url_id"}

    batch = Batch.objects.get(pk=payload["batch_id"])
    submitted_url = SubmittedURL.objects.get(pk=payload["submitted_url_id"])
    assert submitted_url.url == "https://example.com/page"
    assert BatchRequest.objects.filter(batch=batch, submitted_url=submitted_url).exists()

    # Mirrors extraction._save_success field-for-field.
    assert submitted_url.extracted_text == LONG_TEXT
    assert submitted_url.extracted_title == "A Title"
    assert submitted_url.extraction_method == SubmittedURL.ExtractionMethod.EXTENSION
    assert submitted_url.extracted_at is not None
    assert submitted_url.status == SubmittedURL.Status.OK
    assert submitted_url.failure_kind == ""
    assert submitted_url.failure_reason == ""

    # process_extension_submission was enqueued and actually ran (Huey
    # immediate mode, autouse fixture) as a no-op, without error.


def test_valid_submit_title_optional_defaults_blank(client, submit_url, token):
    body = {"url": "https://example.com/no-title", "text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 202
    submitted_url = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert submitted_url.extracted_title == ""


def test_valid_submit_with_images_persists_candidates(client, submit_url, token):
    body = {
        "url": "https://example.com/with-images",
        "title": "T",
        "text": LONG_TEXT,
        "images": [
            "https://example.com/img/a.png",
            "https://cdn.example.com/photos/b.jpg",
        ],
    }
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 202
    submitted_url = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert submitted_url.extension_image_urls == [
        "https://example.com/img/a.png",
        "https://cdn.example.com/photos/b.jpg",
    ]


def test_submit_without_images_defaults_to_empty(client, submit_url, token):
    body = {"url": "https://example.com/no-images", "text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 202
    submitted_url = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert submitted_url.extension_image_urls == []


def test_submit_null_images_defaults_to_empty(client, submit_url, token):
    body = {
        "url": "https://example.com/null-images",
        "text": LONG_TEXT,
        "images": None,
    }
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 202
    submitted_url = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert submitted_url.extension_image_urls == []


def test_submit_drops_malformed_image_entries_individually(
    client, submit_url, token
):
    body = {
        "url": "https://example.com/mixed-images",
        "text": LONG_TEXT,
        "images": [
            "https://example.com/good.png",
            123,
            None,
            {"url": "https://example.com/obj.png"},
            "ftp://example.com/not-http.png",
            "data:image/png;base64,AAA",
            "",
            "/relative/path.png",
            "  https://example.com/spaced.png  ",
        ],
    }
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 202
    submitted_url = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert submitted_url.extension_image_urls == [
        "https://example.com/good.png",
        "https://example.com/spaced.png",
    ]


def test_submit_missing_auth_header_401(client, submit_url):
    body = {"url": "https://example.com/page", "text": LONG_TEXT}
    resp = client.post(submit_url, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_submit_wrong_scheme_401(client, submit_url, token):
    body = {"url": "https://example.com/page", "text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Token {token}",
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_submit_bad_token_401(client, submit_url, token):
    body = {"url": "https://example.com/page", "text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header("wrong-token"),
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_submit_malformed_json_400(client, submit_url, token):
    resp = client.post(
        submit_url,
        data="not json{{",
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid JSON body"}


def test_submit_missing_text_400(client, submit_url, token):
    body = {"url": "https://example.com/page"}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "text too short", "min_chars": 200}
    assert SubmittedURL.objects.count() == 0


def test_submit_too_short_text_400(client, submit_url, token):
    body = {"url": "https://example.com/page", "text": "too short"}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "text too short", "min_chars": 200}
    assert SubmittedURL.objects.count() == 0


def test_submit_invalid_url_400(client, submit_url, token):
    body = {"url": "not-a-url", "text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid or missing url"}
    assert SubmittedURL.objects.count() == 0


def test_submit_missing_url_400(client, submit_url, token):
    body = {"text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid or missing url"}


def test_double_submit_creates_new_batch_and_overwrites_fields(client, submit_url, token):
    first_body = {"url": "https://example.com/dup", "title": "First", "text": LONG_TEXT}
    second_text = "second " * 60
    second_body = {"url": "https://example.com/dup", "title": "Second", "text": second_text}

    first_resp = client.post(
        submit_url,
        data=json.dumps(first_body),
        content_type="application/json",
        **auth_header(token),
    )
    second_resp = client.post(
        submit_url,
        data=json.dumps(second_body),
        content_type="application/json",
        **auth_header(token),
    )

    assert first_resp.status_code == 202
    assert second_resp.status_code == 202
    assert first_resp.json()["submitted_url_id"] == second_resp.json()["submitted_url_id"]
    assert first_resp.json()["batch_id"] != second_resp.json()["batch_id"]

    assert SubmittedURL.objects.count() == 1
    assert Batch.objects.count() == 2
    assert BatchRequest.objects.count() == 2

    submitted_url = SubmittedURL.objects.get(pk=first_resp.json()["submitted_url_id"])
    assert submitted_url.extracted_text == second_text
    assert submitted_url.extracted_title == "Second"


def test_submit_options_preflight(client, submit_url):
    resp = client.options(submit_url)
    assert resp.status_code == 200
    assert not resp.content
    assert resp["Access-Control-Allow-Origin"] == "chrome-extension://abcdefghijklmnop"
    assert resp["Access-Control-Allow-Methods"] == "POST, OPTIONS"
    assert resp["Access-Control-Allow-Headers"] == "Authorization, Content-Type"


def test_submit_response_has_cors_header_when_extension_id_set(client, submit_url, token):
    body = {"url": "https://example.com/cors", "text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert resp["Access-Control-Allow-Origin"] == "chrome-extension://abcdefghijklmnop"


def test_submit_no_cors_header_when_extension_id_unset(client, submit_url, token, settings):
    settings.EXTENSION_ID = ""
    body = {"url": "https://example.com/no-cors", "text": LONG_TEXT}
    resp = client.post(
        submit_url,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )
    assert "Access-Control-Allow-Origin" not in resp


# --- GET /api/extension/submit/<id>/status/ -----------------------------


def test_status_missing_auth_401(client):
    resp = client.get(status_url(1))
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_status_unknown_id_404_json(client, token):
    resp = client.get(status_url(999999), **auth_header(token))
    assert resp.status_code == 404
    assert resp["Content-Type"].startswith("application/json")
    assert resp.json() == {"error": "not found"}


def test_status_options_preflight(client):
    resp = client.options(status_url(1))
    assert resp.status_code == 200
    assert not resp.content
    assert resp["Access-Control-Allow-Origin"] == "chrome-extension://abcdefghijklmnop"
    assert resp["Access-Control-Allow-Methods"] == "GET, OPTIONS"
    assert resp["Access-Control-Allow-Headers"] == "Authorization, Content-Type"


def test_status_not_started(client, token):
    batch = Batch.objects.create()
    submitted_url = SubmittedURL.objects.create(
        url="https://example.com/pending", batch=batch, status=SubmittedURL.Status.OK
    )
    BatchRequest.objects.create(batch=batch, submitted_url=submitted_url)

    resp = client.get(status_url(submitted_url.pk), **auth_header(token))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload == {
        "submitted_url_id": submitted_url.pk,
        "url": submitted_url.url,
        "status": "ok",
        "generation_status": "",
        "generation_error": "",
        "terminal": False,
        "review_url": None,
    }


def test_status_generation_ok_review_url_from_latest_batch_request(client, token):
    origin_batch = Batch.objects.create()
    submitted_url = SubmittedURL.objects.create(
        url="https://example.com/ok",
        batch=origin_batch,
        status=SubmittedURL.Status.OK,
        generation_status=SubmittedURL.GenerationStatus.OK,
    )
    BatchRequest.objects.create(batch=origin_batch, submitted_url=submitted_url)

    # A later re-submission batch - the most recent BatchRequest, not
    # submitted_url.batch, should back review_url.
    latest_batch = Batch.objects.create()
    BatchRequest.objects.create(batch=latest_batch, submitted_url=submitted_url)

    resp = client.get(status_url(submitted_url.pk), **auth_header(token))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["terminal"] is True
    expected_review_url = reverse("submissions:card_review", kwargs={"pk": latest_batch.pk})
    assert payload["review_url"].endswith(expected_review_url)
    assert payload["review_url"].startswith("http")


def test_status_generation_failed_terminal_no_review_url(client, token):
    batch = Batch.objects.create()
    submitted_url = SubmittedURL.objects.create(
        url="https://example.com/gen-failed",
        batch=batch,
        status=SubmittedURL.Status.OK,
        generation_status=SubmittedURL.GenerationStatus.FAILED,
        generation_error="insufficient content",
    )
    BatchRequest.objects.create(batch=batch, submitted_url=submitted_url)

    resp = client.get(status_url(submitted_url.pk), **auth_header(token))
    payload = resp.json()
    assert payload["terminal"] is True
    assert payload["review_url"] is None
    assert payload["generation_status"] == "failed"
    assert payload["generation_error"] == "insufficient content"


def test_status_extraction_failed_terminal_no_review_url(client, token):
    batch = Batch.objects.create()
    submitted_url = SubmittedURL.objects.create(
        url="https://example.com/extract-failed",
        batch=batch,
        status=SubmittedURL.Status.FAILED,
        failure_kind=SubmittedURL.FailureKind.DNS,
        failure_reason="host not found",
    )
    BatchRequest.objects.create(batch=batch, submitted_url=submitted_url)

    resp = client.get(status_url(submitted_url.pk), **auth_header(token))
    payload = resp.json()
    assert payload["status"] == "failed"
    assert payload["generation_status"] == ""
    assert payload["terminal"] is True
    assert payload["review_url"] is None
