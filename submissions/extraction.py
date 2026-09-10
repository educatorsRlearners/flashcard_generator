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
import random
import re
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone as _dt_timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

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

# --- Fetch politeness: robots.txt, per-domain rate limiting, retry/backoff --
#
# All state below is in-process only: it lives for the duration of a single
# ``extract_content`` run. Sharing the robots cache / rate-limit clock across
# worker processes is #8's job (background batch processing), not this one.

#: How long a parsed ``robots.txt`` is trusted before it is re-fetched (seconds).
ROBOTS_CACHE_TTL = 3600
#: Minimum spacing between two fetches to the same registrable domain (seconds).
#: A ``Crawl-delay`` in ``robots.txt`` raises this for that domain when larger.
PER_DOMAIN_MIN_INTERVAL = 1.0
#: How many times a transient fetch failure is retried before giving up.
MAX_RETRIES = 3
#: Base delay for exponential backoff: ``RETRY_BASE_DELAY * 2 ** attempt`` (s).
RETRY_BASE_DELAY = 1.0
#: Upper bound on any single backoff / ``Retry-After`` wait (seconds).
RETRY_MAX_DELAY = 30.0


# --- Injectable seam: the whole module sleeps / reads the clock / fetches
#     robots.txt only through these three functions, so the test suite can
#     replace them with a fake clock, a sleep spy, and canned robots bodies
#     and run instantly and offline.


def _now() -> float:
    """Monotonic wall-clock reading used by the rate limiter and robots cache."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Block for *seconds* (retry backoff and per-domain rate-limit waits)."""
    if seconds and seconds > 0:
        time.sleep(seconds)


def _jitter(amount: float) -> float:
    """Random jitter in ``[0, amount]`` added to a computed backoff delay."""
    if amount <= 0:
        return 0.0
    return random.uniform(0.0, amount)


def _fetch_robots_text(robots_url: str):
    """Return the body of *robots_url* as text, or ``None``.

    ``None`` means "treat as allow-all": a missing (404), empty, or
    network-erroring ``robots.txt`` must never itself fail the target URL.
    """
    request = urllib.request.Request(
        robots_url, headers={"User-Agent": _USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=STATIC_TIMEOUT) as response:
            status = getattr(response, "status", None) or getattr(
                response, "code", None
            )
            try:
                if status is not None and int(status) >= 400:
                    return None
            except (TypeError, ValueError):
                pass
            raw = response.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError:
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


#: host -> ``(fetched_at_monotonic, RobotFileParser)``
_robots_cache: dict = {}
#: registrable domain -> last fetch time (monotonic)
_domain_last_fetch: dict = {}
#: registrable domain -> minimum interval raised by a robots ``Crawl-delay``
_domain_min_interval: dict = {}


def _reset_politeness_state() -> None:
    """Drop all in-process robots / rate-limit state (used by the test suite)."""
    _robots_cache.clear()
    _domain_last_fetch.clear()
    _domain_min_interval.clear()


def _registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 for *host*.

    MVP heuristic: the last two labels. Subdomains of one site therefore share
    a rate-limit bucket. Multi-part public suffixes (``co.uk``) are not handled
    - acceptable for now per the issue; #8 can tighten this if it matters.
    """
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def _robots_parser_for(url: str):
    parts = urlsplit(url)
    host = parts.hostname or ""
    cached = _robots_cache.get(host)
    now = _now()
    if cached is not None and (now - cached[0]) < ROBOTS_CACHE_TTL:
        return cached[1]

    scheme = parts.scheme or "https"
    robots_url = urlunsplit((scheme, parts.netloc, "/robots.txt", "", ""))
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    text = _fetch_robots_text(robots_url)
    if text is None:
        parser.allow_all = True
    else:
        parser.parse(text.splitlines())
    parser.modified()  # set mtime so can_fetch / crawl_delay are live
    _robots_cache[host] = (now, parser)
    return parser


def _note_crawl_delay(url: str, parser) -> None:
    """Raise the target domain's minimum interval to a robots ``Crawl-delay``."""
    delay = None
    try:
        delay = parser.crawl_delay(_USER_AGENT)
    except Exception:  # pragma: no cover - defensive
        delay = None
    if delay is None:
        try:
            rate = parser.request_rate(_USER_AGENT)
        except Exception:  # pragma: no cover - defensive
            rate = None
        if rate is not None and getattr(rate, "requests", 0):
            delay = rate.seconds / rate.requests
    if delay:
        domain = _registrable_domain(urlsplit(url).hostname or "")
        _domain_min_interval[domain] = max(
            _domain_min_interval.get(domain, 0.0), float(delay)
        )


def _check_robots(url: str, *, ignore_robots: bool = False) -> None:
    """Raise :class:`RobotsDisallowed` if *url* is disallowed for our agent."""
    if ignore_robots:
        return
    parser = _robots_parser_for(url)
    _note_crawl_delay(url, parser)
    if not parser.can_fetch(_USER_AGENT, url):
        raise RobotsDisallowed(url)


def _rate_limit(url: str) -> None:
    """Space this fetch from the previous one to the same registrable domain."""
    domain = _registrable_domain(urlsplit(url).hostname or "")
    interval = max(
        PER_DOMAIN_MIN_INTERVAL, _domain_min_interval.get(domain, 0.0)
    )
    last = _domain_last_fetch.get(domain)
    now = _now()
    if last is not None:
        wait = interval - (now - last)
        if wait > 0:
            _sleep(wait)
            now = _now()
    _domain_last_fetch[domain] = now


def _retry_cause(exc):
    return exc.cause if isinstance(exc, FetchError) else exc


def _is_retryable(exc) -> bool:
    """True for a transient failure (timeout / connection / 429 / 5xx)."""
    cause = _retry_cause(exc)
    if isinstance(cause, urllib.error.HTTPError):
        return cause.code == 429 or 500 <= cause.code < 600
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return True
    if isinstance(cause, socket.gaierror):
        return False
    if isinstance(cause, urllib.error.URLError):
        reason = cause.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return True
        if isinstance(reason, socket.gaierror):
            return False
        if isinstance(reason, ConnectionError):
            return True
        text = str(reason).lower()
        if any(marker in text for marker in _DNS_MARKERS):
            return False
        return any(marker in text for marker in _CONNECTION_MARKERS)
    if isinstance(cause, ConnectionError):
        return True
    return False


def _retry_after_delay(exc):
    """Seconds requested by a ``Retry-After`` header, capped, or ``None``."""
    cause = _retry_cause(exc)
    if not isinstance(cause, urllib.error.HTTPError):
        return None
    headers = getattr(cause, "headers", None)
    value = None
    if headers is not None:
        try:
            value = headers.get("Retry-After")
        except Exception:  # pragma: no cover - defensive
            value = None
    if not value:
        return None
    value = str(value).strip()
    if value.isdigit():
        seconds = float(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt_timezone.utc)
        seconds = (when - datetime.now(_dt_timezone.utc)).total_seconds()
    seconds = max(0.0, seconds)
    return min(seconds, RETRY_MAX_DELAY)


def _backoff_delay(attempt: int) -> float:
    capped = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt))
    return capped + _jitter(capped)


def _fetch_static_with_retry(url: str):
    """:func:`fetch_static` wrapped in a bounded exponential-backoff retry.

    Retries timeouts, connection errors, HTTP 429 and HTTP 5xx up to
    :data:`MAX_RETRIES` times, honouring ``Retry-After`` when present. DNS
    failures, 4xx (except 429), unsupported types and oversized bodies are not
    retried - they raise on the first attempt. When every retry of a transient
    failure is used up, raises :class:`RetriesExhausted` carrying the cause.
    """
    attempt = 0
    while True:
        try:
            return fetch_static(url)
        except (FetchError, UnsupportedContentType, BodyTooLarge) as exc:
            if not _is_retryable(exc):
                raise
            if attempt >= MAX_RETRIES:
                raise RetriesExhausted(exc, attempt + 1) from exc
            retry_after = _retry_after_delay(exc)
            delay = (
                retry_after
                if retry_after is not None
                else _backoff_delay(attempt)
            )
            _sleep(delay)
            attempt += 1


class RobotsDisallowed(Exception):
    """The target URL is disallowed by the host's ``robots.txt``."""

    def __init__(self, url):
        super().__init__(url)
        self.url = url


class RetriesExhausted(Exception):
    """Every retry of a transient fetch failure was used up."""

    def __init__(self, last_exc, attempts):
        super().__init__(str(last_exc))
        self.last_exc = last_exc
        self.attempts = attempts


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

    if isinstance(exc, RobotsDisallowed):
        return Kind.BLOCKED_BY_ROBOTS, "disallowed by robots.txt"
    if isinstance(exc, RetriesExhausted):
        _, inner = classify_failure(exc.last_exc, url=url)
        return (
            Kind.RETRIES_EXHAUSTED,
            f"retries exhausted after {exc.attempts} attempts: {inner}",
        )
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


def extract(
    submitted_url: SubmittedURL,
    *,
    force: bool = False,
    ignore_robots: bool = False,
) -> ExtractionResult:
    """Fetch, extract, and persist content for *submitted_url*.

    Static path first; escalates to the browser fallback when the static text is
    below :data:`MIN_CONTENT_CHARS`. Overwrites any previous extraction in
    place. Never raises for an ordinary fetch/extraction failure - it records
    ``status = failed`` with a short reason and returns.
    """
    Method = SubmittedURL.ExtractionMethod

    try:
        _check_robots(submitted_url.url, ignore_robots=ignore_robots)
        _rate_limit(submitted_url.url)
        fetched = _fetch_static_with_retry(submitted_url.url)
    except (
        RobotsDisallowed,
        RetriesExhausted,
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

    # Escalate to the browser fallback - which also honours robots.txt and the
    # per-domain rate limiter before navigating (it has no retry loop of its
    # own; a navigation timeout is still reported as a timeout).
    try:
        _check_robots(submitted_url.url, ignore_robots=ignore_robots)
        _rate_limit(submitted_url.url)
        rendered = render_browser(submitted_url.url)
    except RobotsDisallowed as exc:
        return _save_failure(submitted_url, exc, Method.BROWSER)
    except Exception as exc:
        return _save_failure(submitted_url, BrowserError(exc), Method.BROWSER)

    rendered_text, rendered_title = _extract_from_html(rendered, submitted_url.url)
    title = title or rendered_title

    if _is_insufficient(rendered_text):
        return _save_failure(
            submitted_url, NoExtractableContent(), Method.BROWSER
        )

    return _save_success(submitted_url, rendered_text, title, Method.BROWSER)
