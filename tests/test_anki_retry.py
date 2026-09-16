"""Periodic Anki-push retry (issue #58).

``submissions.tasks.retry_anki_push_task`` is a ``db_periodic_task`` that
re-pushes accepted+unsynced cards left behind when Anki was unreachable at
Finish time. Tests invoke it directly via ``.call_local()`` with
``FakeAnki`` (pattern in ``tests/test_llm_alerts.py``, fakes in
``tests/test_anki.py``).
"""

import pytest
from django.conf import settings

from submissions import anki, tasks
from submissions.models import Batch, Card, SubmittedURL
from tests.test_anki import FakeAnki

pytestmark = pytest.mark.django_db

_counter = 0


def _mkcard(batch, front="Front?", **kw):
    global _counter
    _counter += 1
    su = SubmittedURL.objects.create(
        url=f"https://example.com/ankiretry-{_counter}",
        status=SubmittedURL.Status.OK,
    )
    return Card.objects.create(
        submitted_url=su,
        batch=batch,
        note_type=Card.NoteType.BASIC,
        front=front,
        back="Back.",
        source_term="T",
        tags={},
        review_status=Card.ReviewStatus.ACCEPTED,
        **kw,
    )


def test_retry_pushes_multi_deck_and_skips_deckless(monkeypatch):
    b1 = Batch.objects.create(deck_name="D1")
    b2 = Batch.objects.create(deck_name="D2")
    b3 = Batch.objects.create()  # no stored deck -> skipped
    c1 = _mkcard(b1, front="d1?")
    c2 = _mkcard(b2, front="d2?")
    c3 = _mkcard(b3, front="nodeck?")
    fake = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    tasks.retry_anki_push_task.call_local()

    for c in (c1, c2):
        c.refresh_from_db()
        assert c.synced_at is not None
    c3.refresh_from_db()
    assert c3.synced_at is None  # deck-less stays unsynced
    decks = {n["deckName"] for n in fake.notes_added()}
    assert decks == {"D1", "D2"}
    assert settings.ANKI_DECK_NAME not in decks
    # Per-batch outcome recorded so the review page flips to done.
    b1.refresh_from_db()
    b2.refresh_from_db()
    assert b1.push_status == Batch.PushStatus.DONE
    assert b1.push_deck_name == "D1"
    assert b1.push_pushed_count == 1
    assert b2.push_status == Batch.PushStatus.DONE
    assert b2.push_deck_name == "D2"
    # Deck-less batch untouched (no outcome row to misreport).
    b3.refresh_from_db()
    assert b3.push_status == ""


def test_retry_unreachable_logs_info_no_raise_nothing_synced(monkeypatch, caplog):
    batch = Batch.objects.create(deck_name="D1")
    batch.record_push_unreachable()  # stale banner from the failed Finish push
    card = _mkcard(batch)
    monkeypatch.setattr(
        anki, "AnkiConnectClient", lambda *a, **k: FakeAnki(unreachable=True)
    )

    with caplog.at_level("INFO", logger="submissions.tasks"):
        tasks.retry_anki_push_task.call_local()  # must not raise; consumer stays up

    card.refresh_from_db()
    assert card.synced_at is None
    assert card.anki_note_id is None
    infos = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(infos) == 1
    assert "Anki unreachable, leaving card(s) unsynced" in infos[0].message
    # Untouched: the stale unreachable banner is left for the next interval.
    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.UNREACHABLE


def test_retry_success_flips_unreachable_banner_to_done(monkeypatch):
    batch = Batch.objects.create(deck_name="D1")
    batch.record_push_unreachable()
    card = _mkcard(batch)
    fake = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    tasks.retry_anki_push_task.call_local()

    card.refresh_from_db()
    assert card.synced_at is not None
    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.DONE
    assert batch.push_pushed_count == 1


def test_retry_rerun_pushes_nothing_no_duplicate(monkeypatch):
    batch = Batch.objects.create(deck_name="D1")
    _mkcard(batch)
    fake1 = FakeAnki(existing_decks=[])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake1)
    tasks.retry_anki_push_task.call_local()
    assert len(fake1.notes_added()) == 1

    # Second run (e.g. next interval, or racing a manual push_to_anki that
    # already synced everything): zero new notes via synced_at idempotency,
    # and no new outcome write (nothing was pushed).
    batch.refresh_from_db()
    finished_at = batch.push_finished_at
    fake2 = FakeAnki(existing_decks=["D1"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake2)
    tasks.retry_anki_push_task.call_local()

    assert fake2.notes_added() == []
    batch.refresh_from_db()
    assert batch.push_finished_at == finished_at
