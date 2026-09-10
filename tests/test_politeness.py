"""Fetch politeness: robots.txt, per-domain rate limiting, retry/backoff (#17).

No real network, no real browser, no real ``time.sleep``: the clock, the sleep
call and the ``robots.txt`` fetch are the module's injectable seam and every
test replaces them with a fake clock / sleep spy / canned robots body.
"""

import socket
import urllib.error
from email.message import Message
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command

from submissions import extraction
from submissions.models import Batch, SubmittedURL

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


_JS_ONLY = (
    "<html><head><title>JS App</title></head>"
    "<body><div id='root'></div></body></html>"
)


class FakeClock:
    """Monotonic clock whose ``sleep`` simply advances the clock."""

    def __init__(self):
        self.t = 1_000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if seconds and seconds > 0:
            self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(extraction, "_now", c.now)
    monkeypatch.setattr(extraction, "_sleep", c.sleep)
    monkeypatch.setattr(extraction, "_jitter", lambda amount: 0.0)
    extraction._reset_politeness_state()
    return c


@pytest.fixture
def batch(db):
    return Batch.objects.create()


def _url(batch, href):
    return SubmittedURL.objects.create(url=href, batch=batch)


def _run(*args):
    out = StringIO()
    call_command("extract_content", *args, stdout=out)
    return out.getvalue()


def _set_robots(monkeypatch, mapping):
    """`mapping`: host -> robots.txt body (str) or None (missing / error)."""

    def fake(robots_url):
        return mapping.get(urlsplit(robots_url).hostname)

    monkeypatch.setattr(extraction, "_fetch_robots_text", fake)


def _http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://example.com/x", code, f"HTTP {code}", headers, None
    )


# --- module constants -----------------------------------------------------


def test_named_constants_present():
    for name in (
        "ROBOTS_CACHE_TTL",
        "PER_DOMAIN_MIN_INTERVAL",
        "MAX_RETRIES",
        "RETRY_BASE_DELAY",
        "RETRY_MAX_DELAY",
    ):
        assert isinstance(getattr(extraction, name), (int, float))


# --- robots.txt ---------------------------------------------------------


def test_disallowed_path_fails_and_is_not_fetched(clock, monkeypatch, batch):
    _set_robots(
        monkeypatch, {"blocked.example.com": "User-agent: *\nDisallow: /secret/"}
    )
    calls = []
    monkeypatch.setattr(
        extraction, "fetch_static", lambda url: calls.append(url) or _article_html()
    )
    row = _url(batch, "https://blocked.example.com/secret/page")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.BLOCKED_BY_ROBOTS
    assert row.failure_reason == "disallowed by robots.txt"
    assert calls == []


def test_allowed_path_is_fetched(clock, monkeypatch, batch):
    _set_robots(
        monkeypatch, {"ok.example.com": "User-agent: *\nDisallow: /secret/"}
    )
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    row = _url(batch, "https://ok.example.com/public/page")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK


def test_missing_robots_is_allowed(clock, monkeypatch, batch):
    _set_robots(monkeypatch, {"nofile.example.com": None})
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    row = _url(batch, "https://nofile.example.com/anything")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.failure_kind == ""


def test_empty_robots_is_allowed(clock, monkeypatch, batch):
    _set_robots(monkeypatch, {"empty.example.com": ""})
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    row = _url(batch, "https://empty.example.com/anything")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK


def test_robots_result_is_cached_per_host(clock, monkeypatch, batch):
    fetches = []

    def fake(robots_url):
        fetches.append(robots_url)
        return "User-agent: *\nDisallow:"

    monkeypatch.setattr(extraction, "_fetch_robots_text", fake)
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    a = _url(batch, "https://cache.example.com/one")
    b = _url(batch, "https://cache.example.com/two")

    _run("--url", a.url)
    _run("--url", b.url)

    assert len(fetches) == 1


def test_ignore_robots_flag_skips_the_check(clock, monkeypatch, batch):
    _set_robots(
        monkeypatch, {"blocked.example.com": "User-agent: *\nDisallow: /"}
    )
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    row = _url(batch, "https://blocked.example.com/secret/page")

    _run("--url", row.url, "--ignore-robots")

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK


# --- per-domain rate limiting ------------------------------------------


def test_same_domain_requests_are_spaced(clock, monkeypatch, batch):
    _set_robots(monkeypatch, {"sub1.site.example": "", "sub2.site.example": ""})
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    a = _url(batch, "https://sub1.site.example/a")
    b = _url(batch, "https://sub2.site.example/b")

    _run("--url", a.url)
    _run("--url", b.url)

    assert any(s >= extraction.PER_DOMAIN_MIN_INTERVAL for s in clock.sleeps)


def test_different_domains_are_not_delayed(clock, monkeypatch, batch):
    _set_robots(monkeypatch, {"one.example": "", "two.example": ""})
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    a = _url(batch, "https://one.example/a")
    b = _url(batch, "https://two.example/b")

    _run("--url", a.url)
    _run("--url", b.url)

    assert all(s <= 0 for s in clock.sleeps)


def test_crawl_delay_raises_the_interval(clock, monkeypatch, batch):
    _set_robots(
        monkeypatch,
        {"slow.example": "User-agent: *\nCrawl-delay: 9\nDisallow:"},
    )
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    a = _url(batch, "https://slow.example/a")
    b = _url(batch, "https://slow.example/b")

    _run("--url", a.url)
    _run("--url", b.url)

    assert any(s >= 9 for s in clock.sleeps)


# --- retry / backoff --------------------------------------------------


def test_429_then_success_within_max_retries(clock, monkeypatch, batch):
    attempts = []

    def fake(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise extraction.FetchError(_http_error(429))
        return _article_html()

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://flaky.example/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert len(attempts) == 2
    assert len(clock.sleeps) == 1  # one backoff wait


def test_persistent_500_exhausts_retries(clock, monkeypatch, batch):
    attempts = []

    def fake(url):
        attempts.append(url)
        raise extraction.FetchError(_http_error(500))

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://down.example/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.RETRIES_EXHAUSTED
    assert "retries exhausted" in row.failure_reason
    assert "HTTP 500" in row.failure_reason
    assert len(attempts) == extraction.MAX_RETRIES + 1
    assert len(clock.sleeps) == extraction.MAX_RETRIES


def test_timeout_is_retried_then_exhausts(clock, monkeypatch, batch):
    attempts = []

    def fake(url):
        attempts.append(url)
        raise extraction.FetchError(socket.timeout("timed out"))

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://slowloris.example/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.RETRIES_EXHAUSTED
    assert len(attempts) == extraction.MAX_RETRIES + 1


def test_404_is_not_retried(clock, monkeypatch, batch):
    attempts = []

    def fake(url):
        attempts.append(url)
        raise extraction.FetchError(_http_error(404))

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://gone.example/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.HTTP_CLIENT
    assert len(attempts) == 1
    assert clock.sleeps == []


def test_dns_failure_is_not_retried(clock, monkeypatch, batch):
    attempts = []

    def fake(url):
        attempts.append(url)
        raise extraction.FetchError(socket.gaierror(8, "nodename nor servname"))

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://nope.invalid/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.DNS
    assert len(attempts) == 1
    assert clock.sleeps == []


def test_oversized_body_is_not_retried(clock, monkeypatch, batch):
    attempts = []

    def fake(url):
        attempts.append(url)
        raise extraction.BodyTooLarge()

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://big.example/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.failure_kind == Kind.TOO_LARGE
    assert len(attempts) == 1


def test_retry_after_seconds_is_honoured(clock, monkeypatch, batch):
    attempts = []

    def fake(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise extraction.FetchError(_http_error(503, retry_after=7))
        return _article_html()

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://polite.example/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert clock.sleeps == [7.0]


def test_retry_after_http_date_is_honoured(clock, monkeypatch, batch):
    when = datetime.now(timezone.utc) + timedelta(seconds=12)
    attempts = []

    def fake(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise extraction.FetchError(
                _http_error(429, retry_after=format_datetime(when))
            )
        return _article_html()

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://polite2.example/x")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert len(clock.sleeps) == 1
    assert 0 < clock.sleeps[0] <= extraction.RETRY_MAX_DELAY


def test_retry_after_is_capped_at_max_delay(clock, monkeypatch, batch):
    attempts = []

    def fake(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise extraction.FetchError(_http_error(503, retry_after=99999))
        return _article_html()

    monkeypatch.setattr(extraction, "fetch_static", fake)
    row = _url(batch, "https://polite3.example/x")

    _run("--url", row.url)

    assert clock.sleeps == [extraction.RETRY_MAX_DELAY]


# --- browser fallback -------------------------------------------------


def test_browser_path_passes_robots_and_rate_limiter(clock, monkeypatch, batch):
    _set_robots(monkeypatch, {"spa.example.com": ""})
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _JS_ONLY)
    nav = []
    monkeypatch.setattr(
        extraction,
        "render_browser",
        lambda url: nav.append(url) or _article_html("Rendered"),
    )
    row = _url(batch, "https://spa.example.com/app")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.extraction_method == SubmittedURL.ExtractionMethod.BROWSER
    assert nav == ["https://spa.example.com/app"]
    # static fetch + browser nav, same domain -> spaced by the interval.
    assert any(s >= extraction.PER_DOMAIN_MIN_INTERVAL for s in clock.sleeps)


def test_browser_path_blocked_by_robots_does_not_navigate(
    clock, monkeypatch, batch
):
    # robots allows the static fetch to happen but... to isolate the browser
    # check we disallow outright and confirm neither path fetches.
    _set_robots(
        monkeypatch, {"spa2.example.com": "User-agent: *\nDisallow: /"}
    )
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _JS_ONLY)
    nav = []
    monkeypatch.setattr(
        extraction, "render_browser", lambda url: nav.append(url) or ""
    )
    row = _url(batch, "https://spa2.example.com/app")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.BLOCKED_BY_ROBOTS
    assert nav == []


def test_browser_navigation_timeout_still_reported_as_timeout(
    clock, monkeypatch, batch
):
    _set_robots(monkeypatch, {"spa3.example.com": ""})
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _JS_ONLY)

    def boom(url):
        raise TimeoutError("navigation timeout exceeded")

    monkeypatch.setattr(extraction, "render_browser", boom)
    row = _url(batch, "https://spa3.example.com/app")

    _run("--url", row.url)

    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.TIMEOUT


# --- command-level behaviour ----------------------------------------


def test_command_exits_zero_over_batch_with_a_robots_block(
    clock, monkeypatch, batch
):
    _set_robots(
        monkeypatch,
        {
            "a.example.com": "User-agent: *\nDisallow: /",
            "b.example.com": "",
        },
    )
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _article_html())
    blocked = _url(batch, "https://a.example.com/x")
    allowed = _url(batch, "https://b.example.com/y")

    out = _run("--batch", str(batch.pk))

    blocked.refresh_from_db()
    allowed.refresh_from_db()
    assert blocked.status == SubmittedURL.Status.FAILED
    assert blocked.failure_kind == Kind.BLOCKED_BY_ROBOTS
    assert allowed.status == SubmittedURL.Status.OK
    assert "disallowed by robots.txt" in out
