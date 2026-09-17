"""Per-batch Anki deck (issue #76): Engineer's A stream.

Covers the backend half: ``Batch.deck_name`` storage, the shared
``submissions.anki`` deck validator/normalizer, per-deck push grouping
(never falling back to ``ANKI_DECK_NAME``), and the background task's
optional batch id / the ``push_to_anki`` CLI grouping.
"""

import pytest
from django.contrib import admin
from django.core.management import call_command

from submissions import anki
from submissions.anki import (
    DeckNameError,
    normalize_deck_name,
    push_accepted_cards,
    resolve_deck_choice,
    validate_deck_name,
)
from submissions.models import Batch, Card, SubmittedURL

pytestmark = pytest.mark.django_db


# --- fakes ------------------------------------------------------------


class FakeAnki:
    """Drop-in for AnkiConnectClient: deckNames/createDeck/addNote only."""

    def __init__(self, existing_decks=(), unreachable=False):
        self.url = "http://fake:8765"
        self.existing_decks = list(existing_decks)
        self.unreachable = unreachable
        self.calls = []
        self._next_id = 2000

    def invoke(self, action, **params):
        self.calls.append((action, params))
        if self.unreachable:
            raise anki.AnkiUnreachableError("unreachable")
        if action == "deckNames":
            return list(self.existing_decks)
        if action == "createDeck":
            self.existing_decks.append(params["deck"])
            return 1
        if action == "addNote":
            self._next_id += 1
            return self._next_id
        raise AssertionError(f"unexpected action {action}")

    def notes_added(self):
        return [p["note"] for (a, p) in self.calls if a == "addNote"]


_counter = 0


def _mkcard(batch, front="Front?", back="Back.", term="T", **kw):
    global _counter
    _counter += 1
    su = SubmittedURL.objects.create(
        url=f"https://example.com/batchdeck-{_counter}",
        status=SubmittedURL.Status.OK,
    )
    return Card.objects.create(
        submitted_url=su,
        batch=batch,
        note_type=Card.NoteType.BASIC,
        front=front,
        back=back,
        source_term=term,
        tags={},
        review_status=Card.ReviewStatus.ACCEPTED,
        **kw,
    )


# --- model + admin ----------------------------------------------------


def test_deck_name_defaults_to_none_old_rows_blank():
    batch = Batch.objects.create()
    assert batch.deck_name is None
    batch.refresh_from_db()
    assert batch.deck_name is None


def test_deck_name_max_length_allows_real_names():
    max_length = Batch._meta.get_field("deck_name").max_length
    assert max_length >= 200


def test_admin_shows_deck_name():
    assert "deck_name" in admin.site._registry[Batch].list_display


# --- validator / normalizer (shared with Engineer B via submissions.anki)


def test_validate_strips_but_stores_verbatim():
    assert validate_deck_name("  My Deck  ") == "My Deck"
    assert validate_deck_name("Parent::Child") == "Parent::Child"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        None,
        'say "hi"',
        "line1\nline2",
        "line1\rline2",
        "::Leading",
        "Trailing::",
        "a::::b",
        "a::  ::b",
    ],
)
def test_validate_rejects(bad):
    with pytest.raises(DeckNameError):
        validate_deck_name(bad)


def test_validate_rejects_too_long():
    max_length = Batch._meta.get_field("deck_name").max_length
    with pytest.raises(DeckNameError):
        validate_deck_name("x" * (max_length + 1))


def test_deck_name_error_is_value_error():
    assert issubclass(DeckNameError, ValueError)


def test_normalize_deck_name():
    assert normalize_deck_name(None) == ""
    assert normalize_deck_name("") == ""
    assert normalize_deck_name("  Deck  ") == "Deck"


def test_resolve_deck_choice_typed_wins_over_dropdown():
    assert resolve_deck_choice(new=" Typed ", existing="Dropdown") == "Typed"
    assert resolve_deck_choice(new="", existing=" Dropdown ") == "Dropdown"
    assert resolve_deck_choice() == ""




# --- per-deck push ----------------------------------------------------


def test_push_batch_uses_stored_deck_and_creates_it():
    batch = Batch.objects.create(deck_name="Biology")
    card = _mkcard(batch)
    fake = FakeAnki(existing_decks=[])

    result = push_accepted_cards(client=fake, batch=batch)

    assert result.deck_name == "Biology"
    assert ("createDeck", {"deck": "Biology"}) in fake.calls
    assert result.deck_created is True
    assert fake.notes_added()[0]["deckName"] == "Biology"
    card.refresh_from_db()
    assert card.synced_at is not None


def test_two_batches_two_decks_in_one_run():
    b1 = Batch.objects.create(deck_name="Deck A")
    b2 = Batch.objects.create(deck_name="Deck B")
    _mkcard(b1)
    _mkcard(b2)
    fake = FakeAnki(existing_decks=[])

    grouped = push_accepted_cards(client=fake)

    assert {r.deck_name for r in grouped.results} == {"Deck A", "Deck B"}
    assert grouped.added_count == 2
    assert {n["deckName"] for n in fake.notes_added()} == {"Deck A", "Deck B"}
    text = "\n".join(grouped.summary_lines())
    assert 'Anki deck "Deck A"' in text
    assert 'Anki deck "Deck B"' in text


def test_batches_without_deck_never_pushed_no_fallback(settings):
    settings.ANKI_DECK_NAME = "Fallback Deck Must Never Be Used"
    bare = Batch.objects.create()  # NULL deck
    blank = Batch.objects.create(deck_name="")  # empty deck
    decked = Batch.objects.create(deck_name="Real Deck")
    stray = _mkcard(bare)
    _mkcard(blank)
    _mkcard(decked)
    fake = FakeAnki(existing_decks=["Fallback Deck Must Never Be Used", "Real Deck"])

    grouped = push_accepted_cards(client=fake)

    assert grouped.skipped_no_deck == 2
    for note in fake.notes_added():
        assert note["deckName"] == "Real Deck"
    assert all("Fallback" not in str(c) for c in fake.calls)
    stray.refresh_from_db()
    assert stray.synced_at is None  # stays unsynced, retryable later


def test_push_batch_without_deck_returns_empty_result():
    batch = Batch.objects.create()
    card = _mkcard(batch)
    fake = FakeAnki(existing_decks=[])

    result = push_accepted_cards(client=fake, batch=batch)

    assert result.added_count == 0
    assert fake.notes_added() == []
    card.refresh_from_db()
    assert card.synced_at is None


# --- background task --------------------------------------------------


def _run_task(*args, **kwargs):
    from submissions.tasks import push_accepted_cards_task

    return push_accepted_cards_task(*args, **kwargs)


def test_task_with_id_pushes_only_that_batch(monkeypatch):
    b1 = Batch.objects.create(deck_name="Deck One")
    b2 = Batch.objects.create(deck_name="Deck Two")
    c1 = _mkcard(b1)
    c2 = _mkcard(b2)
    fake = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _run_task(b1.pk)

    c1.refresh_from_db()
    c2.refresh_from_db()
    assert c1.synced_at is not None
    assert c2.synced_at is None
    assert {n["deckName"] for n in fake.notes_added()} == {"Deck One"}


def test_task_none_scans_all_decked_batches(monkeypatch):
    b1 = Batch.objects.create(deck_name="Deck One")
    b2 = Batch.objects.create(deck_name="Deck Two")
    c1 = _mkcard(b1)
    c2 = _mkcard(b2)
    fake = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _run_task()  # old no-arg queued entry: safe, pushes every stored deck

    c1.refresh_from_db()
    c2.refresh_from_db()
    assert c1.synced_at is not None
    assert c2.synced_at is not None


def test_task_deleted_and_garbage_ids_are_noops(monkeypatch):
    fake = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _run_task(999999)  # deleted / never-existed batch
    _run_task("not-an-id")  # unusable id

    assert fake.notes_added() == []


def test_task_unreachable_leaves_unsynced_without_raising(monkeypatch):
    batch = Batch.objects.create(deck_name="Deck One")
    card = _mkcard(batch)
    monkeypatch.setattr(
        anki, "AnkiConnectClient", lambda *a, **k: FakeAnki(unreachable=True)
    )

    _run_task(batch.pk)  # must not propagate

    card.refresh_from_db()
    assert card.synced_at is None


# --- CLI --------------------------------------------------------------


def test_cli_groups_by_stored_deck_with_skipped_line(monkeypatch, capsys):
    b1 = Batch.objects.create(deck_name="Deck A")
    Batch.objects.create()  # no deck -> skipped
    _mkcard(b1)
    _mkcard(Batch.objects.create(deck_name="Deck B"))
    fake = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    call_command("push_to_anki")

    out = capsys.readouterr().out
    assert 'Anki deck "Deck A"' in out
    assert 'Anki deck "Deck B"' in out
    assert "no deck chosen" in out


def test_cli_skipped_zero_line_when_all_decked(monkeypatch, capsys):
    batch = Batch.objects.create(deck_name="Deck A")
    _mkcard(batch)
    fake = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    call_command("push_to_anki")

    out = capsys.readouterr().out
    assert "added 1" in out
    assert "skipped 0 card(s) with no deck chosen" in out
