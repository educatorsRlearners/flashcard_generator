"""Post-generation stages for card generation (issue #130).

:func:`generation.generate_for` persists cards first (``bulk_create`` plus
the generation-OK mark) and then delegates all post-generation work to
:func:`run_post_generation` here:

* local semantic dedup plus the ``dedup_ready`` bookkeeping (issue #78),
* live-deck Anki dedup against the batch's stored deck (issues #29, #76),
* per-card image attachment (issue #12).

Each stage is best-effort with its own ``try/except`` plus logging; log
messages and result strings are identical to the previous inline code in
``generation.generate_for`` (pure move, no behavior change).

Disabling or replacing dedup needs no edit to ``generation.py``:

* ``DEDUP_ENABLED=0`` (Django setting, default on) skips the local-dedup
  embedding call while still marking ``dedup_ready``, so the status
  endpoint's ``terminal`` gating behaves as before.
* an explicit ``stages`` list (injected by the caller, passed through
  ``generate_for``) replaces the default stage pipeline entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from django.conf import settings

from submissions import dedup, images
from submissions.models import SubmittedURL

logger = logging.getLogger(__name__)

#: Default for :func:`dedup_enabled` when the setting is absent.
DEDUP_ENABLED_DEFAULT: bool = True


def dedup_enabled() -> bool:
    """Master switch for local semantic dedup (``DEDUP_ENABLED``).

    Default True. Accepts ``True``/``False`` booleans as well as common
    string forms (``"0"``/``"false"``/``"no"``/``"off"`` disable). When
    disabled, the local-dedup stage skips the embedding call but still
    marks ``dedup_ready`` (see :func:`local_dedup_stage`), so generation
    completes and the status endpoint never waits forever.
    """
    raw = getattr(settings, "DEDUP_ENABLED", DEDUP_ENABLED_DEFAULT)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return DEDUP_ENABLED_DEFAULT
    text = str(raw).strip().lower()
    return text not in ("0", "false", "no", "off", "")


def _mark_dedup_ready(submitted_url: SubmittedURL) -> None:
    """Record that ``dedup.dedup_cards()`` has been attempted (issue #78).

    Called once the ``dedup_cards`` call below returns *or* raises - dedup is
    best-effort here (see the try/except), so "attempted" - not "succeeded"
    - is what unblocks the status endpoint's ``terminal`` flag. Otherwise a
    ``ModelLoadError`` (or any other dedup exception) would poll forever.
    """
    submitted_url.dedup_ready = True
    submitted_url.save(update_fields=["dedup_ready"])


@dataclass
class PostGenerationOutcome:
    """Anki live-deck results collected by the stage runner.

    Local dedup and image attachment report nothing back (best-effort with
    logging only); the Anki stage's duplicates / matches / warnings are
    carried here so ``generate_for`` can merge them into its
    ``GenerationResult`` (never discarded).
    """

    #: How many new cards matched a note already in the Anki deck.
    anki_duplicates: int = 0
    #: Per-match detail: [{"note_id", "note_text", "similarity"}].
    anki_matches: list = field(default_factory=list)
    #: Non-fatal Anki warnings (e.g. unreachable -> local-only fallback).
    anki_warnings: list = field(default_factory=list)


#: A stage takes the URL, its freshly persisted cards, and the shared
#: outcome to record into (stages that report nothing ignore it).
Stage = Callable[..., None]


def local_dedup_stage(submitted_url, cards, outcome=None) -> None:
    """Local semantic dedup stage (#7) with ``dedup_ready`` bookkeeping (#78).

    A missing embedding model must not fail generation - the standalone
    ``dedup_cards`` command is the place that hard-fails and tells the
    engineer to run the download. ``dedup_ready`` is marked "attempted"
    whether dedup succeeded, was skipped (``ModelLoadError`` or
    ``DEDUP_ENABLED=0``) or raised something else, so the status
    endpoint's terminal flag never waits forever on a dedup failure.
    """
    if not dedup_enabled():
        logger.debug("post-generation dedup disabled for %s", submitted_url.url)
        _mark_dedup_ready(submitted_url)
        return
    try:
        dedup.dedup_cards(cards)
    except dedup.ModelLoadError as exc:
        logger.warning(
            "skipped post-generation dedup for %s: %s", submitted_url.url, exc
        )
    except Exception:  # noqa: BLE001 - dedup is best-effort here
        logger.exception("post-generation dedup failed for %s", submitted_url.url)
    finally:
        _mark_dedup_ready(submitted_url)


def anki_live_deck_stage(submitted_url, cards, outcome) -> None:
    """Live-deck semantic dedup stage (#29): compare the new cards against
    the notes currently in the batch's stored Anki deck (#76), never
    ANKI_DECK_NAME.

    Best-effort like local dedup - no stored deck skips live dedup with a
    warning (local-only applies), an unreachable Anki falls back to
    local-only; the returned ``AnkiDedupResult`` is stored on the outcome
    (never discarded) so the command output surfaces the matched note /
    warning. Generation always completes.
    """
    try:
        from submissions import anki as _anki

        batch_deck = getattr(getattr(submitted_url, "batch", None), "deck_name", None)
        anki_result = _anki.dedup_cards_against_anki(cards, deck_name=batch_deck)
        outcome.anki_duplicates = int(anki_result.duplicates or 0)
        outcome.anki_matches = [
            {
                "note_id": m.note_id,
                "note_text": m.note_text,
                "similarity": m.similarity,
            }
            for m in (anki_result.matches or [])
        ]
        if getattr(anki_result, "warning", ""):
            outcome.anki_warnings.append(anki_result.warning)
    except Exception as exc:  # noqa: BLE001 - live-deck dedup is best-effort here
        logger.exception("live-deck dedup failed for %s", submitted_url.url)
        outcome.anki_warnings.append(
            f"Anki deck dedup skipped (live-deck dedup failed: {exc}); "
            "local-only dedup applied."
        )


def image_attachment_stage(submitted_url, cards, outcome=None) -> None:
    """Per-card image stage (#12): source-page image first, Draw Things
    fallback, otherwise no image. Best-effort - image trouble (unreachable
    Draw Things, a failed fetch) never fails or aborts card generation.
    """
    try:
        images.attach_images(submitted_url, cards)
    except Exception:  # noqa: BLE001 - images are strictly best-effort
        logger.exception("image attachment failed for %s", submitted_url.url)


#: Default pipeline run by :func:`run_post_generation` (local dedup, then
#: live-deck Anki dedup, then image attachment - the previous inline order
#: in ``generate_for``).
DEFAULT_STAGES: tuple = (
    local_dedup_stage,
    anki_live_deck_stage,
    image_attachment_stage,
)


def run_post_generation(
    submitted_url,
    cards,
    *,
    stages: Optional[Sequence[Stage]] = None,
) -> PostGenerationOutcome:
    """Run the post-generation stages in order, returning their outcome.

    *stages* replaces :data:`DEFAULT_STAGES` when given (the injection seam:
    pass stubs or a subset without editing generation code). Every stage
    handles its own errors, so a raising stage never stops the later ones
    from running.
    """
    outcome = PostGenerationOutcome()
    for stage in DEFAULT_STAGES if stages is None else stages:
        stage(submitted_url, cards, outcome)
    return outcome
