"""Tests for Engineer B's stream of issue #76 (deck picker UI).

Covers the review Finish form (dropdown + free text, validation, error
path, batch-id task call) and the extension popup passthrough (submit
payload ``deck_name`` + ``GET /api/extension/decks/``). Files owned by
Engineer A (``anki.py``, ``tasks.py``, ``models.py``, ...) are imported,
never edited here.
"""

import json

import pytest
from django.urls import reverse

from submissions import anki
from submissions.extension_auth import mint_token
from submissions.models import Batch, BatchRequest, Card, SubmittedURL
from tests.test_anki import FakeAnki

pytestmark = pytest.mark.django_db


# --- review helpers ----------------------------------------------------


def _url(batch, *, n=0):
    su = SubmittedURL.objects.create(
        url=f"https://example.com/picker-{batch.pk}-{n}",
        status=SubmittedURL.Status.OK,
        generation_status="ok",
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


def _accept(client, batch, card):
    return client.post(
        reverse("submissions:card_review_decision", args=[batch.pk, card.pk]),
        {"decision": "accepted"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


def _finish_url(batch):
    return reverse("submissions:card_review_finish", args=[batch.pk])


def _patch_anki(monkeypatch, fake):
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)


# --- GET review context --------------------------------------------------


def test_review_get_lists_decks_and_marks_available(client, monkeypatch):
    batch = Batch.objects.create()
    _card(_url(batch), batch)
    _patch_anki(monkeypatch, FakeAnki(existing_decks=["Zeta", "Alpha"]))
    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert page.context["deck_names"] == ["Alpha", "Zeta"]
    assert page.context["deck_unavailable"] is False
    assert page.context["stored_deck"] == ""
    content = page.content.decode()
    assert '<select name="deck_choice"' in content
    assert 'id="review-finish-deck-choice"' in content
    assert "<option" in content and "Alpha" in content
    assert 'name="deck_name"' in content


def test_review_get_prefills_stored_deck(client, monkeypatch):
    batch = Batch.objects.create(deck_name="My Deck")
    _card(_url(batch), batch)
    _patch_anki(monkeypatch, FakeAnki(existing_decks=["My Deck", "Other"]))
    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert page.context["stored_deck"] == "My Deck"
    assert page.context["deck_text_value"] == ""
    content = page.content.decode()
    assert '<option value="My Deck" selected>' in content
    # Prefill populates exactly one field: dropdown selected, text empty.
    assert 'id="review-finish-deck-new"' in content
    assert 'id="review-finish-deck-new" value=""' in content


def test_review_get_stored_deck_not_in_live_list_goes_to_text_only(
    client, monkeypatch
):
    batch = Batch.objects.create(deck_name="Offline Deck")
    _card(_url(batch), batch)
    _patch_anki(monkeypatch, FakeAnki(existing_decks=["Other"]))
    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert page.context["stored_deck"] == ""
    assert page.context["deck_text_value"] == "Offline Deck"
    content = page.content.decode()
    assert '<option value="Offline Deck" selected>' not in content
    assert 'value="Offline Deck"' in content
    # Dropdown stays on the placeholder.
    assert "<option value=\"\">Select a deck" in content


def test_review_get_unreachable_degrades_dropdown_keeps_text_input(
    client, monkeypatch
):
    batch = Batch.objects.create()
    _card(_url(batch), batch)
    _patch_anki(monkeypatch, FakeAnki(unreachable=True))
    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert page.context["deck_names"] == []
    assert page.context["deck_unavailable"] is True
    content = page.content.decode()
    assert "Deck list unavailable" in content
    assert 'name="deck_name"' in content  # free text still usable


# --- UX fixes (Engineer B): hints, single-field prefill, a11y ------------


def test_review_shows_typed_wins_hint(client, monkeypatch):
    batch = Batch.objects.create()
    _card(_url(batch), batch)
    _patch_anki(monkeypatch, FakeAnki(existing_decks=["D1"]))
    content = client.get(
        reverse("submissions:card_review", args=[batch.pk])
    ).content.decode()
    assert "If both are filled, the new deck name wins." in content
    assert 'id="review-finish-deck-hint"' in content


def test_popup_shows_typed_wins_and_optional_hints():
    from pathlib import Path

    from django.conf import settings

    html = Path(settings.BASE_DIR, "extension/popup.html").read_text()
    assert "If both are filled, the new deck name wins." in html
    assert "Optional here" in html
    assert "review Finish" in html


def test_popup_select_has_single_accessible_name():
    from pathlib import Path

    from django.conf import settings

    html = Path(settings.BASE_DIR, "extension/popup.html").read_text()
    assert 'for="deck-select"' in html
    assert 'id="deck-select"' in html
    assert 'aria-label="Existing Anki deck"' not in html
    assert 'for="deck-new"' in html
    assert 'id="deck-new"' in html


def test_unavailable_wording_unified_em_dash():
    from pathlib import Path

    from django.conf import settings

    sentence = "Deck list unavailable — type a deck name to continue."
    review = Path(
        settings.BASE_DIR, "submissions/templates/submissions/card_review.html"
    ).read_text()
    popup_js = Path(settings.BASE_DIR, "extension/popup.js").read_text()
    assert sentence in review
    assert sentence in popup_js
    assert "type a name below" not in review
    assert "(Anki unreachable) - type" not in review
    assert "Deck list unavailable — type a name\";" not in popup_js
    assert "(submit still works)" not in popup_js


def test_review_unavailable_prefills_text_only(client, monkeypatch):
    batch = Batch.objects.create(deck_name="Kept Deck")
    _card(_url(batch), batch)
    _patch_anki(monkeypatch, FakeAnki(unreachable=True))
    page = client.get(reverse("submissions:card_review", args=[batch.pk]))
    assert page.context["stored_deck"] == ""
    assert page.context["deck_text_value"] == "Kept Deck"
    assert 'value="Kept Deck"' in page.content.decode()


def test_deck_error_uses_message_error_and_alert(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    _card(su, batch)
    _patch_anki(monkeypatch, FakeAnki(existing_decks=["D1"]))
    content = client.post(_finish_url(batch), {}).content.decode()
    assert 'id="review-finish-deck-error"' in content
    assert 'role="alert"' in content
    assert "message--error" in content
    assert 'for="review-finish-deck-choice"' in content
    assert 'for="review-finish-deck-new"' in content
    assert 'id="review-finish-deck-choice"' in content
    assert 'id="review-finish-deck-new"' in content
    assert "review-finish-deck-hint" in content
    # Both controls link to hint + error ids.
    assert content.count('aria-describedby="review-finish-deck-hint review-finish-deck-error"') >= 2


def test_deck_error_rerender_prefills_single_field(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    _card(su, batch)
    _patch_anki(monkeypatch, FakeAnki(existing_decks=["D1"]))
    # Attempted value not in the live list -> text input only.
    resp = client.post(_finish_url(batch), {"deck_name": 'bad"quote'})
    assert resp.context["stored_deck"] == ""
    assert resp.context["deck_text_value"] == 'bad"quote'


def test_deck_picker_css_rules_and_focus_visible():
    from pathlib import Path

    from django.conf import settings

    css = Path(settings.BASE_DIR, "submissions/static/submissions/app.css").read_text()
    assert ".review-finish__deck" in css
    assert ".review-finish__deck-hint" in css
    assert ".review-finish__deck-error" in css
    for control in ("select:focus-visible", "input:focus-visible", "textarea:focus-visible"):
        assert control in css


# --- Finish with no deck ---------------------------------------------------


def test_finish_with_no_deck_rejected_nothing_pushed(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)
    _accept(client, batch, card)
    fake = FakeAnki(existing_decks=["D1"])
    _patch_anki(monkeypatch, fake)

    for payload in ({}, {"deck_choice": "", "deck_name": ""},
                    {"deck_choice": "", "deck_name": "   "}):
        resp = client.post(_finish_url(batch), payload)
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "review-finish-deck-error" in content
        assert "Choose an Anki deck" in content

    batch.refresh_from_db()
    assert batch.deck_name in (None, "")
    card.refresh_from_db()
    assert card.review_status == Card.ReviewStatus.ACCEPTED  # tally unchanged
    assert card.synced_at is None  # nothing synced
    assert fake.notes_added() == []  # no background push enqueued
    assert all(a != "addNote" for a, _ in fake.calls)


def test_finish_no_deck_beats_undecided_confirm(client, monkeypatch):
    """Missing deck errors even when cards are undecided (no confirm page)."""
    batch = Batch.objects.create()
    su = _url(batch)
    _card(su, batch)
    _patch_anki(monkeypatch, FakeAnki(existing_decks=["D1"]))
    resp = client.post(_finish_url(batch), {})
    assert resp.status_code == 200
    assert "review-finish-deck-error" in resp.content.decode()
    card = batch.cards.get()
    card.refresh_from_db()
    assert card.review_status == Card.ReviewStatus.UNDECIDED  # untouched


# --- Finish with a deck ----------------------------------------------------


def test_finish_with_dropdown_deck_saves_and_pushes(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)
    _accept(client, batch, card)
    fake = FakeAnki(existing_decks=["Picked"])
    _patch_anki(monkeypatch, fake)

    resp = client.post(
        _finish_url(batch), {"deck_choice": "Picked"}, follow=True
    )
    assert resp.status_code == 200
    batch.refresh_from_db()
    assert batch.deck_name == "Picked"
    assert [n["deckName"] for n in fake.notes_added()] == ["Picked"]
    card.refresh_from_db()
    assert card.synced_at is not None


def test_finish_typed_name_wins_over_dropdown(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)
    _accept(client, batch, card)
    fake = FakeAnki(existing_decks=["Dropdown Deck"])
    _patch_anki(monkeypatch, fake)

    client.post(
        _finish_url(batch),
        {"deck_choice": "Dropdown Deck", "deck_name": "  Typed Deck  "},
        follow=True,
    )
    batch.refresh_from_db()
    assert batch.deck_name == "Typed Deck"  # stripped verbatim
    assert [n["deckName"] for n in fake.notes_added()] == ["Typed Deck"]


def test_finish_saves_new_deck_and_creates_it_on_push(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)
    _accept(client, batch, card)
    fake = FakeAnki(existing_decks=[])
    _patch_anki(monkeypatch, fake)

    client.post(_finish_url(batch), {"deck_name": "Brand New"}, follow=True)
    batch.refresh_from_db()
    assert batch.deck_name == "Brand New"
    assert ("createDeck", {"deck": "Brand New"}) in fake.calls
    assert [n["deckName"] for n in fake.notes_added()] == ["Brand New"]


def test_finish_typed_deck_works_when_anki_list_unreachable(
    client, monkeypatch
):
    """Unreachable list degrades the dropdown but never blocks a typed Finish."""
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)
    _accept(client, batch, card)

    calls = {"n": 0}

    class FlakyAnki(FakeAnki):
        def invoke(self, action, **params):
            if action == "deckNames" and calls["n"] == 0:
                calls["n"] += 1
                raise anki.AnkiUnreachableError("down")
            return super().invoke(action, **params)

    _patch_anki(monkeypatch, FlakyAnki(existing_decks=["Typed Deck"]))
    resp = client.post(_finish_url(batch), {"deck_name": "Typed Deck"})
    # Not the deck error page: either the confirm page or a redirect.
    assert "review-finish-deck-error" not in resp.content.decode()
    batch.refresh_from_db()
    assert batch.deck_name == "Typed Deck"


@pytest.mark.parametrize(
    "bad",
    ["A::::B", "::Leading", "Trailing::", 'quo"te', "line\nbreak", "a:: ::b"],
)
def test_finish_rejects_anki_illegal_names(client, monkeypatch, bad):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch)
    _accept(client, batch, card)
    fake = FakeAnki(existing_decks=["D1"])
    _patch_anki(monkeypatch, fake)

    resp = client.post(_finish_url(batch), {"deck_name": bad})
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "review-finish-deck-error" in content
    batch.refresh_from_db()
    assert batch.deck_name in (None, "")
    card.refresh_from_db()
    assert card.synced_at is None
    assert fake.notes_added() == []


def test_finish_with_deck_still_confirms_undecided_first(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    decided = _card(su, batch)
    _card(su, batch, front="Q2", source_term="Y")
    _accept(client, batch, decided)
    fake = FakeAnki(existing_decks=["D1"])
    _patch_anki(monkeypatch, fake)

    resp = client.post(_finish_url(batch), {"deck_choice": "D1"})
    assert resp.status_code == 200
    assert resp.context["confirm_undecided"] == 1
    assert fake.notes_added() == []  # confirm branch pushes nothing
    batch.refresh_from_db()
    assert batch.deck_name == "D1"  # ... but the deck choice is already stored

    client.post(
        _finish_url(batch), {"deck_choice": "D1", "confirm": "1"}, follow=True
    )
    assert [n["deckName"] for n in fake.notes_added()] == ["D1"]


def test_finish_pushes_only_finished_batch(client, monkeypatch):
    """The batch id travels into the push task: per-batch isolation."""
    batch1 = Batch.objects.create()
    su1 = _url(batch1, n=1)
    card1 = _card(su1, batch1)
    batch2 = Batch.objects.create()
    su2 = _url(batch2, n=2)
    card2 = _card(su2, batch2, front="Other?", source_term="O")
    _accept(client, batch1, card1)
    _accept(client, batch2, card2)
    fake = FakeAnki(existing_decks=["D1", "D2"])
    _patch_anki(monkeypatch, fake)

    client.post(_finish_url(batch1), {"deck_choice": "D1"}, follow=True)
    card1.refresh_from_db()
    card2.refresh_from_db()
    assert card1.synced_at is not None
    assert card2.synced_at is None  # other batch untouched
    assert [n["deckName"] for n in fake.notes_added()] == ["D1"]


# --- extension passthrough ---------------------------------------------------


@pytest.fixture(autouse=True)
def _ext_token_file(tmp_path, settings):
    settings.EXTENSION_TOKEN_FILE = tmp_path / ".extension_token"


@pytest.fixture
def ext_token():
    return mint_token()


@pytest.fixture(autouse=True)
def _ext_id(settings):
    settings.EXTENSION_ID = "abcdefghijklmnop"


def _submit(client, token, body):
    return client.post(
        reverse("extension:submit"),
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )


LONG_TEXT = "word " * 60


def test_extension_submit_with_deck_stores_it(client, ext_token):
    resp = _submit(
        client,
        ext_token,
        {
            "url": "https://example.com/deck-a",
            "title": "T",
            "text": LONG_TEXT,
            "deck_name": "  Extension Deck  ",
        },
    )
    assert resp.status_code == 202
    batch = Batch.objects.get(pk=resp.json()["batch_id"])
    assert batch.deck_name == "Extension Deck"  # stripped verbatim


def test_extension_submit_without_deck_accepted_as_null(client, ext_token):
    resp = _submit(
        client,
        ext_token,
        {"url": "https://example.com/no-deck", "text": LONG_TEXT},
    )
    assert resp.status_code == 202
    batch = Batch.objects.get(pk=resp.json()["batch_id"])
    assert batch.deck_name in (None, "")


def test_extension_submit_null_deck_accepted_as_null(client, ext_token):
    resp = _submit(
        client,
        ext_token,
        {
            "url": "https://example.com/null-deck",
            "text": LONG_TEXT,
            "deck_name": None,
        },
    )
    assert resp.status_code == 202
    batch = Batch.objects.get(pk=resp.json()["batch_id"])
    assert batch.deck_name in (None, "")


@pytest.mark.parametrize("bad", ["", "   "])
def test_extension_submit_empty_deck_rejected(client, ext_token, bad):
    resp = _submit(
        client,
        ext_token,
        {
            "url": "https://example.com/empty-deck",
            "text": LONG_TEXT,
            "deck_name": bad,
        },
    )
    assert resp.status_code == 400
    assert "deck" in resp.json()["error"].lower()


@pytest.mark.parametrize(
    "bad", ["A::::B", "::Leading", "Trailing::", 'quo"te', "a\nb"]
)
def test_extension_submit_illegal_deck_rejected_naming_rule(
    client, ext_token, bad
):
    resp = _submit(
        client,
        ext_token,
        {
            "url": "https://example.com/bad-deck",
            "text": LONG_TEXT,
            "deck_name": bad,
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "deck" in body["error"].lower()
    assert body.get("detail")  # names the violated rule


def test_extension_decks_endpoint_lists_decks(client, ext_token, monkeypatch):
    _patch_anki(monkeypatch, FakeAnki(existing_decks=["B", "A"]))
    resp = client.get(
        reverse("extension:decks"),
        HTTP_AUTHORIZATION=f"Bearer {ext_token}",
    )
    assert resp.status_code == 200
    assert resp.json() == {"decks": ["A", "B"], "unavailable": False}


def test_extension_decks_unreachable_degrades_not_errors(
    client, ext_token, monkeypatch
):
    _patch_anki(monkeypatch, FakeAnki(unreachable=True))
    resp = client.get(
        reverse("extension:decks"),
        HTTP_AUTHORIZATION=f"Bearer {ext_token}",
    )
    assert resp.status_code == 200  # submit must NOT be blocked
    assert resp.json() == {"decks": [], "unavailable": True}


def test_extension_decks_requires_auth(client):
    resp = client.get(reverse("extension:decks"))
    assert resp.status_code == 401
