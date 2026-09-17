"""Card generation from ``SubmittedURL.extracted_text`` (issue #6).

This module turns already-extracted page text into ``Card`` rows via the
provider-agnostic LLM client in :mod:`submissions.llm` (issue #5). It does
generation only: no review UI (#9), no Anki push (#11).

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
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from django.conf import settings
from django.db import transaction
from django.utils import timezone

# ``images`` stays imported (no direct use below) so existing patch points
# keep working.
from submissions import feedback, images, llm, post_generation
from submissions.models import Card, SubmittedURL

logger = logging.getLogger(__name__)

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

#: Few-shot cap (issue #10). The generation prompt embeds at most this many
#: most-recent *accepted* ``Feedback`` rows and, separately, at most this many
#: most-recent *rejected* rows - so the few-shot section never grows past
#: ``2 * FEWSHOT_EXAMPLES_PER_CATEGORY`` examples no matter how much feedback
#: accumulates. Selection is "most recent N per category" by timestamp;
#: within the section the examples are ordered oldest-first for a stable,
#: deterministic prompt string.
FEWSHOT_EXAMPLES_PER_CATEGORY: int = 3

#: Skip / failure reason strings (also asserted by the tests).
INSUFFICIENT_CONTENT = "insufficient content for generation"
NO_VALID_CARDS = "no valid cards produced"
ALREADY_HAS_CARDS = "already has cards"

#: Maximum allowed contiguous verbatim run (in words) copied from the page's
#: extracted text into a card's front/back (issue #32). A card with a longer
#: run is "close to source wording". Word-window scan, case- and
#: punctuation-insensitive.
MAX_VERBATIM_WORDS: int = 12

#: Max characters of a matched Anki note's text shown in
#: :meth:`GenerationResult.summary_line` (issue #29). Longer texts are
#: truncated with an ellipsis; the full text stays on the ``AnkiDedupMatch``.
ANKI_MATCH_TEXT_PREVIEW_CHARS: int = 80

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

Restate definitions in your own words. Do not reuse the page's sentences \
verbatim - say the same thing with different wording, never copying a \
sentence from the page.

When you use a programming analogy, use python.
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
                    "note_type": {"type": "string", "enum": ["basic", "cloze"]},
                    "front": {"type": "string"},
                    "back": {"type": "string"},
                    "source_term": {"type": "string"},
                    "topic": {"type": "string"},
                },
                # The Anthropic structured-output API requires every object in
                # the schema to list all its properties in ``required`` and to
                # set ``additionalProperties: false`` explicitly.
                "required": [
                    "note_type",
                    "front",
                    "back",
                    "source_term",
                    "topic",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}


# --- Few-shot section from stored feedback (issue #131) -----------------
#
# Moved to :mod:`submissions.feedback` - this module keeps only thin
# re-exports (so existing imports keep working) and the one-line prompt
# composition in :func:`build_system_prompt`. Prompt output, stored feedback
# and selection order are byte-identical to the previous inline code.

#: Re-exported intact (see :mod:`submissions.feedback`).
FEWSHOT_EXAMPLES_PER_CATEGORY = feedback.FEWSHOT_EXAMPLES_PER_CATEGORY
FEWSHOT_TOKEN_BUDGET = feedback.FEWSHOT_TOKEN_BUDGET
FEWSHOT_ACCEPTED_SHARE = feedback.FEWSHOT_ACCEPTED_SHARE
FEWSHOT_SELECTION_MODE = feedback.FEWSHOT_SELECTION_MODE
FEWSHOT_MIN_FEEDBACK_CHARS = feedback.FEWSHOT_MIN_FEEDBACK_CHARS
FEWSHOT_CHARS_PER_TOKEN = feedback.FEWSHOT_CHARS_PER_TOKEN
_FEWSHOT_HEADER = feedback._FEWSHOT_HEADER
_FEEDBACK_TOKEN_CACHE = feedback._FEEDBACK_TOKEN_CACHE
_EMBED_CACHE = feedback._EMBED_CACHE

fewshot_enabled = feedback.fewshot_enabled
fewshot_token_budget = feedback.fewshot_token_budget
fewshot_accepted_share = feedback.fewshot_accepted_share
fewshot_selection_mode = feedback.fewshot_selection_mode
fewshot_min_feedback_chars = feedback.fewshot_min_feedback_chars
fewshot_chars_per_token = feedback.fewshot_chars_per_token
set_token_counter = feedback.set_token_counter
estimate_tokens = feedback.estimate_tokens
set_feedback_embed_fn = feedback.set_feedback_embed_fn
get_feedback_embed_fn = feedback.get_feedback_embed_fn
clear_fewshot_cache = feedback.clear_fewshot_cache
_format_feedback_example = feedback._format_feedback_example
_recent_feedback = feedback._recent_feedback
_feedback_example_text = feedback._feedback_example_text
_word_set = feedback._word_set
_cached_word_set = feedback._cached_word_set
_overlap_score = feedback._overlap_score
_cosine = feedback._cosine
_ranked_for_decision = feedback._ranked_for_decision
_budgeted_recency = feedback._budgeted_recency
select_feedback_for_page = feedback.select_feedback_for_page
_render_fewshot_section = feedback._render_fewshot_section
build_fewshot_section = feedback.build_fewshot_section


def __getattr__(name):
    """Proxy moved few-shot scalars (``_TOKEN_COUNTER``,
    ``_FEEDBACK_EMBED_FN``) to :mod:`submissions.feedback`.

    They are rebound by the ``set_*`` hooks, so a static alias would go
    stale - attribute access always reads the live value instead.
    """
    if name in ("_TOKEN_COUNTER", "_FEEDBACK_EMBED_FN"):
        return getattr(feedback, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def analogy_language() -> str:
    """Configured analogy language (``CARD_ANALOGY_LANGUAGE``, default "python")."""
    return getattr(settings, "CARD_ANALOGY_LANGUAGE", "python") or "python"


def build_system_prompt(page_text: str | None = None) -> str:
    """``SYSTEM_PROMPT`` plus the feedback few-shot section, if any.

    The analogy language (``CARD_ANALOGY_LANGUAGE``, default "python") is
    baked into ``SYSTEM_PROMPT`` with its default value so the zero-feedback
    prompt equals ``SYSTEM_PROMPT``; a non-default configured language is
    swapped into that sentence here (settings/env change, no code edit).

    *page_text* (the current page's extracted text) enables #25's
    relevance-ranked few-shot selection; ``None`` keeps #10's recency pick.
    Few-shot rendering (including the ``FEWSHOT_ENABLED`` / budget
    short-circuit) lives in :mod:`submissions.feedback` - this is only the
    one-line composition.
    """
    language = analogy_language()
    base = SYSTEM_PROMPT
    if language.lower() != "python":
        base = base.replace(
            "When you use a programming analogy, use python.",
            f"When you use a programming analogy, use {language}.",
        )
    return base + feedback.render_section(page_text)


# --- Result type ----------------------------------------------------


@dataclass
class GenerationResult:
    """Outcome of :func:`generate_for` for one URL."""

    outcome: str  # "created" | "skipped" | "failed"
    reason: str = ""
    counts: dict = field(default_factory=dict)  # {"basic": n, "cloze": n}
    rejected: int = 0
    close_to_source: int = 0
    #: How many new cards matched a note already in the Anki deck (#29).
    anki_duplicates: int = 0
    #: Per-match detail: [{"note_id", "note_text", "similarity"}]. Carries
    #: the matched Anki note so the command output can show it.
    anki_matches: list = field(default_factory=list)
    #: Non-fatal Anki warnings (e.g. unreachable -> local-only fallback).
    anki_warnings: list = field(default_factory=list)

    @property
    def total_created(self) -> int:
        return sum(self.counts.values())

    def summary_line(self, url: str) -> str:
        if self.outcome == "created":
            by_type = ", ".join(
                f"{n} {name}" for name, n in sorted(self.counts.items()) if n
            )
            line = f"{url} created: {by_type or '0 cards'}"
            suffixes = []
            if self.rejected:
                suffixes.append(f"{self.rejected} rejected")
            if self.close_to_source:
                suffixes.append(
                    f"{self.close_to_source} cards close to source wording"
                )
            if self.anki_duplicates:
                suffixes.append(
                    f"{self.anki_duplicates} skipped as already in Anki deck"
                )
            if suffixes:
                line += f" ({', '.join(suffixes)})"
            for match in self.anki_matches:
                text = str((match or {}).get("note_text", "") or "")
                if len(text) > ANKI_MATCH_TEXT_PREVIEW_CHARS:
                    text = text[:ANKI_MATCH_TEXT_PREVIEW_CHARS].rstrip() + "…"
                line += f'\n  skipped as already in Anki deck (matched note: "{text}")'
            for warning in self.anki_warnings:
                line += f"\n  {warning}"
            return line
        return f"{url} {self.outcome}: {self.reason}"


# --- Helpers -------------------------------------------------------


def _non_whitespace_len(text: str) -> int:
    return len("".join(text.split()))


# --- Verbatim-overlap check (issue #32) ----------------------------------
# Decision (b): a card that trips the check is KEPT, not regenerated or
# dropped - the run only logs "N cards close to source wording" in the
# command output (via ``GenerationResult.summary_line``). Rationale: simpler
# and non-destructive; the reviewer still sees the card and can reject it,
# which then feeds the few-shot loop. The per-card validation in
# ``_validated_cards`` is untouched; this check is purely additive.

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    """Lowercased alphanumeric word tokens (case-/punctuation-insensitive)."""
    return _WORD_RE.findall(text.lower())


def longest_verbatim_run(card_text: str, source_text: str) -> int:
    """Longest contiguous verbatim word run shared with the source text.

    Word-window scan capped at ``MAX_VERBATIM_WORDS + 1``: returns
    ``min(true_longest, MAX_VERBATIM_WORDS + 1)``, which is all the caller
    needs to decide "tripped or not" and keeps the scan cheap (window size
    never exceeds 13) no matter how long the page is.
    """
    card_words = _words(card_text or "")
    source_words = _words(source_text or "")
    if not card_words or not source_words:
        return 0
    cap = MAX_VERBATIM_WORDS + 1
    cap = min(cap, len(card_words), len(source_words))
    # Binary search the largest L in [0, cap] with a shared L-gram.
    lo = 0
    hi = cap
    while lo < hi:
        mid = (lo + hi + 1) // 2
        source_grams = {
            tuple(source_words[i : i + mid])
            for i in range(len(source_words) - mid + 1)
        }
        found = any(
            tuple(card_words[i : i + mid]) in source_grams
            for i in range(len(card_words) - mid + 1)
        )
        if found:
            lo = mid
        else:
            hi = mid - 1
    return lo


def is_close_to_source(card_text: str, source_text: str) -> bool:
    """True when *card_text* contains a verbatim run of >MAX_VERBATIM_WORDS."""
    return longest_verbatim_run(card_text, source_text) > MAX_VERBATIM_WORDS


def count_close_to_source(cards: list[Card], source_text: str) -> int:
    """How many kept cards have front or back close to the source wording."""
    n = 0
    for card in cards:
        if is_close_to_source(card.front or "", source_text) or is_close_to_source(
            card.back or "", source_text
        ):
            n += 1
    return n


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


def _call_llm(
    submitted_url: SubmittedURL,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> list[dict]:
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
        # Attribute the call for LLM observability (#28): the client reads
        # this context when it records its LLMCall row. A context manager
        # (rather than new kwargs) keeps this call signature unchanged.
        with llm.call_context(
            batch=submitted_url.batch, submitted_url=submitted_url
        ):
            # No-override path calls llm.generate exactly as before
            # (byte-for-byte identical); overrides are only passed when set.
            _extra: dict = {}
            if provider is not None:
                _extra["provider"] = provider
            if model is not None:
                _extra["model"] = model
            result = llm.generate(
                system=build_system_prompt(content),
                prompt=prompt,
                response_format=CARD_LIST_SCHEMA,
                max_tokens=GENERATION_MAX_TOKENS,
                **_extra,
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
    submitted_url: SubmittedURL,
    *,
    force: bool = False,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    stages: Optional[Sequence[Callable]] = None,
) -> GenerationResult:
    """Generate and persist cards for one ``SubmittedURL``.

    Saves transactionally: the full validated card list is built first, then
    written in one ``atomic`` block (with a ``bulk_create``) - a failure never
    leaves half-written cards.

    * ``force=False`` and the URL already has cards -> skipped.
    * ``force=True`` -> existing cards for the URL are deleted, then regenerated.
    * ``stages`` (optional) replaces the default post-generation pipeline
      (issue #130 injection seam): pass stubs or a subset to disable /
      replace stages without editing this function. ``None`` runs every
      default stage.

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

    raw_cards = _call_llm(submitted_url, provider=provider, model=model)
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

    # Post-generation work (image attachment) runs through the stage runner
    # (issue #130), so a stage can be disabled or replaced without editing
    # this function. Persistence above always runs first.
    post = post_generation.run_post_generation(submitted_url, cards, stages=stages)
    anki_duplicates = post.anki_duplicates
    anki_matches = post.anki_matches
    anki_warnings = post.anki_warnings

    counts = {
        Card.NoteType.BASIC.value: sum(
            1 for c in cards if c.note_type == Card.NoteType.BASIC
        ),
        Card.NoteType.CLOZE.value: sum(
            1 for c in cards if c.note_type == Card.NoteType.CLOZE
        ),
    }
    close_to_source = count_close_to_source(cards, content)
    if close_to_source:
        logger.warning(
            "%d cards close to source wording for %s",
            close_to_source,
            submitted_url.url,
        )
    return GenerationResult(
        outcome="created",
        counts=counts,
        rejected=rejected,
        close_to_source=close_to_source,
        anki_duplicates=anki_duplicates,
        anki_matches=anki_matches,
        anki_warnings=anki_warnings,
    )
