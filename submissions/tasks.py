"""Background tasks for the review/Anki-push flow.

Finishing review of a batch (``card_review_finish`` in
``submissions/views.py``) enqueues one :func:`push_accepted_cards_task` per
finish with the finished batch's id, which pushes only that batch's
accepted-and-unsynced cards to its stored ``deck_name`` in the background
so ``manage.py push_to_anki`` is no longer a required manual step for the
common case (issue #57; per-batch deck in issue #76).
"""

from __future__ import annotations

import logging

from huey.contrib.djhuey import db_task

from .anki import AnkiUnreachableError, push_accepted_cards

logger = logging.getLogger(__name__)


@db_task()
def push_accepted_cards_task(batch_id=None, *args, **kwargs) -> None:
    """Push one finished batch's accepted+unsynced cards to Anki (issue #76).

    Takes the finished batch id and pushes only that batch's cards to its
    stored ``deck_name``. ``batch_id`` is optional so old no-arg queued
    invocations (enqueued before this signature existed) still run safely:
    with no id the task falls back to pushing every deck-assigned batch.
    A batch with no stored deck is skipped (cards stay unsynced) without
    failing other batches.

    Failure isolation: nothing here ever propagates. If Anki/AnkiConnect is
    unreachable the cards simply stay unsynced for a later manual
    ``push_to_anki`` run (or a future retry mechanism - see #58). A per-card
    AnkiConnect error (bad note type, duplicate, ...) is already isolated to
    that one card inside ``push_accepted_cards`` itself and does not reach
    here at all. A deleted batch id is a silent no-op.
    """
    # Tolerate old queued calls that passed the id positionally inside
    # *args, or under a different kwarg name.
    if batch_id is None and args:
        batch_id = args[0]
    if batch_id is None:
        for key in ("batch_pk", "pk", "id"):
            if key in kwargs:
                batch_id = kwargs[key]
                break
    try:
        if batch_id is not None:
            from .models import Batch

            pk = getattr(batch_id, "pk", batch_id)
            try:
                pk = int(pk)
            except (TypeError, ValueError):
                logger.info(
                    "push_accepted_cards_task: ignoring unusable batch id %r",
                    batch_id,
                )
                return
            if not Batch.objects.filter(pk=pk).exists():
                logger.info(
                    "push_accepted_cards_task: batch %s gone, nothing to push",
                    pk,
                )
                return
            push_accepted_cards(batch_id=pk)
        else:
            push_accepted_cards()
    except AnkiUnreachableError as exc:
        logger.info(
            "push_accepted_cards_task: Anki unreachable, leaving card(s) unsynced (%s)",
            exc,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("push_accepted_cards_task: unexpected error pushing to Anki")
