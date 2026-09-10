"""Local semantic dedup of generated cards (issue #7).

After cards are generated (#6) and before they reach the review grid (#9),
every new :class:`~submissions.models.Card` is embedded locally with a small
CPU sentence-transformer and compared, by cosine similarity, against

* cards already stored from previous runs (``dedup_status == "unique"``), and
* the other new cards in the same run / batch.

A card at or above :data:`DEDUP_SIMILARITY_THRESHOLD` similarity to another
card is marked ``duplicate`` with ``duplicate_of`` pointing at the card it
matched, and is hidden from the default review grid (never deleted). With no
other cards to compare against, every card is marked ``unique`` and its
embedding is recorded.

Everything runs offline after a one-time model download. The model load is
isolated in :func:`load_embedding_model` so tests stub it with a deterministic
fake encoder and never touch the network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np

from submissions.models import Card

logger = logging.getLogger(__name__)

#: Sentence-transformer model id. ``all-MiniLM-L6-v2`` is a 22M-parameter,
#: 384-dim model: a few tens of MB on disk, fast on CPU, and a solid default
#: for short-text semantic similarity. Changing this requires a one-time
#: re-download (see the warm-up step in the README) and invalidates every
#: cached ``Card.embedding`` (re-run ``dedup_cards --all --force``).
EMBEDDING_MODEL_NAME: str = "all-MiniLM-L6-v2"

#: Cosine-similarity value at or above which two cards count as
#: near-duplicates. Rationale: with ``all-MiniLM-L6-v2`` normalised
#: embeddings, unrelated flashcard terms typically score well under 0.5,
#: light paraphrases / plural forms ("trace" vs "traces", "span" vs "a span
#: in tracing") land around 0.6-0.85, and true restatements sit above 0.85.
#: 0.82 keeps genuine paraphrases out of the review grid while leaving
#: merely related-but-distinct terms ("span" vs "evaluation") as unique.
#: This is the single place to tune dedup aggressiveness.
DEDUP_SIMILARITY_THRESHOLD: float = 0.82


class ModelLoadError(RuntimeError):
    """Raised when the embedding model cannot be loaded / found.

    The message names the one-time download step. The management command
    turns this into a non-zero exit without marking any card.
    """


# The model is expensive to construct; keep one per process.
_MODEL_CACHE: dict = {}


def load_embedding_model():
    """Load and cache the sentence-transformer model.

    This is the ONLY place the real model is constructed - tests monkeypatch
    this function to return a deterministic fake encoder, so the rest of the
    module (and the whole test suite) stays offline.

    :raises ModelLoadError: if ``sentence-transformers`` or the model weights
        are missing.
    """
    if "model" not in _MODEL_CACHE:
        try:
            from sentence_transformers import SentenceTransformer

            _MODEL_CACHE["model"] = SentenceTransformer(EMBEDDING_MODEL_NAME)
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise ModelLoadError(
                f"Could not load the embedding model {EMBEDDING_MODEL_NAME!r}: "
                f"{exc}\n"
                "Run the one-time model download (needs network, ~tens of MB):\n"
                '  uv run python -c "from sentence_transformers import '
                "SentenceTransformer; "
                f"SentenceTransformer('{EMBEDDING_MODEL_NAME}')\""
            ) from exc
    return _MODEL_CACHE["model"]


# --- Result types ---------------------------------------------------------


@dataclass
class CardDedupResult:
    """Per-card outcome of a dedup run."""

    card: Card
    status: str  # Card.DedupStatus value
    duplicate_of: Optional[Card] = None
    similarity_score: Optional[float] = None
    note: str = ""  # e.g. "empty text - left unique"

    def line(self) -> str:
        term = self.card.source_term or self.card.front[:40]
        head = f"card {self.card.pk} ({term}): "
        if self.note:
            return head + f"{self.status} - {self.note}"
        if self.status == Card.DedupStatus.DUPLICATE and self.duplicate_of:
            return (
                head
                + f"duplicate of card {self.duplicate_of.pk} "
                + f"(similarity {self.similarity_score:.3f})"
            )
        score = "n/a" if self.similarity_score is None else f"{self.similarity_score:.3f}"
        return head + f"unique (max similarity {score})"


@dataclass
class DedupSummary:
    results: list

    @property
    def duplicates(self) -> int:
        return sum(1 for r in self.results if r.status == Card.DedupStatus.DUPLICATE)

    @property
    def unique(self) -> int:
        return sum(1 for r in self.results if r.status == Card.DedupStatus.UNIQUE)


# --- Helpers ------------------------------------------------------------


def _combined_text(card: Card) -> str:
    parts = [card.front or "", card.back or "", card.source_term or ""]
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


def _normalise(matrix: np.ndarray) -> np.ndarray:
    """Row-normalise a 2-D array; zero rows stay zero (never NaN)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


# --- Entry point ------------------------------------------------------


def dedup_cards(cards: Iterable[Card], *, force: bool = False) -> DedupSummary:
    """Embed ``cards`` and classify each as ``unique`` or ``duplicate``.

    * Embeddings are computed in ONE ``model.encode([...])`` call per run and
      cached on ``Card.embedding``; a re-run without ``force`` reuses the
      stored vector.
    * Comparison scope: every other stored ``unique`` card, plus the earlier
      (lower-pk) cards in this same run. Same-run near-duplicates keep the
      lowest pk as ``unique``; the rest point at it.
    * A card whose combined text is empty is left ``unique`` with a null
      score and a warning - never a crash or a NaN.

    :raises ModelLoadError: if the model cannot be loaded (nothing is marked).
    """
    cards = sorted(cards, key=lambda c: c.pk)
    if not cards:
        return DedupSummary(results=[])

    model = load_embedding_model()  # may raise ModelLoadError - caller handles

    new_pks = {c.pk for c in cards}
    existing = list(
        Card.objects.exclude(pk__in=new_pks).filter(
            dedup_status=Card.DedupStatus.UNIQUE
        )
    )

    # Figure out which rows still need an embedding, then encode them all at
    # once (single model.encode call).
    to_encode: list[tuple[Card, str]] = []
    for card in cards:
        text = _combined_text(card)
        if not text:
            continue
        if force or not card.embedding:
            to_encode.append((card, text))
    existing_to_encode: list[Card] = []
    for card in existing:
        if not card.embedding:
            text = _combined_text(card)
            if text:
                to_encode.append((card, text))
                existing_to_encode.append(card)

    if to_encode:
        vectors = np.asarray(
            model.encode([text for _, text in to_encode]), dtype=float
        )
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        for (card, _), vec in zip(to_encode, vectors):
            card.embedding = [float(x) for x in vec]
        # Persist embeddings computed for pre-existing rows (new rows are
        # saved in full below).
        for card in existing_to_encode:
            card.save(update_fields=["embedding"])

    # Build the running comparison pool from existing unique cards.
    pool_cards: list[Card] = [c for c in existing if c.embedding]
    pool_matrix = (
        _normalise(np.asarray([c.embedding for c in pool_cards], dtype=float))
        if pool_cards
        else np.empty((0, 0))
    )

    results: list[CardDedupResult] = []
    updated: list[Card] = []

    for card in cards:
        text = _combined_text(card)
        if not text or not card.embedding:
            card.dedup_status = Card.DedupStatus.UNIQUE
            card.duplicate_of = None
            card.similarity_score = None
            logger.warning("card %s has empty text; left unique", card.pk)
            results.append(
                CardDedupResult(
                    card=card,
                    status=Card.DedupStatus.UNIQUE,
                    note="empty text - left unique, no score",
                )
            )
            updated.append(card)
            continue

        vec = np.asarray(card.embedding, dtype=float)
        norm = np.linalg.norm(vec) or 1.0
        vec = vec / norm

        if pool_matrix.shape[0]:
            sims = pool_matrix @ vec  # vectorised matrix cosine, no double loop
            best_idx = int(np.argmax(sims))
            best = float(sims[best_idx])
        else:
            best_idx = -1
            best = 0.0

        if best >= DEDUP_SIMILARITY_THRESHOLD and best_idx >= 0:
            match = pool_cards[best_idx]
            card.dedup_status = Card.DedupStatus.DUPLICATE
            card.duplicate_of = match
            card.similarity_score = best
            results.append(
                CardDedupResult(
                    card=card,
                    status=Card.DedupStatus.DUPLICATE,
                    duplicate_of=match,
                    similarity_score=best,
                )
            )
        else:
            card.dedup_status = Card.DedupStatus.UNIQUE
            card.duplicate_of = None
            card.similarity_score = best
            results.append(
                CardDedupResult(
                    card=card,
                    status=Card.DedupStatus.UNIQUE,
                    similarity_score=best,
                )
            )
            # Only unique cards join the pool for later cards in this run.
            pool_cards.append(card)
            row = _normalise(vec.reshape(1, -1))
            pool_matrix = (
                row
                if not pool_matrix.shape[0]
                else np.vstack([pool_matrix, row])
            )
        updated.append(card)

    for card in updated:
        card.save(
            update_fields=[
                "dedup_status",
                "duplicate_of",
                "similarity_score",
                "embedding",
            ]
        )

    return DedupSummary(results=results)
