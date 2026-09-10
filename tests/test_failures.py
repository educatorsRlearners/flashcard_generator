"""Tests for the categorised failure taxonomy (issue #4).

No real network calls and no real browser launch: ``fetch_static`` and
``render_browser`` are monkeypatched and every failure condition is simulated
by raising the exception the real code path would raise.
"""

import socket
import urllib.error
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from submissions import extraction
from submissions.extraction import classify_failure
from submissions.models import Batch, BatchRequest, SubmittedURL

pytestmark = pytest.mark.django_db

Kind = SubmittedURL.FailureKind

_SENTENCE = (
    "The migratory patterns of arctic terns span roughly seventy thousand "
    "kilometres each year as the birds trace a winding path between polar "
    "feeding grounds. "
)


def _article_html(title="A Real Article", paragraphs=6):
    body = "".join(f"<p>{_SENTENCE * 3}</p>" for _ in range(paragraphs))
    return (
        f"<html><head><title>{title}</title></head><body>"
        f"<article><h1>{title}</h1>{body}</article></body></html>"
    )


def _http_error(code):
    return urllib.error.HTTPError(
        "https://example.com/x", code, f"HTTP {code}", {}, None
    )


# --- The single mapping: one simulated condition per failure_kind ----------

_CONDITIONS = {
    Kind.DNS: socket.gaierror(8, "nodename nor servname provided, or not known"),
    Kind.CONNECTION: ConnectionRefusedError(61, "Connection refused"),
    Kind.HTTP_CLIENT: _http_error(404),
    Kind.BLOCKED: _http_error(403),
    Kind.TIMEOUT: socket.timeout("timed out"),
    Kind.TOO_LARGE: extraction.BodyTooLarge(),
    Kind.UNSUPPORTED_TYPE: extraction.UnsupportedContentType("application/pdf"),
    Kind.NO_CONTENT: extraction.NoExtractableContent(),
    Kind.UNKNOWN: _http_error(500),
}


@pytest.mark.parametrize("expected_kind,exc", list(_CONDITIONS.items()))
def test_every_failure_kind_has_a_simulated_condition(expected_kind, exc):
    kind, reason = classify_failure(exc, url="https://host.example.com/p")
    assert kind == expected_kind
    assert reason  # always a non-empty one-line explanation


def test_classify_includes_specific_detail():
    assert classify_failure(_http_error(404))[1] == "HTTP 404"
    assert classify_failure(_http_error(429))[1] == "HTTP 429 (rate limited)"
    assert (
        classify_failure(
            socket.gaierror(8, "not known"), url="https://nope.invalid/x"
        )[1]
        == "host not found: nope.invalid"
    )
    assert classify_failure(extraction.BodyTooLarge())[1] == (
        "response exceeded 10 MB cap"
    )


def test_browser_timeout_classifies_as_timeout():
    kind, reason = classify_failure(
        extraction.BrowserError(TimeoutError("navigation timeout exceeded"))
    )
    assert kind == Kind.TIMEOUT


# --- End-to-end through the command --------------------------------------


def _run(*args):
    out = StringIO()
    call_command("extract_content", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def batch():
    return Batch.objects.create()


def _url(batch, href, **kwargs):
    row = SubmittedURL.objects.create(url=href, batch=batch, **kwargs)
    BatchRequest.objects.create(batch=batch, submitted_url=row)
    return row


def test_command_continues_past_failure_and_exits_zero(monkeypatch, batch):
    good_html = _article_html()

    def fake_fetch(url):
        if "bad" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return good_html

    monkeypatch.setattr(extraction, "fetch_static", fake_fetch)
    bad = _url(batch, "https://bad.example.com/x")
    good = _url(batch, "https://good.example.com/y")

    output = _run()  # no selector, returns None -> exit 0

    bad.refresh_from_db()
    good.refresh_from_db()
    assert bad.status == SubmittedURL.Status.FAILED
    assert bad.failure_kind == Kind.HTTP_CLIENT
    assert good.status == SubmittedURL.Status.OK
    assert good.failure_kind == ""
    assert "[http_client]" in output
    assert "HTTP 404" in output


def test_batch_detail_shows_failure_kind_and_reason(client, monkeypatch, batch):
    monkeypatch.setattr(
        extraction,
        "fetch_static",
        lambda url: (_ for _ in ()).throw(
            urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        ),
    )
    row = _url(batch, "https://blocked.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.failure_kind == Kind.BLOCKED

    resp = client.get(reverse("submissions:batch_detail", args=[batch.pk]))
    body = resp.content.decode()
    assert resp.status_code == 200
    assert row.get_failure_kind_display() in body
    assert "HTTP 403 (access refused)" in body
    assert "1 failed: 1 blocked" in body


def test_batch_detail_renders_when_all_failed_and_when_empty(client):
    empty = Batch.objects.create()
    resp = client.get(reverse("submissions:batch_detail", args=[empty.pk]))
    assert resp.status_code == 200

    failed_batch = Batch.objects.create()
    _url(
        failed_batch,
        "https://a.example.com/x",
        status=SubmittedURL.Status.FAILED,
        failure_kind=Kind.TIMEOUT,
        failure_reason="request timed out",
    )
    resp = client.get(
        reverse("submissions:batch_detail", args=[failed_batch.pk])
    )
    assert resp.status_code == 200
    assert "1 failed: 1 timeout" in resp.content.decode()


def test_previously_failed_row_that_now_succeeds_is_cleared(monkeypatch, batch):
    row = _url(batch, "https://flaky.example.com/x")

    monkeypatch.setattr(
        extraction,
        "fetch_static",
        lambda url: (_ for _ in ()).throw(socket.timeout("timed out")),
    )
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.TIMEOUT

    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.failure_kind == ""
    assert row.failure_reason == ""
    assert SubmittedURL.objects.filter(url="https://flaky.example.com/x").count() == 1


def test_failed_before_selection_keeps_method_none_and_is_retried(
    monkeypatch, batch
):
    row = _url(batch, "https://retry.example.com/x")
    monkeypatch.setattr(
        extraction,
        "fetch_static",
        lambda url: (_ for _ in ()).throw(socket.gaierror(8, "not known")),
    )
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.extraction_method == SubmittedURL.ExtractionMethod.NONE
    assert row.failure_kind == Kind.DNS

    # A plain re-run (no selector) selects method == "none" rows, so it retries.
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    _run()
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
