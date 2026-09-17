"""Tests for the AnkiConnect push (issue #11, per-batch deck #76).

No network: :class:`FakeAnki` stands in for :class:`AnkiConnectClient`,
recording every ``invoke`` call and returning canned results. It is passed
straight into ``push_accepted_cards`` (or monkeypatched onto the module for
the management-command path).

Per-batch deck (#76): cards live on batches with a stored ``deck_name``;
``_card`` creates such a batch by default. Single-batch tests push with
``batch=...`` (one :class:`PushResult`); the unscoped path groups by stored
deck (see ``tests/test_batch_deck.py``).
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
    batch = kw.pop("batch", None)
    if batch is None:
        batch = Batch.objects.create(deck_name="Test Deck")
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
    batch = Batch.objects.create(deck_name="Test Deck")
    _card(batch=batch, review_status=Card.ReviewStatus.ACCEPTED)
    _card(batch=batch, review_status=Card.ReviewStatus.REJECTED)
    _card(batch=batch, review_status=Card.ReviewStatus.UNDECIDED)

    fake = FakeAnki(existing_decks=["Test Deck"])
    result = push_accepted_cards(client=fake, batch=batch)

    assert len(fake.notes_added()) == 1
    assert result.added_count == 1


def test_deck_created_when_missing():
    batch = Batch.objects.create(deck_name="My Deck")
    _card(batch=batch)
    fake = FakeAnki(existing_decks=[])
    result = push_accepted_cards(client=fake, batch=batch)

    assert ("createDeck", {"deck": "My Deck"}) in fake.calls
    assert result.deck_created is True


def test_deck_not_recreated_when_present():
    batch = Batch.objects.create(deck_name="My Deck")
    _card(batch=batch)
    fake = FakeAnki(existing_decks=["My Deck"])
    result = push_accepted_cards(client=fake, batch=batch)

    assert all(a != "createDeck" for a, _ in fake.calls)
    assert result.deck_created is False


def test_note_types_mapped():
    _card(note_type=Card.NoteType.BASIC)
    _card(note_type=Card.NoteType.CLOZE)
    fake = FakeAnki(existing_decks=["Test Deck"])
    push_accepted_cards(client=fake)

    models = {n["modelName"] for n in fake.notes_added()}
    assert models == {"Basic", "Cloze"}
    cloze = next(n for n in fake.notes_added() if n["modelName"] == "Cloze")
    assert "Text" in cloze["fields"]


def test_tags_present_on_notes():
    _card()
    fake = FakeAnki(existing_decks=["Test Deck"])
    push_accepted_cards(client=fake)

    tags = fake.notes_added()[0]["tags"]
    assert any(t.startswith("source:") for t in tags)
    assert "added:2026-09-10" in tags
    assert "topic:Biology" in tags


def test_tags_fall_back_to_card_fields_when_meta_missing():
    c = _card(tags={})
    fake = FakeAnki(existing_decks=["Test Deck"])
    push_accepted_cards(client=fake)
    tags = fake.notes_added()[0]["tags"]
    assert f"source:{c.submitted_url.url}" in tags
    assert "topic:X" in tags  # source_term
    assert any(t.startswith("added:") for t in tags)


def test_sync_fields_recorded_on_success():
    c = _card()
    fake = FakeAnki(existing_decks=["Test Deck"])
    push_accepted_cards(client=fake)

    c.refresh_from_db()
    assert c.anki_note_id is not None
    assert c.synced_at is not None


def test_rerun_pushes_nothing_new():
    batch = Batch.objects.create(deck_name="Test Deck")
    _card(batch=batch)
    fake1 = FakeAnki(existing_decks=["Test Deck"])
    push_accepted_cards(client=fake1, batch=batch)

    fake2 = FakeAnki(existing_decks=["Test Deck"])
    result = push_accepted_cards(client=fake2, batch=batch)

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
    batch = Batch.objects.create(deck_name="Test Deck")
    good = _card(batch=batch, front="good?")
    bad = _card(batch=batch, front="bad?")

    def add_note(note):
        if note["fields"].get("Front") == "bad?":
            return AnkiConnectError("model was not found: NoSuchType")
        return 4242

    fake = FakeAnki(existing_decks=["Test Deck"], add_note=add_note)
    result = push_accepted_cards(client=fake, batch=batch)

    assert result.added_count == 1
    assert result.failed_count == 1
    assert result.failed[0][0] == bad.pk
    assert "NoSuchType" in result.failed[0][1]

    good.refresh_from_db()
    bad.refresh_from_db()
    assert good.synced_at is not None
    assert bad.synced_at is None


def test_duplicate_is_skipped_not_fatal():
    batch = Batch.objects.create(deck_name="Test Deck")
    dup = _card(batch=batch, front="dup?")
    ok = _card(batch=batch, front="ok?")

    def add_note(note):
        if note["fields"].get("Front") == "dup?":
            return AnkiConnectError("cannot create note because it is a duplicate")
        return 777

    fake = FakeAnki(existing_decks=["Test Deck"], add_note=add_note)
    result = push_accepted_cards(client=fake, batch=batch)

    assert result.added_count == 1
    assert result.failed_count == 0
    assert len(result.skipped_duplicate) == 1
    assert result.skipped_duplicate[0][0] == dup.pk

    dup.refresh_from_db()
    ok.refresh_from_db()
    assert dup.synced_at is None
    assert ok.synced_at is not None


def test_result_counts():
    batch = Batch.objects.create(deck_name="Test Deck")
    _card(batch=batch, front="a?")
    _card(batch=batch, front="b?")
    _card(batch=batch, front="dup?")
    _card(batch=batch, front="fail?")
    # one already-synced accepted card
    synced = _card(batch=batch, front="old?")
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

    fake = FakeAnki(existing_decks=["Test Deck"], add_note=add_note)
    result = push_accepted_cards(client=fake, batch=batch)

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


# --- issue #22: media sync ---------------------------------------------

import base64 as _b64

from django.core.files.base import ContentFile


class MediaFakeAnki(FakeAnki):
    """FakeAnki plus AnkiConnect media actions.

    ``media`` maps filename -> bytes already in the fake Anki collection.
    ``store_failures`` maps filename -> error message to raise from
    ``storeMediaFile``.
    """

    def __init__(self, *a, media=None, store_failures=None, **k):
        super().__init__(*a, **k)
        self.media = dict(media or {})
        self.store_failures = dict(store_failures or {})

    def invoke(self, action, **params):
        if action == "retrieveMediaFile":
            self.calls.append((action, params))
            if self.unreachable:
                raise AnkiUnreachableError("unreachable")
            data = self.media.get(params["filename"])
            return _b64.b64encode(data).decode("ascii") if data is not None else None
        if action == "storeMediaFile":
            self.calls.append((action, params))
            if self.unreachable:
                raise AnkiUnreachableError("unreachable")
            if params["filename"] in self.store_failures:
                raise AnkiConnectError(self.store_failures[params["filename"]])
            self.media[params["filename"]] = _b64.b64decode(params["data"])
            return params["filename"]
        if action in ("findNotes", "notesInfo"):
            raise AssertionError(f"unexpected action {action} for a push test")
        return super().invoke(action, **params)

    def store_calls(self):
        return [(a, p) for (a, p) in self.calls if a == "storeMediaFile"]


def _image_card(data: bytes, name="pic.png", **kw):
    card = _card(**kw)
    card.image.save(name, ContentFile(data), save=True)
    card.refresh_from_db()
    return card


def test_media_happy_path_upload_then_note_references_bare_filename():
    data = b"\x89PNG-happy-path-bytes"
    card = _image_card(data)
    fake = MediaFakeAnki(existing_decks=["Test Deck"])

    result = push_accepted_cards(client=fake)

    stores = fake.store_calls()
    assert len(stores) == 1
    filename = stores[0][1]["filename"]
    assert "/" not in filename and "://" not in filename
    assert _b64.b64decode(stores[0][1]["data"]) == data  # base64 data form
    notes = fake.notes_added()
    assert len(notes) == 1
    html_fields = " ".join(notes[0]["fields"].values())
    assert f'<img src="{filename}">' in html_fields
    card.refresh_from_db()
    assert card.synced_at is not None
    assert result.added_count == 1
    # deterministic + bounded + safe charset
    assert anki.media_filename_for_bytes(data, "png") == filename
    assert len(filename) <= 64
    import re

    assert re.fullmatch(r"[a-z0-9._-]+", filename)


def test_media_filename_deterministic_and_collision_safe():
    assert anki.media_filename_for_bytes(b"a", "png") == anki.media_filename_for_bytes(b"a", "png")
    assert anki.media_filename_for_bytes(b"a") != anki.media_filename_for_bytes(b"b")


def test_media_skip_when_identical_content_already_in_anki():
    data = b"shared-bytes"
    filename = anki.media_filename_for_bytes(data, "png")
    card = _image_card(data)
    fake = MediaFakeAnki(existing_decks=["Test Deck"], media={filename: data})

    result = push_accepted_cards(client=fake)

    assert fake.store_calls() == []  # skipped, no redundant upload
    assert f'<img src="{filename}">' in " ".join(fake.notes_added()[0]["fields"].values())
    assert result.added_count == 1


def test_media_same_bytes_across_two_cards_uploaded_once():
    data = b"same-bytes-two-cards"
    _image_card(data, name="one.png")
    _image_card(data, name="two.png")
    fake = MediaFakeAnki(existing_decks=["Test Deck"])

    result = push_accepted_cards(client=fake)

    assert len(fake.store_calls()) == 1
    assert len(fake.notes_added()) == 2
    assert result.added_count == 2


def test_media_rerun_pushes_no_duplicate_media():
    batch = Batch.objects.create(deck_name="Test Deck")
    data = b"rerun-bytes"
    card = _image_card(data, batch=batch)
    fake1 = MediaFakeAnki(existing_decks=["Test Deck"])
    push_accepted_cards(client=fake1, batch=batch)
    assert len(fake1.store_calls()) == 1

    # card stays synced -> re-run sends nothing (no duplicate media/note)
    fake2 = MediaFakeAnki(
        existing_decks=["Test Deck"], media=dict(fake1.media)
    )
    result = push_accepted_cards(client=fake2, batch=batch)
    assert fake2.store_calls() == []
    assert fake2.notes_added() == []
    assert result.skipped_already_synced == 1
    card.refresh_from_db()
    assert card.synced_at is not None


def test_media_store_failure_fails_card_without_broken_reference():
    batch = Batch.objects.create(deck_name="Test Deck")
    good = _image_card(b"good-bytes", name="good.png", batch=batch, front="good?")
    bad = _image_card(b"bad-bytes", name="bad.png", batch=batch, front="bad?")
    bad_filename = anki.media_filename_for_bytes(b"bad-bytes", "png")
    fake = MediaFakeAnki(
        existing_decks=["Test Deck"],
        store_failures={bad_filename: "disk full"},
    )

    result = push_accepted_cards(client=fake, batch=batch)

    assert result.failed_count == 1
    assert result.failed[0][0] == bad.pk
    assert "disk full" in result.failed[0][1]
    fronts = [n["fields"].get("Front") for n in fake.notes_added()]
    assert "bad?" not in fronts  # no note with a broken reference
    assert "good?" in fronts  # later/earlier card unaffected
    bad.refresh_from_db()
    good.refresh_from_db()
    assert bad.synced_at is None  # retryable: still unsynced
    assert bad.anki_note_id is None
    assert good.synced_at is not None


def test_no_image_card_pushes_without_media_calls():
    _card()
    fake = MediaFakeAnki(existing_decks=["Test Deck"])

    result = push_accepted_cards(client=fake)

    assert fake.store_calls() == []
    assert all(a != "retrieveMediaFile" for a, _ in fake.calls)
    assert result.added_count == 1
