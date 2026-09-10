"""Tests for the image-OCR extraction path (issue #19).

No real network, no Tesseract, no browser: ``urllib.request.urlopen`` is
monkeypatched to serve committed fixture bytes from ``tests/fixtures/``,
and the native OCR seam (``_ocr_image`` / ``_pdf_page_images`` /
``_ocr_available``) is stubbed unless a test explicitly exercises the live
toolchain. Tests needing the real toolchain SKIP (not fail) when it is
absent, so the suite passes from a clean checkout.
"""

import pathlib
import time
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from submissions import extraction
from submissions.models import Batch, SubmittedURL

pytestmark = pytest.mark.django_db

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PNG_CT = "image/png"
JPEG_CT = "image/jpeg"
PDF_CT = "application/pdf"

LONG_TEXT = " ".join(["Arctic tern migration route"] * 30)
OTHER_TEXT = " ".join(["Antarctic feeding grounds circuit"] * 30)

LIVE = extraction._ocr_available()
needs_live_ocr = pytest.mark.skipif(
    not LIVE, reason="OCR toolchain absent (needs Tesseract + pytesseract)"
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
        raise AssertionError("render_browser must never run for OCR content")

    monkeypatch.setattr(extraction, "render_browser", boom)


def _stub_ocr(monkeypatch, available=True, text_fn=None):
    """Fake the toolchain as present and stub recognition.

    ``text_fn`` maps a call index -> transcript; defaults to LONG_TEXT.
    """
    monkeypatch.setattr(extraction, "_ocr_available", lambda: available)
    calls = []

    def fake_ocr(image):
        calls.append(image)
        if text_fn is not None:
            return text_fn(len(calls) - 1)
        return LONG_TEXT

    monkeypatch.setattr(extraction, "_ocr_image", fake_ocr)
    return calls


def _run(*args):
    out = StringIO()
    call_command("extract_content", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def batch(db):
    return Batch.objects.create()


def _url(batch, href):
    return SubmittedURL.objects.create(url=href, batch=batch)


# --- live toolchain -------------------------------------------------------


@needs_live_ocr
def test_live_text_image_ok(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"photo": (_fixture("ocr_text.png"), PNG_CT)})
    row = _url(batch, "https://example.com/photo.png")

    output = _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR
    assert row.extracted_title == ""
    assert row.extracted_at is not None
    assert (
        extraction._non_whitespace_len(row.extracted_text)
        >= extraction.MIN_CONTENT_CHARS
    )
    assert "ocr" in output


@needs_live_ocr
def test_live_blank_image_no_readable_text(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"blank": (_fixture("ocr_blank.png"), PNG_CT)})
    row = _url(batch, "https://example.com/blank.png")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == SubmittedURL.FailureKind.NO_CONTENT
    assert "no readable text" in row.failure_reason
    assert row.extracted_text == ""
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR


# --- routing --------------------------------------------------------------


def test_png_content_type_routes_to_ocr(monkeypatch, batch):
    _no_browser(monkeypatch)
    _stub_ocr(monkeypatch)
    _serve(monkeypatch, {"photo": (_fixture("ocr_text.png"), PNG_CT)})
    row = _url(batch, "https://example.com/photo.png")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR
    assert row.extracted_title == ""
    assert row.extracted_at is not None
    assert "migration route" in row.extracted_text


def test_jpeg_content_type_routes_to_ocr(monkeypatch, batch):
    _no_browser(monkeypatch)
    _stub_ocr(monkeypatch)
    _serve(monkeypatch, {"photo": (_fixture("ocr_text.png"), JPEG_CT)})
    row = _url(batch, "https://example.com/photo.jpg")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR


def test_octet_stream_with_matching_extension_and_magic_bytes_routes_to_ocr(
    monkeypatch, batch
):
    _no_browser(monkeypatch)
    _stub_ocr(monkeypatch)
    _serve(
        monkeypatch,
        {"photo": (_fixture("ocr_text.png"), "application/octet-stream")},
    )
    row = _url(batch, "https://example.com/photo.png")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR


def test_octet_stream_without_image_extension_stays_unsupported(
    monkeypatch, batch
):
    _no_browser(monkeypatch)
    _serve(
        monkeypatch,
        {"file": (_fixture("ocr_text.png"), "application/octet-stream")},
    )
    row = _url(batch, "https://example.com/file.bin")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == SubmittedURL.FailureKind.UNSUPPORTED_TYPE
    assert row.extraction_method == SubmittedURL.ExtractionMethod.NONE


def test_sniff_magic_bytes_unit():
    assert extraction._sniff_image_format(b"\x89PNG\r\n\x1a\n" + b"0" * 10) == "png"
    assert extraction._sniff_image_format(b"\xff\xd8\xff" + b"0" * 10) == "jpeg"
    assert (
        extraction._sniff_image_format(b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 4)
        == "webp"
    )
    assert extraction._sniff_image_format(b"II*\x00" + b"0" * 10) == "tiff"
    assert extraction._sniff_image_format(b"MM\x00*" + b"0" * 10) == "tiff"
    assert extraction._sniff_image_format(b"%PDF-1.4") is None
    assert extraction._sniff_image_format(b"not an image") is None


# --- OCR outcomes ---------------------------------------------------------


def test_blank_image_fails_no_readable_text(monkeypatch, batch):
    _no_browser(monkeypatch)
    _stub_ocr(monkeypatch, text_fn=lambda _i: "   \n  ")
    _serve(monkeypatch, {"blank": (_fixture("ocr_blank.png"), PNG_CT)})
    row = _url(batch, "https://example.com/blank.png")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == SubmittedURL.FailureKind.NO_CONTENT
    assert "no readable text" in row.failure_reason
    assert row.extracted_text == ""
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR


def test_scanned_pdf_pages_concatenated_in_order(monkeypatch, batch):
    from PIL import Image

    _no_browser(monkeypatch)
    monkeypatch.setattr(extraction, "_ocr_available", lambda: True)
    pages = [Image.new("RGB", (10, 10), "white") for _ in range(2)]
    monkeypatch.setattr(extraction, "_pdf_page_images", lambda body: pages)
    transcripts = ["PAGE ONE MARKER " + LONG_TEXT, "PAGE TWO MARKER " + LONG_TEXT]
    by_id = {id(page): text for page, text in zip(pages, transcripts)}
    monkeypatch.setattr(extraction, "_ocr_image", lambda image: by_id[id(image)])
    _serve(monkeypatch, {"scan": (_fixture("ocr_scan.pdf"), PDF_CT)})
    row = _url(batch, "https://example.com/scan.pdf")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR
    assert row.extracted_title == ""
    assert "PAGE ONE MARKER" in row.extracted_text
    assert "PAGE TWO MARKER" in row.extracted_text
    assert row.extracted_text.index("PAGE ONE MARKER") < row.extracted_text.index(
        "PAGE TWO MARKER"
    )


def test_image_only_pdf_handoff_uses_ocr_method(monkeypatch, batch):
    _no_browser(monkeypatch)
    _stub_ocr(monkeypatch)
    monkeypatch.setattr(
        extraction, "_pdf_page_images", lambda body: [_stub_page()]
    )
    _serve(monkeypatch, {"scan": (_fixture("image_only.pdf"), PDF_CT)})
    row = _url(batch, "https://example.com/scan.pdf")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR


def test_corrupt_image_fails_and_command_continues(monkeypatch, batch):
    _no_browser(monkeypatch)
    _stub_ocr(monkeypatch)
    _serve(
        monkeypatch,
        {
            "broken": (b"not an image at all\x00\xff\xfe", PNG_CT),
            "good": (_fixture("ocr_text.png"), PNG_CT),
        },
    )
    bad = _url(batch, "https://example.com/broken.png")
    good = _url(batch, "https://example.com/good.png")

    _run()  # no selector -> all rows; must exit 0

    bad.refresh_from_db()
    good.refresh_from_db()
    assert bad.status == SubmittedURL.Status.FAILED
    assert "could not read image" in bad.failure_reason
    assert bad.extraction_method == SubmittedURL.ExtractionMethod.OCR
    assert good.status == SubmittedURL.Status.OK


def test_ocr_timeout_fails_cleanly(monkeypatch, batch):
    _no_browser(monkeypatch)
    monkeypatch.setattr(extraction, "_ocr_available", lambda: True)

    def hang(image):
        time.sleep(30)

    monkeypatch.setattr(extraction, "_ocr_image", hang)
    _serve(monkeypatch, {"slow": (_fixture("ocr_text.png"), PNG_CT)})
    row = _url(batch, "https://example.com/slow.png")

    with override_settings(OCR_TIMEOUT_SECONDS=0.2):
        _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == SubmittedURL.FailureKind.TIMEOUT
    assert "timed out" in row.failure_reason
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR


def test_toolchain_absent_fails_with_setup_hint_and_continues(
    monkeypatch, batch
):
    _no_browser(monkeypatch)
    monkeypatch.setattr(extraction, "pytesseract", None)
    monkeypatch.setattr(extraction.shutil, "which", lambda _name: None)
    _serve(
        monkeypatch,
        {
            "photo": (_fixture("ocr_text.png"), PNG_CT),
            "other": (_fixture("ocr_text.png"), PNG_CT),
        },
    )
    first = _url(batch, "https://example.com/photo.png")
    second = _url(batch, "https://example.com/other.png")

    _run()  # must exit 0, no traceback

    for row in (first, second):
        row.refresh_from_db()
        assert row.status == SubmittedURL.Status.FAILED
        assert "OCR" in row.failure_reason
        assert "Image OCR" in row.failure_reason
        assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR
        assert row.extracted_text == ""


def test_ocr_disabled_via_settings(monkeypatch, batch):
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"photo": (_fixture("ocr_text.png"), PNG_CT)})
    row = _url(batch, "https://example.com/photo.png")

    with override_settings(OCR_ENABLED=False):
        _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert "OCR" in row.failure_reason
    assert row.extraction_method == SubmittedURL.ExtractionMethod.OCR


# --- caps / rerun ---------------------------------------------------------


def test_oversized_image_fails_too_large(monkeypatch, batch):
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (extraction.MAX_BODY_BYTES + 10)
    _no_browser(monkeypatch)
    _serve(monkeypatch, {"huge": (big, PNG_CT)})
    row = _url(batch, "https://example.com/huge.png")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == SubmittedURL.FailureKind.TOO_LARGE


def test_rerun_force_overwrites_ocr(monkeypatch, batch):
    _no_browser(monkeypatch)
    calls = _stub_ocr(monkeypatch, text_fn=lambda i: [LONG_TEXT, OTHER_TEXT][min(i, 1)])
    _serve(monkeypatch, {"photo": (_fixture("ocr_text.png"), PNG_CT)})
    row = _url(batch, "https://example.com/photo.png")
    _run("--url", row.url)
    row.refresh_from_db()
    assert "migration route" in row.extracted_text
    first_at = row.extracted_at

    _run("--url", row.url, "--force")

    assert SubmittedURL.objects.count() == 1
    row.refresh_from_db()
    assert "feeding grounds" in row.extracted_text
    assert row.extracted_at >= first_at
    assert len(calls) == 2


def test_finished_ocr_row_skipped_without_force(monkeypatch, batch):
    _no_browser(monkeypatch)
    _stub_ocr(monkeypatch)
    _serve(monkeypatch, {"photo": (_fixture("ocr_text.png"), PNG_CT)})
    row = _url(batch, "https://example.com/photo.png")
    _run("--url", row.url)

    output = _run("--batch", str(batch.pk))

    assert "skipped (already extracted" in output
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK


def test_ocr_path_never_invokes_render_browser(monkeypatch, batch):
    calls = []
    monkeypatch.setattr(
        extraction, "render_browser", lambda url: calls.append(url) or ""
    )
    _stub_ocr(monkeypatch)
    monkeypatch.setattr(
        extraction, "_pdf_page_images", lambda body: [_stub_page()]
    )
    _serve(
        monkeypatch,
        {
            "a": (_fixture("ocr_text.png"), PNG_CT),
            "b": (_fixture("ocr_scan.pdf"), PDF_CT),
        },
    )
    _url(batch, "https://example.com/a.png")
    _url(batch, "https://example.com/b.pdf")

    _run()

    assert calls == []
    assert SubmittedURL.objects.filter(
        status=SubmittedURL.Status.OK,
        extraction_method=SubmittedURL.ExtractionMethod.OCR,
    ).count() == 2


def _stub_page():
    from PIL import Image

    return Image.new("RGB", (10, 10), "white")


def test_ocr_helpers_callable_directly(monkeypatch):
    monkeypatch.setattr(extraction, "_ocr_available", lambda: True)
    monkeypatch.setattr(extraction, "_ocr_image", lambda image: LONG_TEXT)
    text = extraction._ocr_image_bytes(_fixture("ocr_text.png"))
    assert "migration route" in text

    with pytest.raises(extraction.OcrError):
        extraction._ocr_image_bytes(b"not an image\x00\xff")
