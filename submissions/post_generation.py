"""Post-generation stages for card generation (issue #130).

:func:`generation.generate_for` persists cards first (``bulk_create`` plus
the generation-OK mark) and then delegates all post-generation work to
:func:`run_post_generation` here: per-card image attachment (issue #12).

Each stage is best-effort with its own ``try/except`` plus logging; log
messages and result strings are identical to the previous inline code in
``generation.generate_for`` (pure move, no behavior change).

An explicit ``stages`` list (injected by the caller, passed through
``generate_for``) replaces the default stage pipeline entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from submissions import images

logger = logging.getLogger(__name__)


@dataclass
class PostGenerationOutcome:
    """Outcome collected by the stage runner.

    Image attachment reports nothing back (best-effort with logging only);
    this is kept as the shared outcome object so future stages have
    somewhere to record results without changing the stage signature.
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


def image_attachment_stage(submitted_url, cards, outcome=None) -> None:
    """Per-card image stage (#12): source-page image first, Draw Things
    fallback, otherwise no image. Best-effort - image trouble (unreachable
    Draw Things, a failed fetch) never fails or aborts card generation.
    """
    try:
        images.attach_images(submitted_url, cards)
    except Exception:  # noqa: BLE001 - images are strictly best-effort
        logger.exception("image attachment failed for %s", submitted_url.url)


#: Default pipeline run by :func:`run_post_generation` (image attachment
#: only, per issue #169's removal of the dedup stages).
DEFAULT_STAGES: tuple = (image_attachment_stage,)


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
