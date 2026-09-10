"""Content extraction for :class:`submissions.models.SubmittedURL`.

Static fast path: a bounded stdlib HTTP fetch + ``trafilatura`` main-content
extraction. When that yields too little text the browser fallback renders the
page with headless Chromium (Playwright) and extracts from the rendered DOM.

Nothing here talks to an LLM, generates cards, or does batch orchestration.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
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


class BrowserError(Exception):
    """Wraps any exception raised while rendering the browser fallback."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


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


def fetch_static(url: str) -> str:
    """Fetch *url* over HTTP and return the decoded HTML body.

    Follows redirects (stdlib default). Raises :class:`FetchError` with a short
    human-readable reason on any network error, non-2xx status, non-HTML
    content type, timeout, or oversized body.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=STATIC_TIMEOUT) as response:
            content_type = response.headers.get_content_type()
            if content_type not in _HTML_CONTENT_TYPES:
                raise UnsupportedContentType(content_type)
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise BodyTooLarge()
            charset = response.headers.get_content_charset() or "utf-8"
            return body.decode(charset, errors="replace")
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


def extract(submitted_url: SubmittedURL, *, force: bool = False) -> ExtractionResult:
    """Fetch, extract, and persist content for *submitted_url*.

    Static path first; escalates to the browser fallback when the static text is
    below :data:`MIN_CONTENT_CHARS`. Overwrites any previous extraction in
    place. Never raises for an ordinary fetch/extraction failure - it records
    ``status = failed`` with a short reason and returns.
    """
    Method = SubmittedURL.ExtractionMethod

    try:
        html = fetch_static(submitted_url.url)
    except (
        FetchError,
        UnsupportedContentType,
        BodyTooLarge,
        urllib.error.URLError,
        OSError,
    ) as exc:
        return _save_failure(submitted_url, exc, Method.NONE)

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
