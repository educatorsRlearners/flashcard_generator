"""Per-card images (issue #12).

The source-page fetch, the per-image download and the Draw Things HTTP
call are all replaced with fakes - these tests never touch the network
and never need a running Draw Things.
"""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from submissions import images
from submissions.models import Card, SubmittedURL

pytestmark = pytest.mark.django_db

#: Captured before the suite-wide ``_no_card_images`` stub (tests/conftest.py)
#: can replace it, so this module can exercise the real implementation.
_REAL_ATTACH_IMAGES = images.attach_images


@pytest.fixture(autouse=True)
def _use_real_attach_images(monkeypatch):
    monkeypatch.setattr(images, "attach_images", _REAL_ATTACH_IMAGES)


# --- helpers --------------------------------------------------------


def png_bytes(width: int, height: int, color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def make_url(url="https://example.com/article"):
    return SubmittedURL.objects.create(
        url=url,
        status=SubmittedURL.Status.OK,
        extraction_method=SubmittedURL.ExtractionMethod.STATIC,
        extracted_text="x" * 500,
    )


def make_card(submitted_url, note_type="basic", **kw):
    opts = dict(
        note_type=note_type,
        front="What is a mitochondrion?",
        back="The powerhouse of the cell.",
        source_term="mitochondrion",
        tags={"topic": "biology"},
    )
    opts.update(kw)
    return Card.objects.create(submitted_url=submitted_url, **opts)


class FakeDrawThings:
    """Stand-in for :class:`images.DrawThingsClient`."""

    def __init__(self, result: bytes | None):
        self.result = result
        self.calls: list[str] = []

    def generate(self, prompt: str) -> bytes | None:
        self.calls.append(prompt)
        return self.result


@pytest.fixture
def fake_fetch(monkeypatch):
    """Install a URL -> (FetchedImage | Exception) map for _fetch_image."""

    def _install(mapping: dict):
        def _fetch(url: str):
            value = mapping[url]
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(images, "_fetch_image", _fetch)

    return _install


@pytest.fixture(autouse=True)
def _media_to_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")


# --- usable-image rules -------------------------------------------


def test_usable_rules_reject_small_pixel_and_accept_real_image():
    assert images.is_usable_image(png_bytes(1, 1)) is False
    assert images.is_usable_image(png_bytes(64, 64)) is False  # icon-sized
    assert images.is_usable_image(png_bytes(400, 300)) is True


def test_usable_rules_reject_tiny_byte_payload_and_non_image():
    assert images.is_usable_image(b"") is False
    assert images.is_usable_image(b"not an image at all") is False


def test_usable_rules_reject_disallowed_content_type():
    data = png_bytes(400, 300)
    assert images.is_usable_image(data, "image/svg+xml") is False
    assert images.is_usable_image(data, "image/png") is True


def test_candidates_exclude_chrome_tracking_and_data_uris():
    html = """
      <img src="/img/hero-photo.jpg" width="800" height="600">
      <img src="https://cdn.example.com/assets/site-logo.png">
      <img src="/tracking/pixel.gif">
      <img src="https://a.example.com/favicon.ico">
      <img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
      <img src="/img/spacer.gif" width="1" height="1">
      <img src="/diagram.png">
    """
    cands = images.image_candidates(html, "https://example.com/article")
    assert cands == [
        "https://example.com/img/hero-photo.jpg",
        "https://example.com/diagram.png",
    ]


# --- fallback chain --------------------------------------------


def test_source_page_image_is_chosen(fake_fetch):
    su = make_url()
    card = make_card(su)
    url = "https://example.com/img/hero-photo.jpg"
    fake_fetch({url: images.FetchedImage(png_bytes(600, 400), "image/jpeg")})
    dt = FakeDrawThings(png_bytes(512, 512))

    outcome = images.choose_card_image(card, [url], dt)

    assert outcome.source == Card.ImageSource.SOURCE_PAGE
    assert outcome.data is not None
    assert dt.calls == []  # Draw Things never asked


def test_no_usable_source_images_calls_draw_things(fake_fetch):
    su = make_url()
    card = make_card(su)
    small = "https://example.com/tiny.png"
    fake_fetch({small: images.FetchedImage(png_bytes(20, 20), "image/png")})
    dt = FakeDrawThings(png_bytes(512, 512))

    outcome = images.choose_card_image(card, [small], dt)

    assert dt.calls, "Draw Things should have been called"
    assert outcome.source == Card.ImageSource.DRAW_THINGS
    assert outcome.data is not None


def test_fetch_failure_falls_through_to_next_candidate_then_draw_things(fake_fetch):
    su = make_url()
    card = make_card(su)
    broken = "https://example.com/404.png"
    unusable = "https://example.com/small.png"
    fake_fetch(
        {
            broken: httpx.ConnectError("connection refused"),
            unusable: images.FetchedImage(png_bytes(10, 10), "image/png"),
        }
    )
    dt = FakeDrawThings(png_bytes(512, 512))

    outcome = images.choose_card_image(card, [broken, unusable], dt)

    assert dt.calls
    assert outcome.source == Card.ImageSource.DRAW_THINGS


def test_fetch_failure_falls_through_to_next_usable_candidate(fake_fetch):
    su = make_url()
    card = make_card(su)
    broken = "https://example.com/404.png"
    good = "https://example.com/good.png"
    fake_fetch(
        {
            broken: httpx.ReadTimeout("timeout"),
            good: images.FetchedImage(png_bytes(500, 500), "image/png"),
        }
    )
    dt = FakeDrawThings(png_bytes(512, 512))

    outcome = images.choose_card_image(card, [broken, good], dt)

    assert outcome.source == Card.ImageSource.SOURCE_PAGE
    assert dt.calls == []


def test_draw_things_unreachable_yields_no_image_and_no_error(fake_fetch, monkeypatch):
    su = make_url()
    card = make_card(su)

    def _boom(url, payload, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(images, "_draw_things_post", _boom)
    client = images.DrawThingsClient("http://127.0.0.1:7860", enabled=True)

    outcome = images.choose_card_image(card, [], client)

    assert outcome.data is None
    assert outcome.source == Card.ImageSource.NONE


def test_draw_things_error_or_empty_result_yields_no_image(monkeypatch):
    su = make_url()
    card = make_card(su)

    # HTTP error response
    def _error(url, payload, timeout):
        request = httpx.Request("POST", url)
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr(images, "_draw_things_post", _error)
    client = images.DrawThingsClient(enabled=True)
    assert client.generate("prompt") is None

    # Empty result
    monkeypatch.setattr(images, "_draw_things_post", lambda *a: {"images": []})
    assert client.generate("prompt") is None


def test_draw_things_disabled_is_skipped(monkeypatch):
    called = []
    monkeypatch.setattr(
        images, "_draw_things_post", lambda *a: called.append(1) or {"images": ["x"]}
    )
    client = images.DrawThingsClient(enabled=False)
    assert client.generate("prompt") is None
    assert called == []


# --- attach_images: persistence + invariants -------------------


def test_attach_images_stores_source_image_and_placement(monkeypatch, fake_fetch):
    su = make_url()
    basic = make_card(su, note_type="basic")
    cloze = make_card(
        su, note_type="cloze", front="The {{c1::mitochondrion}} makes ATP."
    )
    html = '<img src="/hero.jpg" width="900" height="700">'
    monkeypatch.setattr(images, "_fetch_page_html", lambda url: html)
    fake_fetch(
        {
            "https://example.com/hero.jpg": images.FetchedImage(
                png_bytes(700, 500), "image/jpeg"
            )
        }
    )
    dt = FakeDrawThings(None)

    images.attach_images(su, [basic, cloze], draw_things=dt)

    for card in (basic, cloze):
        card.refresh_from_db()
        assert card.image_source == Card.ImageSource.SOURCE_PAGE
        assert bool(card.image) is True
    assert basic.image_placement == "answer"
    assert cloze.image_placement == "question"
    assert dt.calls == []


def test_attach_images_never_aborts_when_draw_things_down(monkeypatch):
    su = make_url()
    cards = [make_card(su, source_term=f"t{i}") for i in range(3)]
    monkeypatch.setattr(images, "_fetch_page_html", lambda url: "<html></html>")

    def _boom(url, payload, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(images, "_draw_things_post", _boom)

    images.attach_images(su, cards, draw_things=images.DrawThingsClient(enabled=True))

    for card in cards:
        card.refresh_from_db()
        assert not card.image
        assert card.image_source == Card.ImageSource.NONE


def test_attach_images_zero_or_one_image_per_card(monkeypatch, fake_fetch):
    su = make_url()
    card = make_card(su)
    html = '<img src="/a.png" width="800" height="800"><img src="/b.png" width="800" height="800">'
    monkeypatch.setattr(images, "_fetch_page_html", lambda url: html)
    fake_fetch(
        {
            "https://example.com/a.png": images.FetchedImage(
                png_bytes(600, 600), "image/png"
            ),
            "https://example.com/b.png": images.FetchedImage(
                png_bytes(600, 600), "image/png"
            ),
        }
    )

    images.attach_images(su, [card], draw_things=FakeDrawThings(None))

    card.refresh_from_db()
    # exactly one file reference, from the first candidate
    assert bool(card.image) is True
    assert card.image_source == Card.ImageSource.SOURCE_PAGE
    assert card.image.name.startswith("cards/")


def test_generation_still_succeeds_when_image_step_raises(monkeypatch):
    """attach_images blowing up must not fail card generation."""
    from submissions import generation

    monkeypatch.setattr(
        generation.images,
        "attach_images",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("kaboom")),
    )

    class _Result:
        parsed = {
            "cards": [
                {
                    "note_type": "basic",
                    "front": "Q?",
                    "back": "A.",
                    "source_term": "x",
                    "topic": "",
                }
            ]
        }
        text = ""

    monkeypatch.setattr(
        generation.llm, "generate", lambda **kw: _Result()
    )
    monkeypatch.setattr(generation.dedup, "dedup_cards", lambda cards: None)
    su = make_url()

    result = generation.generate_for(su)

    assert result.outcome == "created"
    assert su.cards.count() == 1
