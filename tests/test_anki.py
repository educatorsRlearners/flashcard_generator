"""Tests for the AnkiConnect push (issue #11).

No network: :class:`FakeAnki` stands in for :class:`AnkiConnectClient`,
recording every ``invoke`` call and returning canned results. It is passed
straight into ``push_accepted_cards`` (or monkeypatched onto the module for
the management-command path).
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from submissions import anki
from submissions.anki import (
    AnkiConnectError,
    AnkiUnreachableError,
    push_accepted_cards,
)
from submissions.models import Batch, Card, SubmittedURL

pytestmark = pytest.mark.django_db


# --- fakes / fixtures ------------------------------------------------


class FakeAnki:
    """Drop-in for AnkiConnectClient.

    ``existing_decks`` seeds ``deckNames``. ``add_note`` is either a callable
    ``(note) -> note_id | Exception`` or None (auto-incrementing ids).
    ``unreachable`` makes every call raise AnkiUnreachableError.
    """

    def __init__(self, existing_decks=(), add_note=None, unreachable=False):
        self.url = "http://fake:8765"
        self.existing_decks = list(existing_decks)
        self.add_note = add_note
        self.unreachable = unreachable
        self.calls = []
        self._next_id = 1000

    def invoke(self, action, **params):
        self.calls.append((action, params))
        if self.unreachable:
            raise AnkiUnreachableError(
                f"Could not reach AnkiConnect at {self.url} (Connection refused)."
            )
        if action == "deckNames":
            return list(self.existing_decks)
        if action == "createDeck":
            self.existing_decks.append(params["deck"])
            return 1
        if action == "addNote":
            note = params["note"]
            if callable(self.add_note):
                outcome = self.add_note(note)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            self._next_id += 1
            return self._next_id
        raise AssertionError(f"unexpected action {action}")

    def notes_added(self):
        return [p["note"] for (a, p) in self.calls if a == "addNote"]


def _card(review_status=Card.ReviewStatus.ACCEPTED, note_type=Card.NoteType.BASIC, **kw):
    batch = kw.pop("batch", None) or Batch.objects.create()
    su = kw.pop("submitted_url", None) or SubmittedURL.objects.create(
        url=f"https://example.com/{Card.objects.count()}-{id(kw)}",
        status=SubmittedURL.Status.OK,
    )
    opts = dict(
        submitted_url=su,
        batch=batch,
        note_type=note_type,
        front="What is X?" if note_type == Card.NoteType.BASIC else "The {{c1::X}}.",
        back="X is a thing.",
        source_term="X",
        tags={
            "source_url": su.url,
            "date_added": "2026-09-10",
            "topic": "Biology",
        },
        review_status=review_status,
    )
    opts.update(kw)
    return Card.objects.create(**opts)


# --- tests ----------------------------------------------------------


def test_only_accepted_cards_are_sent():
    _card(review_status=Card.ReviewStatus.ACCEPTED)
    _card(review_status=Card.ReviewStatus.REJECTED)
    _card(review_status=Card.ReviewStatus.UNDECIDED)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    result = push_accepted_cards(client=fake)

    assert len(fake.notes_added()) == 1
    assert result.added_count == 1


def test_deck_created_when_missing():
    _card()
    fake = FakeAnki(existing_decks=[])
    result = push_accepted_cards(client=fake)

    assert ("createDeck", {"deck": "Flashcard Generator"}) in fake.calls
    assert result.deck_created is True


def test_deck_not_recreated_when_present():
    _card()
    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    result = push_accepted_cards(client=fake)

    assert all(a != "createDeck" for a, _ in fake.calls)
    assert result.deck_created is False


def test_note_types_mapped():
    _card(note_type=Card.NoteType.BASIC)
    _card(note_type=Card.NoteType.CLOZE)
    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    push_accepted_cards(client=fake)

    models = {n["modelName"] for n in fake.notes_added()}
    assert models == {"Basic", "Cloze"}
    cloze = next(n for n in fake.notes_added() if n["modelName"] == "Cloze")
    assert "Text" in cloze["fields"]


def test_tags_present_on_notes():
    _card()
    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    push_accepted_cards(client=fake)

    tags = fake.notes_added()[0]["tags"]
    assert any(t.startswith("source:") for t in tags)
    assert "added:2026-09-10" in tags
    assert "topic:Biology" in tags


def test_tags_fall_back_to_card_fields_when_meta_missing():
    c = _card(tags={})
    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    push_accepted_cards(client=fake)
    tags = fake.notes_added()[0]["tags"]
    assert f"source:{c.submitted_url.url}" in tags
    assert "topic:X" in tags  # source_term
    assert any(t.startswith("added:") for t in tags)


def test_sync_fields_recorded_on_success():
    c = _card()
    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    push_accepted_cards(client=fake)

    c.refresh_from_db()
    assert c.anki_note_id is not None
    assert c.synced_at is not None


def test_rerun_pushes_nothing_new():
    _card()
    fake1 = FakeAnki(existing_decks=["Flashcard Generator"])
    push_accepted_cards(client=fake1)

    fake2 = FakeAnki(existing_decks=["Flashcard Generator"])
    result = push_accepted_cards(client=fake2)

    assert fake2.notes_added() == []
    assert result.added_count == 0
    assert result.skipped_already_synced == 1


def test_unreachable_anki_clean_error_nothing_synced():
    c = _card()
    fake = FakeAnki(unreachable=True)

    with pytest.raises(AnkiUnreachableError) as exc:
        push_accepted_cards(client=fake)

    assert fake.url in str(exc.value)
    c.refresh_from_db()
    assert c.synced_at is None
    assert c.anki_note_id is None


def test_per_note_error_is_isolated():
    good = _card(front="good?")
    bad = _card(front="bad?")

    def add_note(note):
        if note["fields"].get("Front") == "bad?":
            return AnkiConnectError("model was not found: NoSuchType")
        return 4242

    fake = FakeAnki(existing_decks=["Flashcard Generator"], add_note=add_note)
    result = push_accepted_cards(client=fake)

    assert result.added_count == 1
    assert result.failed_count == 1
    assert result.failed[0][0] == bad.pk
    assert "NoSuchType" in result.failed[0][1]

    good.refresh_from_db()
    bad.refresh_from_db()
    assert good.synced_at is not None
    assert bad.synced_at is None


def test_duplicate_is_skipped_not_fatal():
    dup = _card(front="dup?")
    ok = _card(front="ok?")

    def add_note(note):
        if note["fields"].get("Front") == "dup?":
            return AnkiConnectError("cannot create note because it is a duplicate")
        return 777

    fake = FakeAnki(existing_decks=["Flashcard Generator"], add_note=add_note)
    result = push_accepted_cards(client=fake)

    assert result.added_count == 1
    assert result.failed_count == 0
    assert len(result.skipped_duplicate) == 1
    assert result.skipped_duplicate[0][0] == dup.pk

    dup.refresh_from_db()
    ok.refresh_from_db()
    assert dup.synced_at is None
    assert ok.synced_at is not None


def test_result_counts():
    _card(front="a?")
    _card(front="b?")
    _card(front="dup?")
    _card(front="fail?")
    # one already-synced accepted card
    synced = _card(front="old?")
    synced.synced_at = synced.created_at
    synced.anki_note_id = 1
    synced.save()

    def add_note(note):
        front = note["fields"].get("Front")
        if front == "dup?":
            return AnkiConnectError("it is a duplicate")
        if front == "fail?":
            return AnkiConnectError("some other error")
        return 9000

    fake = FakeAnki(existing_decks=["Flashcard Generator"], add_note=add_note)
    result = push_accepted_cards(client=fake)

    assert result.added_count == 2
    assert result.skipped_already_synced == 1
    assert len(result.skipped_duplicate) == 1
    assert result.skipped_count == 2
    assert result.failed_count == 1
    text = "\n".join(result.summary_lines())
    assert "added 2" in text
    assert "some other error" in text


def test_management_command_reports_and_handles_unreachable(monkeypatch, capsys):
    _card()
    monkeypatch.setattr(
        anki, "AnkiConnectClient", lambda *a, **k: FakeAnki(unreachable=True)
    )
    with pytest.raises(CommandError) as exc:
        call_command("push_to_anki")
    assert "AnkiConnect" in str(exc.value)


def test_management_command_success(monkeypatch, capsys):
    _card()
    fake = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)
    call_command("push_to_anki")
    out = capsys.readouterr().out
    assert "added 1" in out
