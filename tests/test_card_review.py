"""Tests for the batch card-review grid (issue #9)."""

import pytest
from django.urls import reverse

from submissions.models import Batch, BatchRequest, Card, SubmittedURL

pytestmark = pytest.mark.django_db


def _url(batch, *, status=SubmittedURL.Status.OK, gen="ok", n=0):
    su = SubmittedURL.objects.create(
        url=f"https://example.com/{batch.pk}-{n}",
        status=status,
        generation_status=gen,
    )
    BatchRequest.objects.create(batch=batch, submitted_url=su)
    return su


def _card(su, batch, **kw):
    opts = dict(
        submitted_url=su,
        batch=batch,
        note_type=Card.NoteType.BASIC,
        front="What is X?",
        back="X is a thing.",
        source_term="X",
        tags={},
    )
    opts.update(kw)
    return Card.objects.create(**opts)


def _decide(client, batch, card, decision, reason=None):
    data = {"decision": decision}
    if reason is not None:
        data["reason"] = reason
    return client.post(
        reverse("submissions:card_review_decision", args=[batch.pk, card.pk]),
        data,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


def test_decision_is_stored_and_visible_after_reload(client):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)

    resp = _decide(client, batch, card, "accepted")
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "accepted"

    card.refresh_from_db()
    assert card.review_status == Card.ReviewStatus.ACCEPTED

    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert b'review-card--accepted' in page.content
    assert page.context["tally"]["accepted"] == 1


def test_rejection_reason_is_optional(client):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    c2 = _card(su, batch, front="Q2", source_term="Y")

    _decide(client, batch, c1, "rejected")  # no reason
    _decide(client, batch, c2, "rejected", reason="off topic")

    c1.refresh_from_db()
    c2.refresh_from_db()
    assert c1.review_status == Card.ReviewStatus.REJECTED
    assert c1.rejection_reason == ""
    assert c2.rejection_reason == "off topic"

    # reason survives reload and can be edited later while staying rejected
    _decide(client, batch, c1, "rejected", reason="added later")
    c1.refresh_from_db()
    assert c1.review_status == Card.ReviewStatus.REJECTED
    assert c1.rejection_reason == "added later"


def test_switching_decision_updates_stored_value(client):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)

    _decide(client, batch, card, "rejected", reason="nope")
    card.refresh_from_db()
    assert card.rejection_reason == "nope"

    _decide(client, batch, card, "accepted")
    card.refresh_from_db()
    assert card.review_status == Card.ReviewStatus.ACCEPTED
    assert card.rejection_reason == ""  # accepting never keeps a reason


def test_empty_batch_shows_empty_state(client):
    batch = Batch.objects.create()
    _url(batch)  # extracted + generated, but produced no cards

    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert page.context["ready"] is True
    assert b"No cards were generated for this batch" in page.content
    assert b'<ul class="review-grid">' not in page.content


def test_cards_not_ready_state(client):
    batch = Batch.objects.create()
    _url(batch, status=SubmittedURL.Status.PENDING, gen="")

    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert page.context["ready"] is False
    assert b"not ready" in page.content
    assert b'<ul class="review-grid">' not in page.content


def test_dedup_duplicates_excluded_from_grid(client):
    batch = Batch.objects.create()
    su = _url(batch)
    keep = _card(su, batch)
    _card(su, batch, dedup_status=Card.DedupStatus.DUPLICATE, duplicate_of=keep)

    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert page.context["tally"]["total"] == 1


def test_finish_with_undecided_requires_confirm(client):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    c2 = _card(su, batch, front="Q2", source_term="Y")
    _decide(client, batch, c1, "accepted")

    finish_url = reverse("submissions:card_review_finish", args=[batch.pk])
    resp = client.post(finish_url)
    assert resp.status_code == 200
    assert resp.context["confirm_undecided"] == 1
    c2.refresh_from_db()
    assert c2.review_status == Card.ReviewStatus.UNDECIDED  # untouched

    resp = client.post(finish_url, {"confirm": "1"}, follow=True)
    assert resp.status_code == 200
    c2.refresh_from_db()
    assert c2.review_status == Card.ReviewStatus.UNDECIDED  # still undecided


def test_review_grid_renders_card_image_by_placement(client):
    batch = Batch.objects.create()
    su = _url(batch)
    cloze = _card(
        su, batch, note_type=Card.NoteType.CLOZE, front="The {{c1::sky}} is blue.",
        back="", source_term="sky", image="cards/cloze.png",
        image_source="source_page",
    )
    basic = _card(
        su, batch, source_term="Y", back="Basic answer text.",
        image="cards/basic.png", image_source="draw_things",
    )
    plain = _card(su, batch, source_term="Z")  # no image

    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    content = page.content.decode()

    assert "cards/cloze.png" in content
    assert "cards/basic.png" in content
    # cloze image sits before its cloze text (question side)
    assert content.index("cards/cloze.png") < content.index("review-card__cloze")
    # basic image sits after its back text (answer side)
    assert content.index("Basic answer text.") < content.index("cards/basic.png")
    # the imageless card renders no <img>
    plain_html = content.split(f'id="card-{plain.pk}"')[1].split("</li>")[0]
    assert "review-card__image" not in plain_html


def test_empty_and_notready_states_use_design_system_panel(client):
    empty_batch = Batch.objects.create()
    _url(empty_batch)
    page = client.get(reverse("submissions:card_review", args=[empty_batch.pk]))
    assert b'class="empty-state review-empty"' in page.content

    notready = Batch.objects.create()
    _url(notready, status=SubmittedURL.Status.PENDING, gen="")
    page = client.get(reverse("submissions:card_review", args=[notready.pk]))
    assert b'class="empty-state review-notready"' in page.content


def test_tally_is_an_aria_live_region(client):
    batch = Batch.objects.create()
    su = _url(batch)
    _card(su, batch)
    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert b'id="review-tally"' in page.content
    assert b'aria-live="polite"' in page.content


def _reason_wrap_is_hidden(content):
    import re

    m = re.search(
        rb'<span class="review-card__reason-wrap" data-role="reason-wrap"([^>]*)>',
        content,
    )
    assert m, "reason wrap span not found"
    return b"hidden" in m.group(1)


@pytest.mark.parametrize(
    "status,hidden",
    [
        (Card.ReviewStatus.ACCEPTED, True),
        (Card.ReviewStatus.UNDECIDED, True),
        (Card.ReviewStatus.REJECTED, False),
    ],
)
def test_reason_wrap_hidden_attr_tracks_decision(client, status, hidden):
    batch = Batch.objects.create()
    su = _url(batch)
    _card(su, batch, review_status=status)
    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert _reason_wrap_is_hidden(page.content) is hidden


def test_reason_wrap_css_has_hidden_guard():
    from pathlib import Path

    from django.conf import settings

    css = Path(settings.BASE_DIR, "submissions/static/submissions/app.css").read_text()
    assert ".review-card__reason-wrap[hidden]" in css


def test_batch_detail_links_to_review(client):
    batch = Batch.objects.create()
    page = client.get(reverse("submissions:batch_detail", args=[batch.pk]))
    assert reverse("submissions:card_review", args=[batch.pk]).encode() in page.content
