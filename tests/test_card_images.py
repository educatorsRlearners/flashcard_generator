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


# --- extension-submitted candidates (issue #42) -----------------------


def test_merged_candidates_without_extension_images_matches_server_list(
    monkeypatch,
):
    su = make_url()
    assert su.extension_image_urls == []
    html = '<img src="/a.png" width="800" height="800">'
    monkeypatch.setattr(images, "_fetch_page_html", lambda url: html)
    assert images._merged_candidates(su) == ["https://example.com/a.png"]


def test_attach_images_merges_extension_first_with_dedup(monkeypatch):
    su = make_url()
    ext_only = "https://example.com/ext-only.png"
    shared = "https://example.com/shared.png"
    server_only = "https://example.com/server-only.png"
    su.extension_image_urls = [ext_only, shared]
    su.save(update_fields=["extension_image_urls"])
    html = (
        f'<img src="{shared}" width="800" height="800">'
        f'<img src="{server_only}" width="800" height="800">'
    )
    monkeypatch.setattr(images, "_fetch_page_html", lambda url: html)

    calls: list[str] = []

    def _fetch(url: str):
        calls.append(url)
        if url == server_only:
            return images.FetchedImage(png_bytes(600, 600), "image/png")
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(images, "_fetch_image", _fetch)
    card = make_card(su)

    images.attach_images(su, [card], draw_things=FakeDrawThings(None))

    # Extension candidates first in order, shared URL fetched only once
    # despite appearing in both sources, server-only candidate last.
    assert calls == [ext_only, shared, server_only]
    card.refresh_from_db()
    assert card.image_source == Card.ImageSource.SOURCE_PAGE
    assert bool(card.image) is True


def test_attach_images_uses_extension_list_when_server_refetch_fails(
    monkeypatch, fake_fetch
):
    su = make_url()
    url = "https://example.com/ext-hero.png"
    su.extension_image_urls = [url]
    su.save(update_fields=["extension_image_urls"])
    # Authenticated / JS-only page: the static re-fetch sees nothing.
    monkeypatch.setattr(images, "_fetch_page_html", lambda page_url: None)
    fake_fetch({url: images.FetchedImage(png_bytes(600, 600), "image/png")})
    card = make_card(su)

    images.attach_images(su, [card], draw_things=FakeDrawThings(None))

    card.refresh_from_db()
    assert card.image_source == Card.ImageSource.SOURCE_PAGE
    assert bool(card.image) is True


def test_merged_candidates_capped_at_max(monkeypatch):
    su = make_url()
    su.extension_image_urls = [f"https://example.com/e{i}.png" for i in range(20)]
    su.save(update_fields=["extension_image_urls"])
    html = "".join(
        f'<img src="/s{i}.png" width="800" height="800">' for i in range(20)
    )
    monkeypatch.setattr(images, "_fetch_page_html", lambda url: html)

    merged = images._merged_candidates(su)

    assert len(merged) == images.MAX_IMAGE_CANDIDATES
    assert merged[:20] == [f"https://example.com/e{i}.png" for i in range(20)]
    assert merged[20:] == [f"https://example.com/s{i}.png" for i in range(5)]


def test_merged_candidates_tolerates_missing_extension_field(monkeypatch):
    class _Stub:
        url = "https://example.com/article"

    monkeypatch.setattr(
        images,
        "_fetch_page_html",
        lambda url: '<img src="/a.png" width="800" height="800">',
    )
    assert images._merged_candidates(_Stub()) == ["https://example.com/a.png"]


def test_filter_absolute_candidates_applies_server_exclusions():
    candidates = [
        "https://example.com/photo.jpg",
        "https://cdn.example.com/site-logo.png",  # chrome marker
        "https://example.com/tracking/pixel.gif",  # chrome marker
        "data:image/gif;base64,AAA",  # not http(s)
        "ftp://example.com/photo.jpg",  # not http(s)
        "https://example.com/photo.jpg",  # duplicate
        "https://example.com/vector.svg",  # non-raster extension
        "https://example.com/extensionless-path",  # no extension -> kept
    ]
    assert images._filter_absolute_candidates(candidates) == [
        "https://example.com/photo.jpg",
        "https://example.com/extensionless-path",
    ]


# --- manual image replacement from the review grid (issue #26) ---------
#
# These tests exercise the review-grid endpoints (per-card POST / fetch),
# reusing #12's discovery + Draw Things client with the same fakes as above.

import json as _json

from django.urls import reverse as _reverse

from submissions.models import Batch as _Batch
from submissions.models import BatchRequest as _BatchRequest
from submissions.models import Feedback as _Feedback


def _review_batch():
    batch = _Batch.objects.create()
    su = make_url(url=f"https://example.com/review-{batch.pk}")
    _BatchRequest.objects.create(batch=batch, submitted_url=su)
    return batch, su


def _img_post(client, batch, card, action, data=None):
    return client.post(
        _reverse(f"submissions:card_review_image_{action}", args=[batch.pk, card.pk]),
        data or {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


def _review_url(batch):
    return _reverse("submissions:card_review", args=[batch.pk])


def _attach_bytes(card, data, source="source_page"):
    card.image.save(f"card_{card.pk}.png", ContentFile(data), save=False)
    card.image_source = source
    card.save(update_fields=["image", "image_source"])
    card.refresh_from_db()
    return card


from django.core.files.base import ContentFile  # noqa: E402


def test_select_candidate_persists_and_is_reload_visible(client, fake_fetch):
    batch, su = _review_batch()
    card = make_card(su)
    old_bytes = png_bytes(600, 600, color=(1, 2, 3))
    _attach_bytes(card, old_bytes)
    old_name = card.image.name

    good_url = "https://example.com/review-good.png"
    new_bytes = png_bytes(600, 600, color=(9, 9, 9))
    fake_fetch({good_url: images.FetchedImage(new_bytes, "image/png")})

    resp = _img_post(client, batch, card, "select", {"candidate_url": good_url})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["image_manually_set"] is True
    assert payload["image_source"] == "source_page"

    card.refresh_from_db()
    assert card.image.name != old_name
    assert card.image_manually_set is True
    assert card.image_source == Card.ImageSource.SOURCE_PAGE
    # original #12 pick retained for revert
    assert card.original_image.name == old_name
    assert card.original_image_source == "source_page"

    page = client.get(_review_url(batch))
    assert card.image.name.encode() in page.content


def test_select_failing_candidate_keeps_old_image(client, fake_fetch):
    import httpx as _httpx

    batch, su = _review_batch()
    card = make_card(su)
    _attach_bytes(card, png_bytes(600, 600))
    old_name = card.image.name

    bad_url = "https://example.com/review-404.png"
    fake_fetch({bad_url: _httpx.ConnectError("refused")})

    resp = _img_post(client, batch, card, "select", {"candidate_url": bad_url})
    assert resp.status_code == 400
    assert "error" in resp.json()

    card.refresh_from_db()
    assert card.image.name == old_name
    assert card.image_manually_set is False


def test_select_unusable_candidate_keeps_old_image(client, fake_fetch):
    batch, su = _review_batch()
    card = make_card(su)
    _attach_bytes(card, png_bytes(600, 600))
    old_name = card.image.name

    tiny_url = "https://example.com/review-tiny.png"
    fake_fetch({tiny_url: images.FetchedImage(png_bytes(10, 10), "image/png")})

    resp = _img_post(client, batch, card, "select", {"candidate_url": tiny_url})
    assert resp.status_code == 400

    card.refresh_from_db()
    assert card.image.name == old_name


def test_regenerate_replaces_image_and_marks_manual(client, monkeypatch):
    batch, su = _review_batch()
    card = make_card(su)
    _attach_bytes(card, png_bytes(600, 600, color=(1, 2, 3)))
    old_name = card.image.name
    fresh = png_bytes(512, 512, color=(200, 1, 1))

    class _DT:
        def __init__(self, *a, **kw):
            pass

        def generate(self, prompt):
            assert prompt  # same #12 prompt builder feeds the client
            return fresh

    monkeypatch.setattr(images, "DrawThingsClient", _DT)

    resp = _img_post(client, batch, card, "regenerate")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["image_manually_set"] is True
    assert payload["image_source"] == "draw_things"

    card.refresh_from_db()
    assert card.image.name != old_name
    assert card.image_source == Card.ImageSource.DRAW_THINGS
    assert card.image_manually_set is True
    assert card.original_image.name == old_name  # original retained


def test_failed_regenerate_keeps_old_image_with_message(client, monkeypatch):
    batch, su = _review_batch()
    card = make_card(su)
    _attach_bytes(card, png_bytes(600, 600))
    old_name = card.image.name

    class _DT:
        def __init__(self, *a, **kw):
            pass

        def generate(self, prompt):
            return None  # unreachable / error / empty

    monkeypatch.setattr(images, "DrawThingsClient", _DT)

    resp = _img_post(client, batch, card, "regenerate")
    assert resp.status_code == 400
    assert "error" in resp.json()

    card.refresh_from_db()
    assert card.image.name == old_name
    assert card.image_manually_set is False


def test_regenerate_when_draw_things_disabled_reports_disabled(client, settings):
    settings.DRAW_THINGS_ENABLED = False
    batch, su = _review_batch()
    card = make_card(su)

    resp = _img_post(client, batch, card, "regenerate")
    assert resp.status_code == 400
    assert "disabled" in resp.json()["error"].lower()

    card.refresh_from_db()
    assert not card.image
    assert card.image_manually_set is False


def test_remove_then_revert_image(client):
    batch, su = _review_batch()
    card = make_card(su)
    _attach_bytes(card, png_bytes(600, 600), source="draw_things")
    old_name = card.image.name

    resp = _img_post(client, batch, card, "remove")
    assert resp.status_code == 200
    assert resp.json()["image_source"] == "none"

    card.refresh_from_db()
    assert not card.image
    assert card.image_source == Card.ImageSource.NONE
    assert card.image_manually_set is True
    assert card.original_image.name == old_name  # ref retained

    resp = _img_post(client, batch, card, "revert")
    assert resp.status_code == 200
    assert resp.json()["image_manually_set"] is False

    card.refresh_from_db()
    assert card.image.name == old_name
    assert card.image_source == Card.ImageSource.DRAW_THINGS
    assert card.image_manually_set is False


def test_image_replacement_leaves_decision_and_text_untouched(client, fake_fetch):
    batch, su = _review_batch()
    card = make_card(su)
    _attach_bytes(card, png_bytes(600, 600))

    client.post(
        _reverse("submissions:card_review_decision", args=[batch.pk, card.pk]),
        {"decision": "accepted"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    good_url = "https://example.com/review-keep.png"
    fake_fetch({good_url: images.FetchedImage(png_bytes(600, 600), "image/png")})
    resp = _img_post(client, batch, card, "select", {"candidate_url": good_url})
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "accepted"

    card.refresh_from_db()
    assert card.review_status == Card.ReviewStatus.ACCEPTED
    assert card.front == "What is a mitochondrion?"
    assert card.back == "The powerhouse of the cell."

    fb = _Feedback.objects.get()
    assert fb.front == "What is a mitochondrion?"


def test_replacement_uses_final_image_for_anki():
    from submissions import anki as _anki

    batch, su = _review_batch()
    card = make_card(su)
    data = png_bytes(600, 600, color=(5, 5, 5))
    _attach_bytes(card, data)

    assert _anki.card_image_bytes(card) == data


def test_candidates_endpoint_lists_source_candidates(client, monkeypatch):
    batch, su = _review_batch()
    card = make_card(su)
    html = (
        '<img src="/cand-a.png" width="800" height="800">'
        '<img src="/cand-b.png" width="800" height="800">'
    )
    monkeypatch.setattr(images, "_fetch_page_html", lambda url: html)

    resp = client.get(
        _reverse("submissions:card_review_image_candidates", args=[batch.pk, card.pk]),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["candidates"] == [
        "https://example.com/cand-a.png",
        "https://example.com/cand-b.png",
    ]


def test_no_candidates_control_still_renders(client, monkeypatch):
    batch, su = _review_batch()
    card = make_card(su)
    monkeypatch.setattr(images, "_fetch_page_html", lambda url: None)

    resp = client.get(
        _reverse("submissions:card_review_image_candidates", args=[batch.pk, card.pk]),
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.json()["candidates"] == []

    page = client.get(_review_url(batch))
    content = page.content.decode()
    assert 'data-role="image-choose"' in content
    assert 'data-role="image-regen"' in content
    assert 'data-role="image-remove"' in content


def test_cloze_replacement_preview_stays_on_question_side(client, monkeypatch):
    batch, su = _review_batch()
    card = make_card(
        su, note_type="cloze", front="The {{c1::mitochondrion}} makes ATP."
    )
    fresh = png_bytes(512, 512)

    class _DT:
        def __init__(self, *a, **kw):
            pass

        def generate(self, prompt):
            return fresh

    monkeypatch.setattr(images, "DrawThingsClient", _DT)
    _img_post(client, batch, card, "regenerate")

    page = client.get(_review_url(batch))
    content = page.content.decode()
    assert "review-card__image--question" in content
    assert content.index(card.image.name) < content.index("review-card__cloze")
