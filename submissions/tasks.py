"""Background tasks for the review/Anki-push flow.

Finishing review of a batch (``card_review_finish`` in
``submissions/views.py``) enqueues one :func:`push_accepted_cards_task` per
finish with the finished batch's id, which pushes only that batch's
accepted-and-unsynced cards to its stored ``deck_name`` in the background
so ``manage.py push_to_anki`` is no longer a required manual step for the
common case (issue #57; per-batch deck in issue #76).

Also hosts :func:`check_llm_alerts_task` (issue #90), a periodic check that
logs a warning when recent LLM cost or failure rate crosses a configured
threshold - see its docstring.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from huey.contrib.djhuey import db_periodic_task, db_task
from huey import crontab

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


# --- LLM cost/failure-rate alerting (issue #90) ---------------------------

#: Default window (minutes) used when ``LLM_ALERT_WINDOW_MINUTES`` is blank
#: or malformed.
_DEFAULT_ALERT_WINDOW_MINUTES = 60.0

#: Per-process, in-memory "currently breached?" state for each alert
#: dimension, so a WARNING is logged only on the not-breached -> breached
#: transition (re-alert suppression) and a recovery is logged once when a
#: breached dimension drops back under its threshold. Intentionally not
#: persisted anywhere - a worker restart clears it, and the next breached
#: check simply alerts again (acceptable per issue #90).
_alert_breached = {"cost": False, "failure_rate": False}


def _parse_nonneg_float(value: Optional[str]) -> Optional[float]:
    """Parse an env-var string as a non-negative float, else ``None``.

    Blank/unset, non-numeric, and negative values all map to ``None`` so
    callers can treat "invalid" the same as "unset" without raising at
    import/startup time (issue #90's malformed-value requirement).
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _track_alert(key: str, breached: bool, warning_message: str, recovered_message: str) -> None:
    """Log on breach transitions only, updating ``_alert_breached[key]``.

    Logs one ``WARNING`` on the not-breached -> breached transition, one
    ``INFO`` "recovered" line on breached -> not-breached, and nothing while
    the state is unchanged (re-alert suppression).
    """
    was_breached = _alert_breached.get(key, False)
    if breached and not was_breached:
        logger.warning(warning_message)
        _alert_breached[key] = True
    elif not breached and was_breached:
        logger.info(recovered_message)
        _alert_breached[key] = False


@db_periodic_task(crontab(minute="*/5"))
def check_llm_alerts_task() -> None:
    """Warn when recent LLM cost or failure rate crosses a threshold.

    Runs every 5 minutes under both ``manage.py dev`` and standalone
    ``manage.py run_huey`` (issue #90). Reads ``LLMCall`` rows (issue #28)
    from a rolling window (``LLM_ALERT_WINDOW_MINUTES``, default 60) ending
    at ``timezone.now()`` (UTC).

    Two independent, optionally-configured checks:

    - Cost: if ``LLM_ALERT_COST_USD_THRESHOLD`` is set and the summed
      ``estimated_cost_usd`` over the window exceeds it, warn.
    - Failure rate: if ``LLM_ALERT_FAILURE_RATE_THRESHOLD`` is set and
      ``failed_count / total_count`` (as a percentage) over the window
      exceeds it, warn. Skipped entirely when there are zero calls in the
      window (no division by zero, no alert either way).

    Both thresholds unset is a no-op: the task still runs on schedule but
    issues no queries and logs nothing. A malformed threshold or window
    value (non-numeric, negative) is treated as unset for that setting
    rather than raising. See :func:`_track_alert` for the re-alert
    suppression / recovery policy.
    """
    from .models import LLMCall

    cost_threshold = _parse_nonneg_float(settings.LLM_ALERT_COST_USD_THRESHOLD)
    failure_threshold = _parse_nonneg_float(settings.LLM_ALERT_FAILURE_RATE_THRESHOLD)

    if cost_threshold is None and failure_threshold is None:
        return

    window_minutes = _parse_nonneg_float(settings.LLM_ALERT_WINDOW_MINUTES)
    if not window_minutes:
        window_minutes = _DEFAULT_ALERT_WINDOW_MINUTES

    window_start = timezone.now() - timedelta(minutes=window_minutes)
    calls = LLMCall.objects.filter(created_at__gte=window_start)

    if cost_threshold is not None:
        total_cost = calls.aggregate(total=Sum("estimated_cost_usd"))["total"] or 0
        total_cost = float(total_cost)
        _track_alert(
            "cost",
            total_cost > cost_threshold,
            warning_message=(
                f"LLM cost alert: ${total_cost:.2f} over last "
                f"{window_minutes:g} min exceeds threshold ${cost_threshold:.2f}"
            ),
            recovered_message=(
                f"LLM cost alert recovered: ${total_cost:.2f} over last "
                f"{window_minutes:g} min back under threshold ${cost_threshold:.2f}"
            ),
        )

    if failure_threshold is not None:
        total_count = calls.count()
        if total_count > 0:
            failed_count = calls.filter(status=LLMCall.Status.FAILED).count()
            failure_rate = (failed_count / total_count) * 100
            _track_alert(
                "failure_rate",
                failure_rate > failure_threshold,
                warning_message=(
                    f"LLM failure-rate alert: {failure_rate:.1f}% "
                    f"({failed_count}/{total_count}) over last {window_minutes:g} min "
                    f"exceeds threshold {failure_threshold:.1f}%"
                ),
                recovered_message=(
                    f"LLM failure-rate alert recovered: {failure_rate:.1f}% "
                    f"({failed_count}/{total_count}) over last {window_minutes:g} min "
                    f"back under threshold {failure_threshold:.1f}%"
                ),
            )
