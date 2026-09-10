"""Content extraction for :class:`submissions.models.SubmittedURL`.

Static fast path: a bounded stdlib HTTP fetch + ``trafilatura`` main-content
extraction. When that yields too little text the browser fallback renders the
page with headless Chromium (Playwright) and extracts from the rendered DOM.

A response that is a PDF or a Word ``.docx`` is routed to the ``document``
path instead (``pdfminer.six`` for PDF text, the stdlib ``zipfile`` + ``xml``
for docx); the browser fallback is never used for documents.

Nothing here talks to an LLM, generates cards, or does batch orchestration.
"""

from __future__ import annotations

import io
import re
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit

import trafilatura
from django.utils import timezone

from .models import SubmittedURL

# --- Tunable constants -------------------------------------------------------

#: A static result is "too little content" below this many non-whitespace chars.
MIN_CONTENT_CHARS = 200
#: Bounded timeout (seconds) for the static HTTP fetch.
STATIC_TIMEOUT = 10
#: Bounded navigation timeout (milliseconds) for the browser fallback.
BROWSER_TIMEOUT_MS = 30_000
#: Cap on the downloaded static body; larger responses are failed, not buffered.
MAX_BODY_BYTES = 10 * 1024 * 1024

_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "application/xml", "text/xml"}

#: Non-HTML document content types handled by the ``document`` extraction path.
_PDF_CONTENT_TYPE = "application/pdf"
_DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_DOCUMENT_CONTENT_TYPES = {_PDF_CONTENT_TYPE, _DOCX_CONTENT_TYPE}
#: Content types generic enough that we sniff the leading bytes to classify.
_SNIFFABLE_CONTENT_TYPES = {"application/octet-stream", "binary/octet-stream", ""}

_DOCX_MAIN_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DC_NS = "http://purl.org/dc/elements/1.1/"

_USER_AGENT = "flashcard-generator/0.1 (+content extraction)"


class FetchError(Exception):
    """Wraps a low-level fetch exception raised by the static path."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


class UnsupportedContentType(Exception):
    """The static fetch got a non-HTML ``Content-Type``."""

    def __init__(self, content_type):
        super().__init__(content_type)
        self.content_type = content_type


class BodyTooLarge(Exception):
    """The static response body exceeded :data:`MAX_BODY_BYTES`."""


class NoExtractableContent(Exception):
    """Both extraction paths returned less than :data:`MIN_CONTENT_CHARS`."""


class NoExtractableText(Exception):
    """A PDF parsed cleanly but exposed no usable text layer (scanned/image)."""


class PdfEncrypted(Exception):
    """The PDF is encrypted / password-protected and cannot be opened."""


class DocumentParseError(Exception):
    """A PDF or docx payload was malformed / truncated / unreadable."""

    def __init__(self, detail=""):
        super().__init__(detail)
        self.detail = str(detail)


class BrowserError(Exception):
    """Wraps any exception raised while rendering the browser fallback."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


@dataclass
class FetchedDocument:
    """A supported non-HTML document body returned by :func:`fetch_static`."""

    fmt: str  # "pdf" or "docx"
    body: bytes
    content_type: str


@dataclass
class ExtractionResult:
    outcome: str  # "ok" or "failed"
    method: str  # SubmittedURL.ExtractionMethod value actually used / last tried
    char_count: int = 0
    reason: str = ""
    kind: str = ""  # SubmittedURL.FailureKind value, "" when outcome == "ok"


_DNS_MARKERS = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "name resolution",
    "no address associated with hostname",
)
_CONNECTION_MARKERS = (
    "refused",
    "reset",
    "unreachable",
    "aborted",
    "broken pipe",
    "connection",
)


def classify_failure(exc, *, url=""):
    """Turn a failure - an exception or a signalled condition - into
    ``(failure_kind, failure_reason)``.

    This is the single place that maps a condition to a taxonomy entry. The
    ``extract_content`` command and any future async worker call
    :func:`extract` and read :class:`ExtractionResult`; they never re-classify.
    New ``failure_kind`` values get wired in here and nowhere else.
    """
    Kind = SubmittedURL.FailureKind
    host = urlsplit(url).hostname or ""
    host_suffix = f": {host}" if host else ""

    if isinstance(exc, UnsupportedContentType):
        return Kind.UNSUPPORTED_TYPE, f"unsupported content type: {exc.content_type}"
    if isinstance(exc, BodyTooLarge):
        cap_mb = MAX_BODY_BYTES // (1024 * 1024)
        return Kind.TOO_LARGE, f"response exceeded {cap_mb} MB cap"
    if isinstance(exc, NoExtractableContent):
        return (
            Kind.NO_CONTENT,
            "no extractable content (both paths returned under the threshold)",
        )
    if isinstance(exc, NoExtractableText):
        return (
            Kind.NO_CONTENT,
            "PDF has no extractable text layer (scanned / image-only); "
            "OCR of scanned PDFs is a separate follow-up (#19)",
        )
    if isinstance(exc, PdfEncrypted):
        return Kind.UNKNOWN, "PDF is encrypted / password-protected"
    if isinstance(exc, DocumentParseError):
        suffix = f": {exc.detail}" if exc.detail else ""
        return Kind.UNKNOWN, f"could not parse document{suffix}"
    if isinstance(exc, BrowserError):
        detail = f"{type(exc.cause).__name__}: {exc.cause}".lower()
        if "timeout" in detail:
            return Kind.TIMEOUT, "browser navigation timed out"
        return Kind.UNKNOWN, f"browser error: {exc.cause}"

    cause = exc.cause if isinstance(exc, FetchError) else exc

    if isinstance(cause, urllib.error.HTTPError):
        code = cause.code
        if code == 429:
            return Kind.BLOCKED, "HTTP 429 (rate limited)"
        if code in (401, 403):
            return Kind.BLOCKED, f"HTTP {code} (access refused)"
        if 400 <= code < 500:
            return Kind.HTTP_CLIENT, f"HTTP {code}"
        return Kind.UNKNOWN, f"HTTP {code}"
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return Kind.TIMEOUT, "request timed out"
    if isinstance(cause, socket.gaierror):
        return Kind.DNS, f"host not found{host_suffix}"

    if isinstance(cause, urllib.error.URLError):
        reason = cause.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return Kind.TIMEOUT, "request timed out"
        if isinstance(reason, socket.gaierror):
            return Kind.DNS, f"host not found{host_suffix}"
        if isinstance(reason, ConnectionError):
            return Kind.CONNECTION, f"connection failed: {reason}"
        text = str(reason).lower()
        if any(marker in text for marker in _DNS_MARKERS):
            return Kind.DNS, f"host not found{host_suffix}"
        if any(marker in text for marker in _CONNECTION_MARKERS):
            return Kind.CONNECTION, f"connection failed: {reason}"
        return Kind.UNKNOWN, f"fetch failed: {reason}"

    if isinstance(cause, ConnectionError):
        return Kind.CONNECTION, f"connection failed: {cause}"

    return Kind.UNKNOWN, f"unexpected error: {cause}"


# --- Static fast path ------------------------------------------------------


def _detect_document_format(body: bytes, content_type: str):
    """Return ``"pdf"`` / ``"docx"`` for a supported document, else ``None``."""
    if content_type == _PDF_CONTENT_TYPE:
        return "pdf"
    if content_type == _DOCX_CONTENT_TYPE:
        return "docx"
    if content_type in _SNIFFABLE_CONTENT_TYPES:
        if body[:5] == b"%PDF-":
            return "pdf"
        if body[:2] == b"PK" and _zip_contains(body, "word/document.xml"):
            return "docx"
    return None


def _zip_contains(body: bytes, name: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return name in archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def fetch_static(url: str):
    """Fetch *url* over HTTP and return its body.

    Returns a ``str`` (decoded HTML) for an HTML response, or a
    :class:`FetchedDocument` for a supported non-HTML document (PDF / docx).
    Follows redirects (stdlib default). Raises :class:`FetchError` on any
    network error/timeout, :class:`BodyTooLarge` on an oversized body, and
    :class:`UnsupportedContentType` for any other content type.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=STATIC_TIMEOUT) as response:
            content_type = response.headers.get_content_type()
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise BodyTooLarge()
            if content_type in _HTML_CONTENT_TYPES:
                charset = response.headers.get_content_charset() or "utf-8"
                return body.decode(charset, errors="replace")
            fmt = _detect_document_format(body, content_type)
            if fmt is not None:
                return FetchedDocument(fmt, body, content_type)
            raise UnsupportedContentType(content_type)
    except (urllib.error.URLError, OSError) as exc:
        # URLError covers HTTPError; OSError covers socket.timeout, gaierror,
        # and bare ConnectionError. classify_failure() sorts them out.
        raise FetchError(exc) from exc


def render_browser(url: str) -> str:
    """Render *url* in headless Chromium and return the rendered HTML.

    Isolated so tests can stub the browser launch. Raises on navigation
    failure/timeout; the caller maps that to a failure reason.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, timeout=BROWSER_TIMEOUT_MS, wait_until="load")
            return page.content()
        finally:
            browser.close()


def _extract_from_html(html: str, url: str) -> tuple[str, str]:
    """Return ``(main_text, title)`` from *html* using trafilatura."""
    text = trafilatura.extract(html, url=url) or ""
    title = ""
    try:
        metadata = trafilatura.extract_metadata(html)
        if metadata and metadata.title:
            title = metadata.title
    except Exception:  # pragma: no cover - metadata parsing is best-effort
        title = ""
    return text.strip(), (title or "").strip()


def _decode_pdf_string(value) -> str:
    if isinstance(value, bytes):
        if value[:2] == b"\xfe\xff":
            return value[2:].decode("utf-16-be", "replace").strip()
        if value[:2] == b"\xff\xfe":
            return value[2:].decode("utf-16-le", "replace").strip()
        return value.decode("latin-1", "replace").strip()
    return str(value).strip()


def _clean_pdf_text(raw: str) -> str:
    """Drop page-break noise and running headers/footers repeated across pages."""
    pages = [page.strip("\n") for page in raw.split("\x0c")]
    per_page = [[line.rstrip() for line in page.splitlines()] for page in pages]

    counts: Counter[str] = Counter()
    for lines in per_page:
        for line in {ln.strip() for ln in lines if ln.strip()}:
            counts[line] += 1
    n = len(per_page)
    repeated = {
        line
        for line, seen in counts.items()
        if n >= 3 and seen >= max(2, round(n * 0.6))
    }

    cleaned_pages = []
    for lines in per_page:
        kept = "\n".join(ln for ln in lines if ln.strip() not in repeated).strip()
        if kept:
            cleaned_pages.append(kept)
    text = "\n\n".join(cleaned_pages)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_from_pdf(body: bytes) -> tuple[str, str]:
    """Return ``(main_text, title)`` from PDF *body* using pdfminer.six.

    Raises :class:`PdfEncrypted` for a password-protected file and
    :class:`DocumentParseError` for malformed / truncated bytes.
    """
    from pdfminer.high_level import extract_text
    from pdfminer.pdfdocument import PDFDocument, PDFPasswordIncorrect
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import resolve1

    title = ""
    try:
        document = PDFDocument(PDFParser(io.BytesIO(body)))
        for info in document.info or []:
            raw_title = info.get("Title")
            if raw_title:
                title = _decode_pdf_string(resolve1(raw_title))
                break
    except PDFPasswordIncorrect as exc:
        raise PdfEncrypted(str(exc)) from exc
    except Exception as exc:  # malformed / truncated / unreadable structure
        raise DocumentParseError(f"{type(exc).__name__}: {exc}") from exc

    try:
        text = extract_text(io.BytesIO(body)) or ""
    except PDFPasswordIncorrect as exc:
        raise PdfEncrypted(str(exc)) from exc
    except Exception as exc:
        raise DocumentParseError(f"{type(exc).__name__}: {exc}") from exc

    return _clean_pdf_text(text), title


def _extract_from_docx(body: bytes) -> tuple[str, str]:
    """Return ``(paragraph_text, title)`` from a ``.docx`` *body* using stdlib.

    Reads ``word/document.xml`` and ``docProps/core.xml`` straight out of the
    ZIP. Raises :class:`DocumentParseError` for malformed / truncated bytes.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            document_xml = archive.read("word/document.xml")
            try:
                core_xml = archive.read("docProps/core.xml")
            except KeyError:
                core_xml = None
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise DocumentParseError(f"{type(exc).__name__}: {exc}") from exc

    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError as exc:
        raise DocumentParseError(f"malformed word/document.xml: {exc}") from exc

    w = f"{{{_DOCX_MAIN_NS}}}"
    paragraphs = []
    for para in root.iter(f"{w}p"):
        parts = []
        for node in para.iter():
            if node.tag == f"{w}t":
                parts.append(node.text or "")
            elif node.tag == f"{w}tab":
                parts.append("\t")
            elif node.tag in (f"{w}br", f"{w}cr"):
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    text = "\n".join(paragraphs).strip()

    title = ""
    if core_xml:
        try:
            node = ET.fromstring(core_xml).find(f"{{{_DC_NS}}}title")
            if node is not None and node.text:
                title = node.text.strip()
        except ET.ParseError:
            title = ""

    return text, title


def _non_whitespace_len(text: str) -> int:
    return len("".join(text.split()))


def _is_insufficient(text: str) -> bool:
    return _non_whitespace_len(text) < MIN_CONTENT_CHARS


def _save_failure(
    url: SubmittedURL, exc, method: str
) -> ExtractionResult:
    kind, reason = classify_failure(exc, url=url.url)
    url.status = SubmittedURL.Status.FAILED
    url.failure_kind = kind
    url.failure_reason = reason
    url.extraction_method = method
    url.extracted_text = ""
    url.extracted_title = ""
    url.extracted_at = None
    url.save()
    return ExtractionResult(
        outcome="failed", method=method, reason=reason, kind=kind
    )


def _save_success(
    url: SubmittedURL, text: str, title: str, method: str
) -> ExtractionResult:
    url.extracted_text = text
    url.extracted_title = title
    url.extraction_method = method
    url.extracted_at = timezone.now()
    url.status = SubmittedURL.Status.OK
    url.failure_kind = ""
    url.failure_reason = ""
    url.save()
    return ExtractionResult(
        outcome="ok", method=method, char_count=_non_whitespace_len(text)
    )


def _extract_document(
    submitted_url: SubmittedURL, fetched: FetchedDocument
) -> ExtractionResult:
    """Parse a fetched PDF / docx and persist the outcome.

    The browser fallback is never used for documents.
    """
    method = SubmittedURL.ExtractionMethod.DOCUMENT
    try:
        if fetched.fmt == "pdf":
            text, title = _extract_from_pdf(fetched.body)
        else:
            text, title = _extract_from_docx(fetched.body)
    except (PdfEncrypted, DocumentParseError) as exc:
        return _save_failure(submitted_url, exc, method)

    if fetched.fmt == "pdf" and _is_insufficient(text):
        return _save_failure(submitted_url, NoExtractableText(), method)

    return _save_success(submitted_url, text, title, method)


def extract(submitted_url: SubmittedURL, *, force: bool = False) -> ExtractionResult:
    """Fetch, extract, and persist content for *submitted_url*.

    Static path first; escalates to the browser fallback when the static text is
    below :data:`MIN_CONTENT_CHARS`. Overwrites any previous extraction in
    place. Never raises for an ordinary fetch/extraction failure - it records
    ``status = failed`` with a short reason and returns.
    """
    Method = SubmittedURL.ExtractionMethod

    try:
        fetched = fetch_static(submitted_url.url)
    except (
        FetchError,
        UnsupportedContentType,
        BodyTooLarge,
        urllib.error.URLError,
        OSError,
    ) as exc:
        return _save_failure(submitted_url, exc, Method.NONE)

    if isinstance(fetched, FetchedDocument):
        return _extract_document(submitted_url, fetched)

    html = fetched
    text, title = _extract_from_html(html, submitted_url.url)

    if not _is_insufficient(text):
        return _save_success(submitted_url, text, title, Method.STATIC)

    # Escalate to the browser fallback.
    try:
        rendered = render_browser(submitted_url.url)
    except Exception as exc:
        return _save_failure(submitted_url, BrowserError(exc), Method.BROWSER)

    rendered_text, rendered_title = _extract_from_html(rendered, submitted_url.url)
    title = title or rendered_title

    if _is_insufficient(rendered_text):
        return _save_failure(
            submitted_url, NoExtractableContent(), Method.BROWSER
        )

    return _save_success(submitted_url, rendered_text, title, Method.BROWSER)
