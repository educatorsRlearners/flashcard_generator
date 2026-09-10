"""Card generation from ``SubmittedURL.extracted_text`` (issue #6).

This module turns already-extracted page text into ``Card`` rows via the
provider-agnostic LLM client in :mod:`submissions.llm` (issue #5). It does
generation only: no dedup (#7), no review UI (#9), no Anki push (#11).

Note-type auto-detection rule
-----------------------------
The note type is decided **per card**, by the model, following the rule
baked into :data:`SYSTEM_PROMPT`:

* **basic** - a term that has a standalone definition. ``front`` holds the
  term or a question, ``back`` holds the definition.
* **cloze** - a term that appears naturally inside a reusable sentence.
  ``front`` holds that sentence with the term wrapped in Anki cloze markers
  (``{{c1::...}}``, ``{{c2::...}}`` for further deletions); ``back`` may be
  blank.

A single URL can therefore produce a mix of both. Validation here rejects a
``cloze`` card with no ``{{cN::...}}`` marker, a card with an empty front,
a bad/absent ``source_term``, and an unknown note type.

Card count
----------
The count is driven by the density of the content: the prompt tells the
model not to pad, and nothing here targets a fixed N. :data:`MAX_CARDS_PER_URL`
is a safety cap only - extra cards past it are dropped, not an objective.

The ``batch`` link on a card
----------------------------
A ``Card`` copies ``submitted_url.batch`` - the URL's originating batch
(``SubmittedURL.batch``, per the #15 ``BatchRequest`` model). That FK is
nullable and may already be null (URL created outside a batch, or its
originating batch was deleted); we copy whatever is there, ``None``
included. ``submitted_url`` is always the authoritative link.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from submissions import llm
from submissions.models import Card, SubmittedURL

# --- Named constants (no magic literals in the logic below) -------------

#: Minimum number of non-whitespace characters in ``extracted_text`` for a
#: URL to be worth sending to the model. Below this the URL is skipped with
#: reason ``INSUFFICIENT_CONTENT``.
MIN_CONTENT_CHARS: int = 200

#: Safety cap on cards persisted per URL. Purely a guard against a runaway
#: response - NOT a target count. Valid cards past this many are discarded.
MAX_CARDS_PER_URL: int = 40

#: Upper bound on how much extracted text is put in the prompt. Longer text
#: is truncated at this many characters before the call.
MAX_PROMPT_CHARS: int = 12_000

#: Output-token ceiling for the generation call.
GENERATION_MAX_TOKENS: int = 4_096

#: Skip / failure reason strings (also asserted by the tests).
INSUFFICIENT_CONTENT = "insufficient content for generation"
NO_VALID_CARDS = "no valid cards produced"
ALREADY_HAS_CARDS = "already has cards"

_VALID_NOTE_TYPES = {Card.NoteType.BASIC, Card.NoteType.CLOZE}
_CLOZE_MARKER_RE = re.compile(r"\{\{c\d+::.+?\}\}", re.DOTALL)

# LLMBadResponseError reasons that mean "the model's payload was unusable as
# a card list" rather than "the model refused / was cut off".
_MALFORMED_REASONS = {"malformed_json", "schema_violation"}


# --- Prompt -----------------------------------------------------------

SYSTEM_PROMPT = """You are an expert flashcard author. Given the text of a \
web page, produce Anki-style flashcards that teach its key terms and concepts.

Return a JSON object of the form {"cards": [ ... ]}. Each card object has:
  - note_type: "basic" or "cloze" (choose per card, see below)
  - front: for "basic", the term or question; for "cloze", a sentence that \
uses the term with the term wrapped in Anki cloze markers {{c1::...}} \
(use {{c2::...}}, {{c3::...}} for additional deletions in the same sentence)
  - back: for "basic", the definition; for "cloze", may be "" or extra info
  - source_term: the concept the card teaches (never empty)
  - topic: a short topic label for the card, or "" if none is clear

Note-type rule, applied per card:
  - "basic"  -> a term that has a standalone definition
  - "cloze"  -> a term that appears naturally inside a reusable sentence

The number of cards must follow the density of the content: a short or \
sparse page yields few cards, a rich page yields many. Do not pad. \
Definitions may draw on both the page text and your own knowledge.
"""

#: JSON Schema handed to :func:`submissions.llm.generate` as ``response_format``
#: so the client asks for schema-validated structured output (never free text
#: this module then regex-parses). Item fields are intentionally permissive -
#: per-card validation (empty front, cloze markers, note type) happens in
#: :func:`_validated_cards` so individual bad cards can be filtered rather
#: than failing the whole response.
CARD_LIST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_type": {"type": "string"},
                    "front": {"type": "string"},
                    "back": {"type": "string"},
                    "source_term": {"type": "string"},
                    "topic": {"type": "string"},
                },
            },
        }
    },
    "required": ["cards"],
}


# --- Result type ----------------------------------------------------


@dataclass
class GenerationResult:
    """Outcome of :func:`generate_for` for one URL."""

    outcome: str  # "created" | "skipped" | "failed"
    reason: str = ""
    counts: dict = field(default_factory=dict)  # {"basic": n, "cloze": n}
    rejected: int = 0

    @property
    def total_created(self) -> int:
        return sum(self.counts.values())

    def summary_line(self, url: str) -> str:
        if self.outcome == "created":
            by_type = ", ".join(
                f"{n} {name}" for name, n in sorted(self.counts.items()) if n
            )
            line = f"{url} created: {by_type or '0 cards'}"
            if self.rejected:
                line += f" ({self.rejected} rejected)"
            return line
        return f"{url} {self.outcome}: {self.reason}"


# --- Helpers -------------------------------------------------------


def _non_whitespace_len(text: str) -> int:
    return len("".join(text.split()))


def mark_generation_failed(submitted_url: SubmittedURL, reason: str) -> None:
    """Record that card generation failed for this URL, without touching its
    extraction status. Persists only the two generation fields."""
    submitted_url.generation_status = SubmittedURL.GenerationStatus.FAILED
    submitted_url.generation_error = reason
    submitted_url.save(update_fields=["generation_status", "generation_error"])


def _mark_generation_ok(submitted_url: SubmittedURL) -> None:
    submitted_url.generation_status = SubmittedURL.GenerationStatus.OK
    submitted_url.generation_error = ""
    submitted_url.save(update_fields=["generation_status", "generation_error"])


def _call_llm(submitted_url: SubmittedURL) -> list[dict]:
    """Call the #5 client and return the raw list of card dicts.

    Lets :class:`llm.LLMError` subclasses propagate to the caller, except that
    a malformed / schema-violating payload is turned into an empty list so the
    caller records it as "no valid cards produced".
    """
    content = (submitted_url.extracted_text or "")[:MAX_PROMPT_CHARS]
    prompt = (
        f"Source URL: {submitted_url.url}\n"
        f"Title: {submitted_url.extracted_title or '(none)'}\n\n"
        f"Page text:\n{content}"
    )
    try:
        result = llm.generate(
            system=SYSTEM_PROMPT,
            prompt=prompt,
            response_format=CARD_LIST_SCHEMA,
            max_tokens=GENERATION_MAX_TOKENS,
        )
    except llm.LLMBadResponseError as exc:
        if getattr(exc, "reason", "") in _MALFORMED_REASONS:
            return []
        raise

    data: Any = result.parsed
    if data is None:
        try:
            data = json.loads(result.text)
        except (ValueError, TypeError):
            return []
    if not isinstance(data, dict):
        return []
    cards = data.get("cards")
    return [c for c in cards if isinstance(c, dict)] if isinstance(cards, list) else []


def _validated_cards(
    raw_cards: list[dict], submitted_url: SubmittedURL
) -> tuple[list[Card], int]:
    """Turn raw card dicts into unsaved ``Card`` instances, dropping invalid
    ones. Returns ``(cards, rejected_count)``.
    """
    today = timezone.now().date().isoformat()
    kept: list[Card] = []
    rejected = 0

    for raw in raw_cards:
        note_type = str(raw.get("note_type", "")).strip().lower()
        front = str(raw.get("front", "")).strip()
        back = str(raw.get("back", "") or "").strip()
        source_term = str(raw.get("source_term", "")).strip()
        topic = str(raw.get("topic", "") or "").strip()

        if note_type not in _VALID_NOTE_TYPES:
            rejected += 1
            continue
        if not front or not source_term:
            rejected += 1
            continue
        if note_type == Card.NoteType.CLOZE and not _CLOZE_MARKER_RE.search(front):
            rejected += 1
            continue

        if len(kept) >= MAX_CARDS_PER_URL:
            rejected += 1
            continue

        kept.append(
            Card(
                submitted_url=submitted_url,
                batch=submitted_url.batch,
                note_type=note_type,
                front=front,
                back=back,
                source_term=source_term,
                tags={
                    "source_url": submitted_url.url,
                    "date_added": today,
                    "topic": topic,
                },
            )
        )

    return kept, rejected


# --- Entry point --------------------------------------------------


def generate_for(
    submitted_url: SubmittedURL, *, force: bool = False
) -> GenerationResult:
    """Generate and persist cards for one ``SubmittedURL``.

    Saves transactionally: the full validated card list is built first, then
    written in one ``atomic`` block (with a ``bulk_create``) - a failure never
    leaves half-written cards.

    * ``force=False`` and the URL already has cards -> skipped.
    * ``force=True`` -> existing cards for the URL are deleted, then regenerated.

    :class:`llm.LLMError` subclasses (auth, rate-limit, transient, bad-response)
    are **not** caught here - the management command maps them to exit codes.
    """
    has_cards = submitted_url.cards.exists()
    if has_cards and not force:
        return GenerationResult(outcome="skipped", reason=ALREADY_HAS_CARDS)

    content = submitted_url.extracted_text or ""
    if _non_whitespace_len(content) < MIN_CONTENT_CHARS:
        mark_generation_failed(submitted_url, INSUFFICIENT_CONTENT)
        return GenerationResult(outcome="skipped", reason=INSUFFICIENT_CONTENT)

    raw_cards = _call_llm(submitted_url)
    cards, rejected = _validated_cards(raw_cards, submitted_url)

    if not cards:
        mark_generation_failed(submitted_url, NO_VALID_CARDS)
        return GenerationResult(
            outcome="failed", reason=NO_VALID_CARDS, rejected=rejected
        )

    with transaction.atomic():
        if force and has_cards:
            submitted_url.cards.all().delete()
        Card.objects.bulk_create(cards)
        _mark_generation_ok(submitted_url)

    counts = {
        Card.NoteType.BASIC.value: sum(
            1 for c in cards if c.note_type == Card.NoteType.BASIC
        ),
        Card.NoteType.CLOZE.value: sum(
            1 for c in cards if c.note_type == Card.NoteType.CLOZE
        ),
    }
    return GenerationResult(outcome="created", counts=counts, rejected=rejected)
