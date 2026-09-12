"""Tests for heuristic paywall / bot-wall / consent-wall detection (issue #21).

No real network calls and no real browser launch: ``fetch_static`` and
``render_browser`` are monkeypatched and every wall is a committed HTML
fixture string fed through the real ``extract`` path.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from submissions import extraction
from submissions.extraction import WALL_MARKERS, classify_failure, classify_wall
from submissions.models import Batch, BatchRequest, SubmittedURL

pytestmark = pytest.mark.django_db

Kind = SubmittedURL.FailureKind

_SENTENCE = (
    "The migratory patterns of arctic terns span roughly seventy thousand "
    "kilometres each year as the birds trace a winding path between polar "
    "feeding grounds. "
)


def _article_html(title="A Real Article", paragraphs=6, extra=""):
    body = "".join(f"<p>{_SENTENCE * 3}</p>" for _ in range(paragraphs))
    return (
        f"<html><head><title>{title}</title></head><body>"
        f"<article><h1>{title}</h1>{body}</article>{extra}</body></html>"
    )


# --- Committed HTML fixtures ---------------------------------------------

#: Cloudflare interstitial: thin visible text + bot markers.
CLOUDFLARE_HTML = (
    "<html><head><title>Just a moment...</title></head><body>"
    '<div id="cf-browser-verification">'
    "Checking your browser before you access example.com."
    "</div>"
    "<p>Please verify you are a human. This security check by Cloudflare "
    "needs a CAPTCHA check.</p>"
    "</body></html>"
)

#: Metered paywall overlay: thin text + subscribe phrasing + overlay id.
PAYWALL_HTML = (
    "<html><head><title>Great News Story</title></head><body>"
    '<div id="metered-paywall-overlay">'
    "<h2>Subscribe to continue</h2>"
    "<p>You've read your last free article. Already a subscriber? Sign in.</p>"
    "</div>"
    "<p>Intro fragment.</p>"
    "</body></html>"
)

#: Consent gate: thin text + consent-management ids + gate phrasing.
CONSENT_HTML = (
    "<html><head><title>News</title></head><body>"
    '<div id="onetrust-consent-sdk">'
    "<h2>We value your privacy</h2>"
    "<p>Choose your privacy settings. Accept cookies to continue. "
    "Powered by Quantcast Choice.</p>"
    "</div>"
    "</body></html>"
)

#: Genuinely thin page: no markers at all.
THIN_HTML = (
    "<html><head><title>Stub</title></head><body>"
    "<article><p>Blue oak.</p></article>"
    "</body></html>"
)

#: Thin page with only a weak hint (bare "subscribe", no strong phrase).
WEAK_HTML = (
    "<html><head><title>Newsletter</title></head><body>"
    "<p>Contact us to subscribe for updates.</p>"
    "</body></html>"
)

_BARE_THIN_HTML = "<html><body><p>tiny</p></body></html>"
_JS_SHELL_HTML = (
    "<html><head><title>JS App</title></head>"
    "<body><div id='root'></div></body></html>"
)


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


def _extracted_len(html, url="https://example.com/x"):
    text, _ = extraction._extract_from_html(html, url)
    return extraction._non_whitespace_len(text)


# --- Fixture sanity: wall fixtures really are thin, prose really is not ---


def test_fixtures_are_thin_but_prose_is_not():
    for html in (CLOUDFLARE_HTML, PAYWALL_HTML, CONSENT_HTML, THIN_HTML):
        assert _extracted_len(html) < extraction.MIN_CONTENT_CHARS
    assert (
        _extracted_len(_article_html()) >= extraction.MIN_CONTENT_CHARS
    )


# --- Pure classifier -------------------------------------------------------


def test_marker_list_lives_in_one_named_place():
    assert isinstance(WALL_MARKERS, dict)
    for key in ("bot_wall", "paywall", "consent_wall"):
        assert WALL_MARKERS[key], key


def test_classify_wall_is_pure_and_deterministic():
    first = classify_wall(CLOUDFLARE_HTML, "thin")
    second = classify_wall(CLOUDFLARE_HTML, "thin")
    assert first == second
    assert first[0] == Kind.BOT_WALL


def test_classify_cloudflare_is_bot_wall():
    kind, reason = classify_wall(CLOUDFLARE_HTML, "thin")
    assert kind == Kind.BOT_WALL
    assert "bot wall" in reason
    assert "checking your browser" in reason or "cf-browser-verification" in reason


def test_classify_metered_overlay_is_paywall():
    kind, reason = classify_wall(PAYWALL_HTML, "thin")
    assert kind == Kind.PAYWALL
    assert "paywall" in reason
    assert "subscribe to continue" in reason


def test_classify_consent_gate_is_consent_wall():
    kind, reason = classify_wall(CONSENT_HTML, "thin")
    assert kind == Kind.CONSENT_WALL
    assert "consent wall" in reason
    assert "onetrust" in reason or "quantcast" in reason


def test_classify_thin_page_is_no_content():
    kind, reason = classify_wall(THIN_HTML, "Blue oak.")
    assert kind == Kind.NO_CONTENT
    assert "no extractable content" in reason
    assert "suspected" not in reason


def test_classify_bare_subscribe_is_not_a_wall():
    # False-positive guard: bare "subscribe" with no strong phrase stays thin.
    kind, reason = classify_wall(WEAK_HTML, "Contact us to subscribe.")
    assert kind == Kind.NO_CONTENT
    assert "suspected" in reason  # noted, never a silent guess


def test_classify_conflicting_markers_prefers_bot_wall():
    html = CLOUDFLARE_HTML + PAYWALL_HTML
    kind, reason = classify_wall(html, "thin")
    assert kind == Kind.NO_CONTENT
    assert "suspected wall but signals conflict" in reason
    assert "bot_wall" in reason
    assert "paywall" in reason
    assert "not confirmed" in reason


def test_wall_kinds_reachable_only_via_classify_wall():
    # classify_failure alone never invents a wall kind from a bare signal.
    kind, _ = classify_failure(extraction.NoExtractableContent())
    assert kind == Kind.NO_CONTENT


# --- End-to-end through extract -------------------------------------------


def _static_then_bare(monkeypatch, static_html):
    monkeypatch.setattr(extraction, "fetch_static", lambda url: static_html)
    monkeypatch.setattr(extraction, "render_browser", lambda url: _BARE_THIN_HTML)


def test_extract_cloudflare_is_bot_wall(monkeypatch, batch):
    _static_then_bare(monkeypatch, CLOUDFLARE_HTML)
    row = _url(batch, "https://cf.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.BOT_WALL
    assert "bot wall" in row.failure_reason
    assert row.extraction_method == SubmittedURL.ExtractionMethod.BROWSER


def test_extract_metered_overlay_is_paywall(monkeypatch, batch):
    _static_then_bare(monkeypatch, PAYWALL_HTML)
    row = _url(batch, "https://pay.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.failure_kind == Kind.PAYWALL
    assert "paywall" in row.failure_reason
    assert "subscribe to continue" in row.failure_reason


def test_extract_consent_gate_is_consent_wall(monkeypatch, batch):
    _static_then_bare(monkeypatch, CONSENT_HTML)
    row = _url(batch, "https://consent.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.failure_kind == Kind.CONSENT_WALL
    assert "consent wall" in row.failure_reason


def test_extract_thin_page_stays_no_content(monkeypatch, batch):
    _static_then_bare(monkeypatch, THIN_HTML)
    row = _url(batch, "https://thin.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.failure_kind == Kind.NO_CONTENT
    assert "no extractable content" in row.failure_reason


def test_article_mentioning_subscribe_in_prose_is_ok(monkeypatch, batch):
    # False-positive guard: plenty of text + the word "subscribe" in prose
    # never reaches the classifier at all.
    html = _article_html(
        extra="<p>Readers who subscribe to our newsletter get more.</p>"
    )
    calls = []
    monkeypatch.setattr(extraction, "fetch_static", lambda url: html)
    monkeypatch.setattr(
        extraction, "render_browser", lambda url: calls.append(url) or html
    )
    row = _url(batch, "https://prose.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.failure_kind == ""
    assert calls == []  # classifier path (browser) never engaged


def test_page_with_enough_text_and_cookie_banner_is_never_reclassified(
    monkeypatch, batch
):
    banner = (
        '<div id="onetrust-consent-sdk">Cookie banner text here.</div>'
    )
    html = _article_html(extra=banner)
    monkeypatch.setattr(extraction, "fetch_static", lambda url: html)
    monkeypatch.setattr(
        extraction, "render_browser", lambda url: (_ for _ in ()).throw(
            AssertionError("browser must not run when static text suffices")
        ),
    )
    row = _url(batch, "https://banner.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.failure_kind == ""


def test_js_only_wall_is_classified(monkeypatch, batch):
    # The wall appears only after JS render: static is a bare shell.
    monkeypatch.setattr(extraction, "fetch_static", lambda url: _JS_SHELL_HTML)
    monkeypatch.setattr(extraction, "render_browser", lambda url: PAYWALL_HTML)
    row = _url(batch, "https://spa.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.FAILED
    assert row.failure_kind == Kind.PAYWALL
    assert "paywall" in row.failure_reason


def test_static_wall_survives_bare_render(monkeypatch, batch):
    # Static HTML carried the wall; the render is thin but marker-free.
    # Both paths feed the same classifier, so the static signal still wins.
    monkeypatch.setattr(extraction, "fetch_static", lambda url: CLOUDFLARE_HTML)
    monkeypatch.setattr(extraction, "render_browser", lambda url: _BARE_THIN_HTML)
    row = _url(batch, "https://static-wall.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.failure_kind == Kind.BOT_WALL


def test_command_continues_past_all_walls_and_exits_zero(monkeypatch, batch):
    pages = {
        "cf": CLOUDFLARE_HTML,
        "pay": PAYWALL_HTML,
        "consent": CONSENT_HTML,
        "thin": THIN_HTML,
        "prose": _article_html(),
    }

    def fake_fetch(url):
        for key, html in pages.items():
            if f"/{key}/" in url:
                return html
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(extraction, "fetch_static", fake_fetch)
    monkeypatch.setattr(extraction, "render_browser", lambda url: _BARE_THIN_HTML)

    rows = {key: _url(batch, f"https://all.example.com/{key}/") for key in pages}
    output = _run()  # no selector: processes every method=='none' row, exits 0

    expected = {
        "cf": Kind.BOT_WALL,
        "pay": Kind.PAYWALL,
        "consent": Kind.CONSENT_WALL,
        "thin": Kind.NO_CONTENT,
        "prose": "",
    }
    for key, row in rows.items():
        row.refresh_from_db()
        assert row.failure_kind == expected[key], key
    assert rows["prose"].status == SubmittedURL.Status.OK
    for key in ("cf", "pay", "consent", "thin"):
        assert rows[key].status == SubmittedURL.Status.FAILED
    assert "[bot_wall]" in output
    assert "[paywall]" in output
    assert "[consent_wall]" in output
    assert "[no_content]" in output


def test_rerun_updates_kind_in_place_and_success_clears(monkeypatch, batch):
    row = _url(batch, "https://flip.example.com/x")

    _static_then_bare(monkeypatch, CLOUDFLARE_HTML)
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.failure_kind == Kind.BOT_WALL

    # Same URL now serves a paywall: kind updates, still one row.
    _static_then_bare(monkeypatch, PAYWALL_HTML)
    _run("--url", row.url, "--force")
    row.refresh_from_db()
    assert row.failure_kind == Kind.PAYWALL
    assert SubmittedURL.objects.filter(url=row.url).count() == 1

    # Later it serves real content: wall classification is cleared.
    monkeypatch.setattr(
        extraction, "fetch_static", lambda url: _article_html()
    )
    _run("--url", row.url, "--force")
    row.refresh_from_db()
    assert row.status == SubmittedURL.Status.OK
    assert row.failure_kind == ""
    assert row.failure_reason == ""
    assert "arctic terns" in row.extracted_text


def test_ambiguous_weak_signals_stay_no_content_with_note(monkeypatch, batch):
    _static_then_bare(monkeypatch, WEAK_HTML)
    row = _url(batch, "https://weak.example.com/x")
    _run("--url", row.url)
    row.refresh_from_db()
    assert row.failure_kind == Kind.NO_CONTENT
    assert "suspected" in row.failure_reason


def test_failure_kind_summary_covers_new_kinds(batch):
    for i, kind in enumerate(
        (Kind.PAYWALL, Kind.BOT_WALL, Kind.CONSENT_WALL, Kind.NO_CONTENT)
    ):
        _url(
            batch,
            f"https://s{i}.example.com/",
            status=SubmittedURL.Status.FAILED,
            failure_kind=kind,
        )
    summary = batch.failure_kind_summary
    assert "4 failed" in summary
    assert "paywall" in summary
    assert "consent gate" in summary
    assert "_" not in summary
