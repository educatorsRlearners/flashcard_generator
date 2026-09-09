"""Tests for submissions.extraction and the extract_content command.

No real network calls and no real browser launch: the stdlib HTTP fetch is
monkeypatched at ``urllib.request.urlopen`` (or at ``fetch_static`` /
``render_browser``) and HTML inputs are local fixture strings.
"""

import socket
import urllib.error
from io import StringIO

import pytest
from django.core.management import call_command

from submissions import extraction
from submissions.models import Batch, SubmittedURL

pytestmark = pytest.mark.django_db


# --- Fixtures ---------------------------------------------------------------

_SENTENCE = (
    "The migratory patterns of arctic terns span roughly seventy thousand "
    "kilometres each year as the birds trace a winding path between polar "
    "feeding grounds. "
)


def _article_html(title="The Great Article Title", paragraphs=6):
    body = "".join(f"<p>{_SENTENCE * 3}</p>" for _ in range(paragraphs))
    return (
        f"<html><head><title>{title}</title></head><body>"
        "<nav>Home About Contact Login Signup NAVIGATION MENU JUNK</nav>"
        f"<article><h1>{title}</h1>{body}</article>"
        "<footer>Copyright 2026 BoilerplateCorp ALL RIGHTS RESERVED FOOTER</footer>"
        "</body></html>"
    )


_JS_ONLY_HTML = (
    "<html><head><title>JS App</title></head>"
    "<body><div id='root'></div><noscript>Enable JS</noscript></body></html>"
)


class _FakeHeaders:
    def __init__(self, content_type="text/html", charset="utf-8"):
        self._ct = content_type
        self._cs = charset

    def get_content_type(self):
        return self._ct

    def get_content_charset(self):
        return self._cs


class _FakeResponse:
    def __init__(self, body=b"", content_type="text/html", charset="utf-8"):
        self._body = body
        self.headers = _FakeHeaders(content_type, charset)

    def read(self, amount=-1):
        if amount is None or amount < 0:
            return self._body
        return self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, factory):
    def fake_urlopen(request, timeout=None):
        return factory(request)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _run(*args):
    out = StringIO()
    call_command("extract_content", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def batch(db):
    return Batch.objects.create()


def _url(batch, href):
    return SubmittedURL.objects.create(url=href, batch=batch)


# --- Tests -----------------------------------------------------------------


def test_static_extraction_success(monkeypatch, batch):
    html = _article_html().encode("utf-8")
    _patch_urlopen(monkeypatch, lambda req: _FakeResponse(html))
    row = _url(batch, "https://example.com/article")

    output = _run("--url", row.url)

    row.refresh_from_db()
    assert row.extraction_method == SubmittedURL.ExtractionMethod.STATIC
    assert row.status == SubmittedURL.Status.OK
    assert row.extracted_at is not None
    assert row.extracted_title == "The Great Article Title"
    assert "arctic terns" in row.extracted_text
    assert "NAVIGATION MENU" not in row.extracted_text
    assert "FOOTER" not in row.extracted_text
    assert "static" in output


def test_fallback_to_browser_succeeds(monkeypatch, batch):
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _JS_ONLY_HTML)
    monkeypatch.setattr(
        extraction, "render_browser", lambda url: _article_html("Rendered Title")
    )
    row = _url(batch, "https://example.com/spa")

    output = _run("--url", row.url)

    row.refresh_from_db()
    assert row.extraction_method == SubmittedURL.ExtractionMethod.BROWSER
    assert row.status == SubmittedURL.Status.OK
    assert "arctic terns" in row.extracted_text
    assert "browser" in output


def test_both_paths_under_threshold_fails(monkeypatch, batch):
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _JS_ONLY_HTML)
    monkeypatch.setattr(
        extraction, "render_browser", lambda url: "<html><body><p>tiny</p></body></html>"
    )
    row = _url(batch, "https://example.com/empty")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert "no extractable content" in row.failure_reason
    assert row.extraction_method == SubmittedURL.ExtractionMethod.BROWSER
    assert row.extracted_text == ""


def test_non_html_content_type_fails(monkeypatch, batch):
    _patch_urlopen(
        monkeypatch,
        lambda req: _FakeResponse(b"%PDF-1.7", content_type="application/pdf"),
    )
    called = []
    monkeypatch.setattr(
        extraction, "render_browser", lambda url: called.append(url) or ""
    )
    row = _url(batch, "https://example.com/doc.pdf")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert "application/pdf" in row.failure_reason
    assert row.extraction_method == SubmittedURL.ExtractionMethod.NONE
    assert called == []


def test_network_error_fails_and_command_continues(monkeypatch, batch):
    good_html = _article_html().encode("utf-8")

    def factory(request):
        if "bad" in request.full_url:
            raise urllib.error.URLError("name resolution failed")
        return _FakeResponse(good_html)

    _patch_urlopen(monkeypatch, factory)
    bad = _url(batch, "https://bad.example.com/x")
    good = _url(batch, "https://good.example.com/y")

    # No selector -> processes all rows with method 'none'; must exit 0.
    _run()

    bad.refresh_from_db()
    good.refresh_from_db()
    assert bad.status == SubmittedURL.Status.FAILED
    assert bad.failure_reason == "connection error"
    assert good.status == SubmittedURL.Status.OK


def test_timeout_fails(monkeypatch, batch):
    def factory(request):
        raise socket.timeout("timed out")

    _patch_urlopen(monkeypatch, factory)
    row = _url(batch, "https://slow.example.com/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_reason == "timeout"


def test_response_too_large_fails(monkeypatch, batch):
    huge = b"x" * (extraction.MAX_BODY_BYTES + 10)
    _patch_urlopen(monkeypatch, lambda req: _FakeResponse(huge))
    row = _url(batch, "https://example.com/huge")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_reason == "response too large"


def test_rerun_overwrites_in_place(monkeypatch, batch):
    first = _article_html("First Title").encode("utf-8")
    _patch_urlopen(monkeypatch, lambda req: _FakeResponse(first))
    row = _url(batch, "https://example.com/changing")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.extracted_title == "First Title"
    first_at = row.extracted_at

    second = _article_html("Second Title").encode("utf-8")
    _patch_urlopen(monkeypatch, lambda req: _FakeResponse(second))
    _run("--url", row.url, "--force")

    assert SubmittedURL.objects.count() == 1
    row.refresh_from_db()
    assert row.extracted_title == "Second Title"
    assert row.extracted_at >= first_at


def test_batch_selector_processes_all_urls(monkeypatch, batch):
    html = _article_html()
    monkeypatch.setattr(extraction, "fetch_static", lambda url: html)
    a = _url(batch, "https://example.com/a")
    b = _url(batch, "https://example.com/b")
    other_batch = Batch.objects.create()
    c = _url(other_batch, "https://example.com/c")

    _run("--batch", str(batch.pk))

    a.refresh_from_db()
    b.refresh_from_db()
    c.refresh_from_db()
    assert a.status == SubmittedURL.Status.OK
    assert b.status == SubmittedURL.Status.OK
    assert c.status == SubmittedURL.Status.PENDING


def test_batch_rerun_skips_finished_without_force(monkeypatch, batch):
    html = _article_html()
    monkeypatch.setattr(extraction, "fetch_static", lambda url: html)
    done = _url(batch, "https://example.com/done")
    done.extraction_method = SubmittedURL.ExtractionMethod.STATIC
    done.status = SubmittedURL.Status.OK
    done.extracted_title = "Kept Title"
    done.save()
    fresh = _url(batch, "https://example.com/fresh")

    output = _run("--batch", str(batch.pk))

    fresh.refresh_from_db()
    done.refresh_from_db()
    assert fresh.status == SubmittedURL.Status.OK
    assert done.extracted_title == "Kept Title"
    assert "skipped" in output
