"""Tests for the batch card-review grid (issue #9)."""

import pytest
from django.urls import reverse

from submissions import anki
from submissions.models import Batch, BatchRequest, Card, SubmittedURL
from tests.test_anki import FakeAnki

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


# --- issue #78: graceful handling of the dedup-timing race -------------


def test_decision_on_now_duplicate_card_is_graceful_not_404(client):
    """A card whose dedup_status is (or becomes) "duplicate" must get a
    handled response from card_review_decision, not a raw 404 (#78)."""
    batch = Batch.objects.create()
    su = _url(batch)
    keep = _card(su, batch)
    duplicate = _card(
        su, batch, front="Q2", source_term="Y",
        dedup_status=Card.DedupStatus.DUPLICATE, duplicate_of=keep,
    )

    resp = _decide(client, batch, duplicate, "accepted")

    assert resp.status_code != 404
    assert resp.status_code == 409
    payload = resp.json()
    assert "error" in payload
    assert payload["dedup_duplicate"] is True

    # No decision was recorded on the duplicate card.
    duplicate.refresh_from_db()
    assert duplicate.review_status == Card.ReviewStatus.UNDECIDED


def test_decision_on_now_duplicate_card_non_xhr_is_graceful_not_404(client):
    """The non-XHR fallback (a plain form POST) also gets a handled
    response - an error message + redirect - not a raw 404 (#78)."""
    batch = Batch.objects.create()
    su = _url(batch)
    keep = _card(su, batch)
    duplicate = _card(
        su, batch, front="Q2", source_term="Y",
        dedup_status=Card.DedupStatus.DUPLICATE, duplicate_of=keep,
    )

    resp = client.post(
        reverse("submissions:card_review_decision", args=[batch.pk, duplicate.pk]),
        {"decision": "accepted"},
    )

    assert resp.status_code != 404
    assert resp.status_code in (302, 200)


def test_decision_on_other_cards_unaffected_by_one_dedup_race(client):
    """The reviewer can keep acting on the rest of the batch after a #78
    dedup-race error on one card - no other card's state is disturbed."""
    batch = Batch.objects.create()
    su = _url(batch)
    keep = _card(su, batch)
    other = _card(su, batch, front="Q2", source_term="Y")
    duplicate = _card(
        su, batch, front="Q3", source_term="Z",
        dedup_status=Card.DedupStatus.DUPLICATE, duplicate_of=keep,
    )

    _decide(client, batch, duplicate, "accepted")
    resp = _decide(client, batch, other, "accepted")

    assert resp.status_code == 200
    other.refresh_from_db()
    assert other.review_status == Card.ReviewStatus.ACCEPTED


def test_decision_on_truly_nonexistent_card_still_404s(client):
    """A card that never existed / isn't in this batch is still a real
    404 - only the dedup-race case is special-cased (#78)."""
    batch = Batch.objects.create()
    _url(batch)

    resp = _decide(client, batch, type("C", (), {"pk": 999999})(), "accepted")
    assert resp.status_code == 404


def test_edit_on_now_duplicate_card_is_graceful_not_404(client):
    batch = Batch.objects.create()
    su = _url(batch)
    keep = _card(su, batch)
    duplicate = _card(
        su, batch, front="Q2", source_term="Y",
        dedup_status=Card.DedupStatus.DUPLICATE, duplicate_of=keep,
    )

    resp = client.post(
        reverse("submissions:card_review_edit", args=[batch.pk, duplicate.pk]),
        {"front": "New front", "back": "New back"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert resp.status_code != 404
    assert resp.status_code == 409
    assert "error" in resp.json()


def test_revert_edit_on_now_duplicate_card_is_graceful_not_404(client):
    batch = Batch.objects.create()
    su = _url(batch)
    keep = _card(su, batch)
    duplicate = _card(
        su, batch, front="Q2", source_term="Y",
        dedup_status=Card.DedupStatus.DUPLICATE, duplicate_of=keep,
    )

    resp = client.post(
        reverse("submissions:card_review_revert_edit", args=[batch.pk, duplicate.pk]),
        {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert resp.status_code != 404
    assert resp.status_code == 409


@pytest.mark.parametrize(
    "view_name,method,extra",
    [
        ("submissions:card_review_image_candidates", "get", {}),
        ("submissions:card_review_image_select", "post", {"candidate_url": "https://example.com/x.png"}),
        ("submissions:card_review_image_regenerate", "post", {}),
        ("submissions:card_review_image_remove", "post", {}),
        ("submissions:card_review_image_revert", "post", {}),
    ],
)
def test_image_endpoints_on_now_duplicate_card_are_graceful_not_404(
    client, view_name, method, extra
):
    batch = Batch.objects.create()
    su = _url(batch)
    keep = _card(su, batch)
    duplicate = _card(
        su, batch, front="Q2", source_term="Y",
        dedup_status=Card.DedupStatus.DUPLICATE, duplicate_of=keep,
    )

    do_request = getattr(client, method)
    resp = do_request(
        reverse(view_name, args=[batch.pk, duplicate.pk]),
        extra,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert resp.status_code != 404
    assert resp.status_code == 409
    assert "error" in resp.json()


def test_finish_with_undecided_requires_confirm(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    c2 = _card(su, batch, front="Q2", source_term="Y")
    _decide(client, batch, c1, "accepted")

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    finish_url = reverse("submissions:card_review_finish", args=[batch.pk])
    resp = client.post(finish_url, {"deck_choice": "Flashcard Generator"})
    assert resp.status_code == 200
    assert resp.context["confirm_undecided"] == 1
    c2.refresh_from_db()
    assert c2.review_status == Card.ReviewStatus.UNDECIDED  # untouched
    # issue #57: the confirm-needed re-render branch does not finish the
    # batch, so it must not push any note (the deckNames call from the
    # best-effort picker context is display-only and always allowed).
    assert fake.notes_added() == []
    assert all(a != "createDeck" for a, _ in fake.calls)
    assert all(a != "addNote" for a, _ in fake.calls)

    resp = client.post(
        finish_url, {"deck_choice": "Flashcard Generator", "confirm": "1"}, follow=True
    )
    assert resp.status_code == 200
    c2.refresh_from_db()
    assert c2.review_status == Card.ReviewStatus.UNDECIDED  # still undecided

    # issue #57: the branch that actually finishes the batch enqueues a
    # Huey task that pushes accepted+unsynced cards to Anki, inline here
    # thanks to the autouse `_huey_immediate` fixture.
    c1.refresh_from_db()
    assert fake.notes_added() == [
        {
            "deckName": "Flashcard Generator",
            "modelName": "Basic",
            "fields": {"Front": "What is X?", "Back": "X is a thing."},
            "tags": anki.card_tags(c1),
            "options": {"allowDuplicate": False},
        }
    ]
    assert c1.synced_at is not None


def test_finish_with_zero_accepted_cards_is_safe_noop(client, monkeypatch):
    """Finishing a batch where nothing was accepted still enqueues the
    task safely; push_accepted_cards() on an empty queryset is a no-op."""
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide(client, batch, c1, "rejected")

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    finish_url = reverse("submissions:card_review_finish", args=[batch.pk])
    resp = client.post(
        finish_url, {"deck_choice": "Flashcard Generator"}, follow=True
    )
    assert resp.status_code == 200
    assert fake.notes_added() == []


def test_finish_response_unaffected_when_anki_unreachable(client, monkeypatch):
    """issue #57: an unreachable Anki at push time never surfaces in the
    finish response or leaves the card synced - it's a later backstop."""
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide(client, batch, c1, "accepted")

    fake = FakeAnki(unreachable=True)
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    finish_url = reverse("submissions:card_review_finish", args=[batch.pk])
    # Typed deck name: Finish is never blocked by an unreachable Anki, the
    # push itself stays unsynced via the existing backstop.
    resp = client.post(finish_url, {"deck_name": "Typed Deck"}, follow=True)
    assert resp.status_code == 200

    c1.refresh_from_db()
    assert c1.synced_at is None


def test_finishing_same_batch_twice_does_not_duplicate_notes(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide(client, batch, c1, "accepted")

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    finish_url = reverse("submissions:card_review_finish", args=[batch.pk])
    client.post(finish_url, {"deck_choice": "Flashcard Generator"}, follow=True)
    assert len(fake.notes_added()) == 1

    # Double-click / re-POST: the second task run finds the card already
    # synced and pushes nothing new.
    client.post(finish_url, {"deck_choice": "Flashcard Generator"}, follow=True)
    assert len(fake.notes_added()) == 1


def test_accepting_cards_without_finishing_does_not_push(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    c2 = _card(su, batch, front="Q2", source_term="Y")

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _decide(client, batch, c1, "accepted")
    _decide(client, batch, c2, "accepted")

    assert fake.calls == []
    c1.refresh_from_db()
    c2.refresh_from_db()
    assert c1.synced_at is None
    assert c2.synced_at is None


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


# --- inline edit of card text (issue #24) ---------------------------------


def _edit(client, batch, card, **fields):
    return client.post(
        reverse("submissions:card_review_edit", args=[batch.pk, card.pk]),
        fields,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


def _revert_edit(client, batch, card):
    return client.post(
        reverse("submissions:card_review_revert_edit", args=[batch.pk, card.pk]),
        {},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


def test_basic_edit_persists_and_is_reload_visible(client):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)

    resp = _edit(client, batch, card, front="New front?", back="New back.")
    assert resp.status_code == 200
    assert resp.json()["front"] == "New front?"

    card.refresh_from_db()
    assert card.front == "New front?"
    assert card.back == "New back."
    assert card.is_edited is True
    assert card.edited_at is not None
    assert card.original_front == "What is X?"
    assert card.original_back == "X is a thing."

    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    content = page.content.decode()
    assert "New front?" in content
    assert "edited" in content


def test_cloze_edit_saves_valid_multi_deletion(client):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(
        su, batch, note_type=Card.NoteType.CLOZE,
        front="The {{c1::sky}} is blue.", back="", source_term="sky",
    )

    resp = _edit(
        client, batch, card,
        front="The {{c1::sky}} is {{c2::blue}} today.",
    )
    assert resp.status_code == 200
    card.refresh_from_db()
    assert card.front == "The {{c1::sky}} is {{c2::blue}} today."
    assert card.is_edited is True
    assert card.original_front == "The {{c1::sky}} is blue."


def test_invalid_cloze_edit_is_refused(client):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(
        su, batch, note_type=Card.NoteType.CLOZE,
        front="The {{c1::sky}} is blue.", back="", source_term="sky",
    )

    resp = _edit(client, batch, card, front="The sky is blue, no markers.")
    assert resp.status_code == 400
    assert "error" in resp.json()

    card.refresh_from_db()
    assert card.front == "The {{c1::sky}} is blue."  # unchanged
    assert card.is_edited is False


def test_empty_or_whitespace_basic_fields_are_refused(client):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)

    for fields in (
        {"front": "", "back": "has back"},
        {"front": "has front", "back": "   "},
        {"front": "  ", "back": "\t"},
    ):
        resp = _edit(client, batch, card, **fields)
        assert resp.status_code == 400

    card.refresh_from_db()
    assert card.front == "What is X?"
    assert card.back == "X is a thing."
    assert card.is_edited is False


def test_second_edit_keeps_first_original(client):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)

    _edit(client, batch, card, front="Edit one", back="Back one.")
    _edit(client, batch, card, front="Edit two", back="Back two.")

    card.refresh_from_db()
    assert card.front == "Edit two"
    assert card.original_front == "What is X?"  # first snapshot kept


def test_revert_restores_original_and_clears_indicator(client):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)

    _edit(client, batch, card, front="Changed", back="Changed back.")
    resp = _revert_edit(client, batch, card)
    assert resp.status_code == 200
    assert resp.json()["is_edited"] is False

    card.refresh_from_db()
    assert card.front == "What is X?"
    assert card.back == "X is a thing."
    assert card.is_edited is False
    assert card.edited_at is None

    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert 'data-role="edited-badge"' in page.content.decode()
    assert page.content.decode().count("edited</span>") == 1  # badge hidden


def test_edit_does_not_change_decision_state(client):
    batch = Batch.objects.create()
    su = _url(batch)
    undecided = _card(su, batch)
    accepted = _card(su, batch, front="A2", source_term="Y")
    rejected = _card(su, batch, front="A3", source_term="Z")
    _decide(client, batch, accepted, "accepted")
    _decide(client, batch, rejected, "rejected", reason="nope")

    _edit(client, batch, undecided, front="U?", back="U.")
    _edit(client, batch, accepted, front="A2?", back="A2.")
    _edit(client, batch, rejected, front="A3?", back="A3.")

    for c, status in (
        (undecided, Card.ReviewStatus.UNDECIDED),
        (accepted, Card.ReviewStatus.ACCEPTED),
        (rejected, Card.ReviewStatus.REJECTED),
    ):
        c.refresh_from_db()
        assert c.review_status == status
    rejected.refresh_from_db()
    assert rejected.rejection_reason == "nope"  # reason kept


def test_edit_one_card_leaves_other_cards_decision_intact(client):
    batch = Batch.objects.create()
    su = _url(batch)
    first = _card(su, batch)
    second = _card(su, batch, front="Q2", source_term="Y")

    # concurrent-safe ordering: accept the second card, then save an edit
    # on the first; neither clobbers the other.
    _decide(client, batch, second, "accepted")
    resp = _edit(client, batch, first, front="First?", back="First.")
    assert resp.status_code == 200

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.front == "First?"
    assert first.review_status == Card.ReviewStatus.UNDECIDED
    assert second.review_status == Card.ReviewStatus.ACCEPTED
    assert second.front == "Q2"


def test_feedback_stores_edited_content_and_notes_edited(client):
    from submissions.models import Feedback

    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch, tags={"source_url": su.url})

    _edit(client, batch, card, front="Edited?", back="Edited.")
    _decide(client, batch, card, "accepted")

    fb = Feedback.objects.get()
    assert fb.front == "Edited?"
    assert fb.back == "Edited."
    assert fb.was_edited is True


def test_feedback_unedited_card_notes_not_edited(client):
    from submissions.models import Feedback

    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)

    _decide(client, batch, card, "accepted")

    fb = Feedback.objects.get()
    assert fb.front == "What is X?"
    assert fb.was_edited is False


def test_anki_note_uses_edited_content():
    from submissions import anki

    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)
    card.front = "Edited front?"
    card.back = "Edited back."
    card.save(update_fields=["front", "back"])

    note = anki.build_note(card, "Deck")
    assert note["fields"] == {"Front": "Edited front?", "Back": "Edited back."}


def test_empty_and_notready_states_render_no_edit_controls(client):
    empty_batch = Batch.objects.create()
    _url(empty_batch)
    page = client.get(reverse("submissions:card_review", args=[empty_batch.pk]))
    assert b'data-role="edit-open"' not in page.content

    notready = Batch.objects.create()
    _url(notready, status=SubmittedURL.Status.PENDING, gen="")
    page = client.get(reverse("submissions:card_review", args=[notready.pk]))
    assert b'data-role="edit-open"' not in page.content


def test_review_grid_renders_edit_and_revert_controls(client):
    batch = Batch.objects.create()
    su = _url(batch)
    _card(su, batch)
    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    content = page.content.decode()
    assert 'data-role="edit-open"' in content
    assert 'data-role="edit-wrap"' in content
    # note type + source URL visible, not editable: no textarea for them
    assert 'name="note_type"' not in content
