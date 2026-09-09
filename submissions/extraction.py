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
    """Raised for any fetch problem; the message is the stored failure reason."""


@dataclass
class ExtractionResult:
    outcome: str  # "ok" or "failed"
    method: str  # SubmittedURL.ExtractionMethod value actually used / last tried
    char_count: int = 0
    reason: str = ""


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
                raise FetchError(f"unsupported content type: {content_type}")
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise FetchError("response too large")
            charset = response.headers.get_content_charset() or "utf-8"
            return body.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise FetchError("timeout") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise FetchError("timeout") from exc
        raise FetchError("connection error") from exc


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


def _save_failure(url: SubmittedURL, reason: str, method: str) -> ExtractionResult:
    url.status = SubmittedURL.Status.FAILED
    url.failure_reason = reason
    url.extraction_method = method
    url.extracted_text = ""
    url.extracted_title = ""
    url.extracted_at = None
    url.save()
    return ExtractionResult(outcome="failed", method=method, reason=reason)


def _save_success(
    url: SubmittedURL, text: str, title: str, method: str
) -> ExtractionResult:
    url.extracted_text = text
    url.extracted_title = title
    url.extraction_method = method
    url.extracted_at = timezone.now()
    url.status = SubmittedURL.Status.OK
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
    except FetchError as exc:
        return _save_failure(submitted_url, str(exc), Method.NONE)

    text, title = _extract_from_html(html, submitted_url.url)

    if not _is_insufficient(text):
        return _save_success(submitted_url, text, title, Method.STATIC)

    # Escalate to the browser fallback.
    try:
        rendered = render_browser(submitted_url.url)
    except Exception as exc:
        name = type(exc).__name__.lower()
        reason = "timeout" if "timeout" in name else "browser error"
        return _save_failure(submitted_url, reason, Method.BROWSER)

    rendered_text, rendered_title = _extract_from_html(rendered, submitted_url.url)
    title = title or rendered_title

    if _is_insufficient(rendered_text):
        return _save_failure(
            submitted_url, "no extractable content", Method.BROWSER
        )

    return _save_success(submitted_url, rendered_text, title, Method.BROWSER)
