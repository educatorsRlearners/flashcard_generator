"""Push-outcome persistence + display on the review page (issue #140).

``card_review_finish`` fires the Anki push in the background
(fire-and-forget, issue #57) so the outcome can't ride Django's
``messages`` framework - it has to be durable state the ``card_review`` GET
view reads back on any later page load. These tests cover the acceptance
criteria: already-synced-only, newly-pushed, unreachable, partial-failure,
visual/textual distinctness from the review-tally message, the pending
marker, persistence across reloads, and the "Finish clicked 3x" race.
"""

from django.utils import timezone
from django.urls import reverse
import pytest

from submissions import anki
from submissions.anki import AnkiConnectError
from submissions.models import Batch, Card
from tests.test_anki import FakeAnki
from tests.test_card_review import _card, _url

pytestmark = pytest.mark.django_db


def _finish(client, batch, deck="Flashcard Generator", **extra):
    data = {"deck_choice": deck}
    data.update(extra)
    return client.post(
        reverse("submissions:card_review_finish", args=[batch.pk]), data, follow=True
    )


def _review_page(client, batch):
    return client.get(reverse("submissions:card_review", args=[batch.pk]))


# --- already-synced-only ------------------------------------------------


def test_already_synced_only_outcome(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    card = _card(su, batch, review_status=Card.ReviewStatus.ACCEPTED,
                 synced_at=timezone.now(), anki_note_id=42)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _finish(client, batch)

    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.DONE
    assert batch.push_pushed_count == 0
    assert batch.push_skipped_count == 1
    assert batch.push_failed_count == 0
    assert fake.notes_added() == []

    page = _review_page(client, batch)
    content = page.content.decode()
    assert "1 card(s) already synced, nothing new to push." in content
    assert "id=\"push-outcome\"" in content
    card.refresh_from_db()
    assert card.synced_at is not None  # untouched


# --- newly pushed ---------------------------------------------------------


def test_newly_pushed_count_outcome(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    c2 = _card(su, batch, front="Q2", source_term="Y")
    _decide_accept(client, batch, c1)
    _decide_accept(client, batch, c2)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _finish(client, batch)

    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.DONE
    assert batch.push_pushed_count == 2
    assert batch.push_failed_count == 0

    page = _review_page(client, batch)
    content = page.content.decode()
    assert "2 card(s) pushed to deck &#x27;Flashcard Generator&#x27;." in content \
        or "2 card(s) pushed to deck 'Flashcard Generator'." in content


# --- unreachable ------------------------------------------------------


def test_unreachable_outcome_shown(client, monkeypatch):
    """Before this task ``AnkiUnreachableError`` was silently swallowed
    (see ``push_accepted_cards_task``'s docstring) - this asserts the
    review page now says so."""
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide_accept(client, batch, c1)

    fake = FakeAnki(unreachable=True)
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _finish(client, batch, deck="Typed Deck")

    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.UNREACHABLE

    page = _review_page(client, batch)
    content = page.content.decode()
    assert "unreachable" in content.lower()
    assert "push-outcome--unreachable" in content


# --- partial failure ----------------------------------------------------


def test_partial_failure_outcome(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    c2 = _card(su, batch, front="Q2", source_term="Y")
    _decide_accept(client, batch, c1)
    _decide_accept(client, batch, c2)

    calls = {"n": 0}

    def add_note(note):
        calls["n"] += 1
        if calls["n"] == 2:
            raise AnkiConnectError("cannot create note: bad note type")
        return 5000 + calls["n"]

    fake = FakeAnki(existing_decks=["Flashcard Generator"], add_note=add_note)
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _finish(client, batch)

    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.DONE
    assert batch.push_pushed_count == 1
    assert batch.push_failed_count == 1

    page = _review_page(client, batch)
    content = page.content.decode()
    assert "1" in content and "failed" in content.lower()


# --- distinct from the tally message ------------------------------------


def test_push_outcome_distinct_from_tally_message(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide_accept(client, batch, c1)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    resp = _finish(client, batch)
    content = resp.content.decode()

    # The tally ("N cards - N accepted, ...") and the push-outcome banner
    # are separate elements with separate ids/classes, so neither is
    # confusable with the other; the tally never mentions "push"/"Anki".
    assert 'id="review-tally"' in content
    assert 'id="push-outcome"' in content
    assert 'class="push-outcome push-outcome--done"' in content
    tally_snippet = content.split('id="review-tally"')[1].split("</p>")[0]
    assert "anki" not in tally_snippet.lower()
    assert "push" not in tally_snippet.lower()


# --- pending marker -------------------------------------------------------


def test_pending_marker_visible_before_task_completes(client, monkeypatch):
    """Huey immediate mode runs the task inline, so there is no real
    window where a request is mid-flight - but the pending marker is set
    synchronously by the view *before* the task runs (issue #140's
    constraint), and the task's own AnkiConnect calls happen strictly
    after that write. Observing the stored state from inside the fake
    client's first call proves the ordering without needing real async."""
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide_accept(client, batch, c1)

    seen = {}

    class ObservingFakeAnki(FakeAnki):
        def invoke(self, action, **params):
            if action == "deckNames" and "status" not in seen:
                seen["status"] = Batch.objects.get(pk=batch.pk).push_status
            return super().invoke(action, **params)

    fake = ObservingFakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    _finish(client, batch)

    assert seen["status"] == Batch.PushStatus.PENDING
    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.DONE  # settled by the time we look now


def test_pending_state_shown_directly(client):
    """A batch whose push is mid-flight (set via ``mark_push_pending``)
    shows a distinct pending banner, not a stale outcome or nothing."""
    batch = Batch.objects.create()
    _url(batch)
    batch.push_status = Batch.PushStatus.DONE
    batch.push_pushed_count = 3
    batch.save(update_fields=["push_status", "push_pushed_count"])

    batch.mark_push_pending()

    page = _review_page(client, batch)
    content = page.content.decode()
    assert "push-outcome--pending" in content
    assert "3 card(s) pushed" not in content  # not the stale earlier outcome
    assert "nothing new to push" not in content  # not a false "nothing to push"


# --- persists across reloads ---------------------------------------------


def test_outcome_persists_across_multiple_reloads(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide_accept(client, batch, c1)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)
    _finish(client, batch)

    first = _review_page(client, batch).content.decode()
    second = _review_page(client, batch).content.decode()
    assert "1 card(s) pushed to deck" in first
    assert "1 card(s) pushed to deck" in second


# --- triple-click race ----------------------------------------------------


def test_finish_clicked_three_times_ends_consistent(client, monkeypatch):
    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    c2 = _card(su, batch, front="Q2", source_term="Y")
    _decide_accept(client, batch, c1)
    _decide_accept(client, batch, c2)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    for _ in range(3):
        resp = _finish(client, batch)
        assert resp.status_code == 200

    batch.refresh_from_db()
    # No crash, and a single consistent terminal state: both cards ended
    # up synced by the first successful run, so the last run (whichever
    # completed last) reports them as already-synced, not a mix of counts
    # from overlapping runs.
    assert batch.push_status == Batch.PushStatus.DONE
    assert batch.push_pushed_count == 0
    assert batch.push_skipped_count == 2
    assert batch.push_failed_count == 0
    assert len(fake.notes_added()) == 2  # each card pushed exactly once total


def _decide_accept(client, batch, card):
    client.post(
        reverse("submissions:card_review_decision", args=[batch.pk, card.pk]),
        {"decision": "accepted"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


# --- unexpected task-level failure (issue #147) ----------------------------


def test_unexpected_error_records_failed_status(client, monkeypatch):
    """A task-level ``RuntimeError`` mid-push leaves a terminal ``failed``
    state instead of stuck-pending (issue #147)."""
    import logging

    from submissions import tasks as push_tasks

    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide_accept(client, batch, c1)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    def _boom(b):
        raise RuntimeError("boom")

    monkeypatch.setattr(push_tasks, "push_batch_accepted_cards", _boom)

    records = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    task_logger = logging.getLogger(push_tasks.__name__)
    task_logger.addHandler(handler)
    try:
        with monkeypatch.context() as m:
            # ensure the task runs with the logger at a level caplog-style
            # handlers can see; the handler above captures everything.
            _finish(client, batch)
    finally:
        task_logger.removeHandler(handler)

    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.FAILED
    assert batch.push_finished_at is not None
    # Counts are zeroed: the crash may have happened mid-push, so any
    # partial totals are unknown and must not be reported.
    assert batch.push_pushed_count == 0
    assert batch.push_skipped_count == 0
    assert batch.push_failed_count == 0
    # logger.exception kept with the existing message.
    assert any(
        "unexpected error pushing to Anki" in r.getMessage() for r in records
    )


def test_failed_banner_shown_and_persists(client, monkeypatch):
    from submissions import tasks as push_tasks

    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    _decide_accept(client, batch, c1)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)
    monkeypatch.setattr(
        push_tasks,
        "push_batch_accepted_cards",
        lambda b: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    _finish(client, batch)
    batch.refresh_from_db()
    assert batch.push_status == "failed"

    first = _review_page(client, batch).content.decode()
    assert 'data-push-status="failed"' in first
    assert "push-outcome--failed" in first
    assert "failed" in first.lower()
    assert "in progress" not in first.lower()

    second = _review_page(client, batch).content.decode()
    third = _review_page(client, batch).content.decode()
    for content in (second, third):
        assert 'data-push-status="failed"' in content
        assert "failed" in content.lower()
        assert "in progress" not in content.lower()


def test_failed_mid_push_keeps_synced_cards_but_zeroes_counts(
    client, monkeypatch
):
    """Cards synced before the mid-push crash stay synced; the terminal
    ``failed`` state does not claim counts it did not measure."""
    from submissions import tasks as push_tasks

    batch = Batch.objects.create()
    su = _url(batch)
    c1 = _card(su, batch)
    c2 = _card(su, batch, front="Q2", source_term="Y")
    _decide_accept(client, batch, c1)
    _decide_accept(client, batch, c2)

    fake = FakeAnki(existing_decks=["Flashcard Generator"])
    monkeypatch.setattr(anki, "AnkiConnectClient", lambda *a, **k: fake)

    def _sync_one_then_boom(b):
        c1.anki_note_id = 4242
        c1.synced_at = timezone.now()
        c1.save(update_fields=["anki_note_id", "synced_at"])
        raise RuntimeError("boom")

    monkeypatch.setattr(
        push_tasks, "push_batch_accepted_cards", _sync_one_then_boom
    )

    _finish(client, batch)

    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.FAILED
    assert batch.push_finished_at is not None
    assert batch.push_pushed_count == 0
    assert batch.push_skipped_count == 0
    assert batch.push_failed_count == 0
    c1.refresh_from_db()
    assert c1.synced_at is not None  # partial progress kept
    assert c1.anki_note_id == 4242


def test_deleted_and_unusable_batch_ids_are_silent_noop(monkeypatch):
    """Deleted / invalid batch ids never crash and never write status."""
    from submissions import tasks as push_tasks

    batch = Batch.objects.create(deck_name="Flashcard Generator")
    pk = batch.pk
    batch.delete()

    push_tasks.push_accepted_cards_task(pk)
    push_tasks.push_accepted_cards_task("abc")
    assert Batch.objects.filter(pk=pk).count() == 0

    other = Batch.objects.create(deck_name="Flashcard Generator")
    other.mark_push_pending()
    push_tasks.push_accepted_cards_task("abc")
    other.refresh_from_db()
    assert other.push_status == Batch.PushStatus.PENDING
    assert other.push_finished_at is None


def test_failed_banner_uses_error_pattern_css():
    """UX fix (#147): .push-outcome--failed must follow the existing
    --unreachable error pattern (red/error + failure icon), not fall back
    to the base info-blue style with the default retry icon."""
    from pathlib import Path

    css = (
        Path(__file__).resolve().parent.parent
        / "submissions" / "static" / "submissions" / "app.css"
    ).read_text()
    assert ".push-outcome--failed" in css
    failed_block = css.split(".push-outcome--failed")[1].split("}")[0]
    assert "var(--error-bg)" in failed_block
    assert "var(--error-fg)" in failed_block
    assert "var(--error-bd)" in failed_block
    assert "21bb" not in failed_block.lower()  # default info/retry icon
