"""Tests for the non-HTML ``document`` extraction path (PDF / .docx).

No real network and no browser launch: ``urllib.request.urlopen`` is
monkeypatched to serve committed fixture bytes from ``tests/fixtures/``.
"""

import pathlib
from io import StringIO

import pytest
from django.core.management import call_command

from submissions import extraction
from submissions.models import Batch, SubmittedURL

pytestmark = pytest.mark.django_db

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PDF_CT = "application/pdf"
DOCX_CT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _fixture(name):
    return (FIXTURES / name).read_bytes()


class _Headers:
    def __init__(self, content_type):
        self._ct = content_type

    def get_content_type(self):
        return self._ct

    def get_content_charset(self):
        return None


class _Response:
    def __init__(self, body, content_type):
        self._body = body
        self.headers = _Headers(content_type)

    def read(self, amount=-1):
        if amount is None or amount < 0:
            return self._body
        return self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, mapping):
    """mapping: {url_substring: (body, content_type)}."""

    def fake_urlopen(request, timeout=None):
        for key, (body, ct) in mapping.items():
            if key in request.full_url:
                return _Response(body, ct)
        raise AssertionError(f"unexpected fetch: {request.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _no_browser(monkeypatch):
    def boom(url):
        raise AssertionError("render_browser must never run for documents")

    monkeypatch.setattr(extraction, "render_browser", boom)


def _run(*args):
    out = StringIO()
    call_command("extract_content", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def batch(db):
    return Batch.objects.create()


def _url(batch, href):
    return SubmittedURL.objects.create(url=href, batch=batch)


# --- PDF ------------------------------------------------------------------


def test_pdf_with_text_layer_ok(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"doc": (_fixture("text_layer.pdf"), PDF_CT)})
    row = _url(batch, "https://example.com/doc.pdf")

    output = _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.DOCUMENT
    assert row.extracted_title == "Arctic Tern Migration"
    assert "Arctic tern undertakes the longest" in row.extracted_text
    assert row.extracted_at is not None
    assert row.failure_kind == ""
    assert "document" in output


def test_pdf_octet_stream_sniffed_by_magic_bytes(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(
        monkeypatch,
        {"doc": (_fixture("text_layer.pdf"), "application/octet-stream")},
    )
    row = _url(batch, "https://example.com/doc.pdf")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.DOCUMENT


def test_image_only_pdf_fails_no_text(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"scan": (_fixture("image_only.pdf"), PDF_CT)})
    row = _url(batch, "https://example.com/scan.pdf")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == SubmittedURL.FailureKind.NO_CONTENT
    assert "no extractable text" in row.failure_reason
    assert "#19" in row.failure_reason
    assert row.extracted_text == ""
    assert row.extraction_method == SubmittedURL.ExtractionMethod.DOCUMENT


def test_encrypted_pdf_fails(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"locked": (_fixture("encrypted.pdf"), PDF_CT)})
    row = _url(batch, "https://example.com/locked.pdf")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert "encrypted" in row.failure_reason
    assert row.extracted_text == ""


def test_truncated_pdf_fails_and_command_continues(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(
        monkeypatch,
        {
            "broken": (_fixture("truncated.pdf"), PDF_CT),
            "good": (_fixture("text_layer.pdf"), PDF_CT),
        },
    )
    bad = _url(batch, "https://example.com/broken.pdf")
    good = _url(batch, "https://example.com/good.pdf")

    _run()  # no selector -> all rows; must exit 0

    bad.refresh_from_db()
    good.refresh_from_db()
    assert bad.status == SubmittedURL.Status.FAILED
    assert "could not parse document" in bad.failure_reason
    assert good.status == SubmittedURL.Status.OK


# --- docx ---------------------------------------------------------------


def test_docx_with_text_ok(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"report": (_fixture("body_text.docx"), DOCX_CT)})
    row = _url(batch, "https://example.com/report.docx")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.DOCUMENT
    assert row.extracted_title == "Long-Distance Flight"
    text = row.extracted_text
    assert "Arctic terns and the physics" in text
    assert text.index("first paragraph") < text.index("second paragraph")
    assert "fuelling stops, and navigation." in text


def test_corrupt_docx_fails(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"bad": (b"PK\x03\x04 not really a zip", DOCX_CT)})
    row = _url(batch, "https://example.com/bad.docx")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert "could not parse document" in row.failure_reason


# --- routing / unsupported --------------------------------------------


def test_unsupported_non_html_type_fails(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"archive": (b"PK\x03\x04zipdata", "application/zip")})
    row = _url(batch, "https://example.com/archive.zip")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == SubmittedURL.FailureKind.UNSUPPORTED_TYPE
    assert "application/zip" in row.failure_reason
    assert row.extraction_method == SubmittedURL.ExtractionMethod.NONE


def test_oversized_document_fails_too_large(monkeypatch, batch):
    big = b"%PDF-" + b"0" * (extraction.MAX_BODY_BYTES + 10)
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"huge": (big, PDF_CT)})
    row = _url(batch, "https://example.com/huge.pdf")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == SubmittedURL.FailureKind.TOO_LARGE


def test_rerun_force_overwrites_document(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"doc": (_fixture("text_layer.pdf"), PDF_CT)})
    row = _url(batch, "https://example.com/doc.pdf")
    _run("--url", row.url)
    row.refresh_from_db()
    first_at = row.extracted_at

    _serve(monkeypatch, {"doc": (_fixture("body_text.docx"), DOCX_CT)})
    _run("--url", row.url, "--force")

    assert SubmittedURL.objects.count() == 1
    row.refresh_from_db()
    assert row.extracted_title == "Long-Distance Flight"
    assert row.extracted_at >= first_at


def test_document_path_never_invokes_render_browser(monkeypatch, batch):
    calls = []
    monkeypatch.setattr(
        extraction, "render_browser", lambda url: calls.append(url) or ""
    )
    _serve(
        monkeypatch,
        {
            "a": (_fixture("text_layer.pdf"), PDF_CT),
            "b": (_fixture("image_only.pdf"), PDF_CT),
            "c": (_fixture("body_text.docx"), DOCX_CT),
        },
    )
    for name in ("a", "b", "c"):
        _url(batch, f"https://example.com/{name}")

    _run()

    assert calls == []


def test_parsers_are_callable_directly():
    text, title = extraction._extract_from_pdf(_fixture("text_layer.pdf"))
    assert title == "Arctic Tern Migration"
    assert "Arctic tern" in text

    with pytest.raises(extraction.PdfEncrypted):
        extraction._extract_from_pdf(_fixture("encrypted.pdf"))

    with pytest.raises(extraction.DocumentParseError):
        extraction._extract_from_pdf(_fixture("truncated.pdf"))

    dtext, dtitle = extraction._extract_from_docx(_fixture("body_text.docx"))
    assert dtitle == "Long-Distance Flight"
    assert "navigation." in dtext
