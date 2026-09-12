"""Background tasks for the review/Anki-push flow.

Finishing review of a batch (``card_review_finish`` in
``submissions/views.py``) enqueues one :func:`push_accepted_cards_task` per
finish, which pushes every accepted-and-unsynced card to Anki in the
background so ``manage.py push_to_anki`` is no longer a required manual step
for the common case (issue #57). This task is shared by both front ends
(the browser extension and, previously, the pasted-URL form).
"""

from __future__ import annotations

import logging

from huey.contrib.djhuey import db_task

from .anki import AnkiUnreachableError, push_accepted_cards

logger = logging.getLogger(__name__)


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

    Failure isolation: nothing here ever propagates. If Anki/AnkiConnect is
    unreachable the cards simply stay unsynced for a later manual
    ``push_to_anki`` run (or a future retry mechanism - see #58). A per-card
    AnkiConnect error (bad note type, duplicate, ...) is already isolated to
    that one card inside ``push_accepted_cards`` itself and does not reach
    here at all.
    """
    try:
        push_accepted_cards()
    except AnkiUnreachableError as exc:
        logger.info(
            "push_accepted_cards_task: Anki unreachable, leaving card(s) unsynced (%s)",
            exc,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("push_accepted_cards_task: unexpected error pushing to Anki")
