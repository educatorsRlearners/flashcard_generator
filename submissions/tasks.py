"""Background tasks for batch processing (issue #8).

Submitting a batch enqueues one :func:`process_url` task per submitted URL.
Each task runs the existing synchronous extraction path
(:func:`submissions.extraction.extract`), which fetches + extracts the page
and persists ``status`` / ``failure_kind`` / ``failure_reason`` on the row
(the failure taxonomy from #4 - tasks never re-classify).

Tasks are isolated: an unexpected exception in one URL's task marks only
that URL ``failed`` and never blocks the other URLs in the batch. The queue
is a local SQLite file, so pending tasks survive a consumer crash and are
picked up when it restarts.

Note: the in-process robots.txt cache and per-domain rate-limit clock in
``submissions.extraction`` are per-worker-process state. Sharing them across
consumer workers is out of scope here and tracked in #17.

Finishing review of a batch (``card_review_finish`` in
``submissions/views.py``) similarly enqueues one
:func:`push_accepted_cards_task` per finish, which pushes every
accepted-and-unsynced card to Anki in the background so
``manage.py push_to_anki`` is no longer a required manual step for the
common case (issue #57).
"""

from __future__ import annotations

import logging

from huey.contrib.djhuey import db_task

from . import extraction
from .anki import AnkiUnreachableError, push_accepted_cards
from .models import SubmittedURL

logger = logging.getLogger(__name__)


def run_extraction(submitted_url: SubmittedURL):
    """Seam around the synchronous extraction path (patched in tests)."""
    return extraction.extract(submitted_url)


@db_task()
def process_url(submitted_url_id: int) -> None:
    """Fetch + extract a single :class:`SubmittedURL` in the background."""
    try:
        submitted_url = SubmittedURL.objects.get(pk=submitted_url_id)
    except SubmittedURL.DoesNotExist:
        return

    try:
        run_extraction(submitted_url)
    except Exception as exc:  # isolate the failure to this one URL
        SubmittedURL.objects.filter(pk=submitted_url_id).update(
            status=SubmittedURL.Status.FAILED,
            failure_kind=SubmittedURL.FailureKind.UNKNOWN,
            failure_reason=f"unexpected error: {exc}",
            extracted_text="",
            extracted_title="",
            extracted_at=None,
        )


@db_task()
def push_accepted_cards_task() -> None:
    """Push accepted+unsynced cards to Anki in the background (issue #57).

    Fired once per ``card_review_finish`` POST that actually finishes a
    batch. Takes no argument that snapshots card state: it just calls
    :func:`push_accepted_cards`, which re-scans ``accepted_unsynced_cards()``
    by query across *all* batches, so it always operates on current DB
    state and incidentally also retries any other batch's previously
    unsynced cards. Finishing the same batch twice (or two batches close
    together) enqueues two task runs, but ``synced_at`` already makes the
    second (or any redundant) run a cheap no-op - never a duplicate note.

    Failure isolation matches :func:`process_url`: nothing here ever
    propagates. If Anki/AnkiConnect is unreachable the cards simply stay
    unsynced for a later manual ``push_to_anki`` run (or a future retry
    mechanism - see #58). A per-card AnkiConnect error (bad note type,
    duplicate, ...) is already isolated to that one card inside
    ``push_accepted_cards`` itself and does not reach here at all.
    """
    try:
        push_accepted_cards()
    except AnkiUnreachableError as exc:
        logger.info(
            "push_accepted_cards_task: Anki unreachable, leaving card(s) unsynced (%s)",
            exc,
        )
    except Exception:  # pragma: no cover - defensive, matches process_url
        logger.exception("push_accepted_cards_task: unexpected error pushing to Anki")


def enqueue_batch(batch) -> int:
    """Enqueue one :func:`process_url` task per distinct URL in *batch*.

    Returns the number of tasks enqueued.
    """
    url_ids = (
        batch.requests.order_by("submitted_url_id")
        .values_list("submitted_url_id", flat=True)
        .distinct()
    )
    count = 0
    for url_id in url_ids:
        process_url(url_id)
        count += 1
    return count
