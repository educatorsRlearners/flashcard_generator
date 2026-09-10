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
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from submissions import dedup, images, llm
from submissions.models import Card, Feedback, SubmittedURL

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


# --- Few-shot section from stored feedback (issue #10) -----------------


def _format_feedback_example(fb: Feedback) -> str:
    lines = [f"- note_type: {fb.note_type}", f"  front: {fb.front}"]
    if fb.back:
        lines.append(f"  back: {fb.back}")
    if fb.decision == Feedback.Decision.REJECTED:
        lines.append(f"  reason: {fb.reason.strip() or '(no reason given)'}")
    return "\n".join(lines)


def _recent_feedback(decision: str) -> list[Feedback]:
    """Most-recent ``FEWSHOT_EXAMPLES_PER_CATEGORY`` rows for *decision*,
    returned oldest-first so the prompt string is deterministic."""
    recent = list(
        Feedback.objects.filter(decision=decision).order_by("-created_at", "-id")[
            :FEWSHOT_EXAMPLES_PER_CATEGORY
        ]
    )
    recent.reverse()
    return recent


# --- Smart few-shot selection (issue #25) ------------------------------
#
# Replaces the pure-recency pick above with a relevance-ranked,
# token-budgeted selection when a page text is available.
#
# EMBEDDING AVAILABILITY (checked 2026-09-10, recorded per the issue):
# ``submissions.llm`` (#5) exposes NO embedding method - ``Provider`` only
# has ``generate``/``check``. Per the issue constraints no new client and no
# new dependency are built here, so relevance uses an offline-safe,
# dependency-free default: token-overlap similarity between the feedback
# example text and the current page text (see ``_overlap_score``). A real
# embedding backend can be plugged in later via :func:`set_feedback_embed_fn`
# (or the ``FEWSHOT_EMBED_FN`` setting holding a dotted path) without
# changing the selection logic; while none is configured, generation uses
# the overlap default and NEVER crashes - unexpected failures degrade to
# the #10 recency cap (see ``select_feedback_for_page``).
#
# TOKEN COUNTING: ``estimate_tokens`` uses a documented approximation for the
# Anthropic Claude family (see ``FEWSHOT_CHARS_PER_TOKEN``): ~4 characters
# per token. The provider's exact tokenizer is server-side and would need a
# new dependency (tiktoken) to mirror locally, which AGENTS.md forbids
# adding without asking - so the budget check uses this approximation and
# says so. Tests stub the counter via :func:`set_token_counter`.
#
# CACHING / PERSISTENCE: the default overlap similarity needs no persisted
# vectors - the per-feedback word set is cached in-memory in
# ``_FEEDBACK_TOKEN_CACHE`` keyed by ``(id, front, back, reason)`` so a row
# is tokenised once per process (stale entries are impossible: the key
# includes the content). A plugged-in embedding backend's vectors are cached
# the same way per run in ``_EMBED_CACHE``. Re-embed path: content change
# naturally misses the cache; an embedding-model/config change calls
# :func:`clear_fewshot_cache`. No schema change (no migration) was needed.
#
# SETTINGS (all with documented defaults, see config/settings.py):
# * ``FEWSHOT_TOKEN_BUDGET`` (default 2000) - max tokens for the whole
#   few-shot section (both categories combined).
# * ``FEWSHOT_ACCEPTED_SHARE`` (default 0.5) - fraction of the budget
#   reserved for accepted examples; the rest goes to rejected ones, so one
#   category cannot crowd out the other.
# * ``FEWSHOT_SELECTION_MODE`` (default "relevance") - "relevance" for this
#   issue's ranking, "recency" to fall back to #10's most-recent behaviour.
# * ``FEWSHOT_MIN_FEEDBACK_CHARS`` (default 20, non-whitespace) - shorter
#   feedback is skipped gracefully (logged, not fatal).
# * ``FEWSHOT_CHARS_PER_TOKEN`` (default 4) - the documented approximation.

#: Total token budget for the few-shot section (both categories combined).
FEWSHOT_TOKEN_BUDGET: int = 2000

#: Share of ``FEWSHOT_TOKEN_BUDGET`` reserved for accepted examples
#: (``1 - share`` goes to rejected). Configurable via setting.
FEWSHOT_ACCEPTED_SHARE: float = 0.5

#: Default selection strategy. "relevance" ranks by similarity to the page;
#: "recency" reproduces #10 (most-recent N per category, oldest-first).
FEWSHOT_SELECTION_MODE: str = "relevance"

#: Feedback whose example text is shorter than this (non-whitespace chars)
#: is skipped gracefully (logged, next candidate used).
FEWSHOT_MIN_FEEDBACK_CHARS: int = 20

#: Documented token-count approximation for the Claude model family:
#: ~4 characters per token (Anthropic's rule of thumb). See
#: :func:`estimate_tokens`.
FEWSHOT_CHARS_PER_TOKEN: int = 4

#: Header lines of the few-shot section (shared by both selection paths).
_FEWSHOT_HEADER = (
    "Reviewer feedback on previously generated cards. Produce more cards "
    "like the accepted examples and avoid the problems in the rejected "
    "examples."
)


def fewshot_token_budget() -> int:
    """Configured total token budget (``FEWSHOT_TOKEN_BUDGET``)."""
    try:
        return max(0, int(getattr(settings, "FEWSHOT_TOKEN_BUDGET", FEWSHOT_TOKEN_BUDGET)))
    except (TypeError, ValueError):
        return FEWSHOT_TOKEN_BUDGET


def fewshot_accepted_share() -> float:
    """Configured accepted share of the budget (``FEWSHOT_ACCEPTED_SHARE``)."""
    try:
        share = float(getattr(settings, "FEWSHOT_ACCEPTED_SHARE", FEWSHOT_ACCEPTED_SHARE))
    except (TypeError, ValueError):
        return FEWSHOT_ACCEPTED_SHARE
    return min(1.0, max(0.0, share))


def fewshot_selection_mode() -> str:
    """Configured strategy: "relevance" (default) or "recency" (#10 fallback)."""
    mode = str(getattr(settings, "FEWSHOT_SELECTION_MODE", FEWSHOT_SELECTION_MODE) or "")
    return "recency" if mode.strip().lower() == "recency" else "relevance"


def fewshot_min_feedback_chars() -> int:
    """Configured minimum example length (``FEWSHOT_MIN_FEEDBACK_CHARS``)."""
    try:
        return max(
            0, int(getattr(settings, "FEWSHOT_MIN_FEEDBACK_CHARS", FEWSHOT_MIN_FEEDBACK_CHARS))
        )
    except (TypeError, ValueError):
        return FEWSHOT_MIN_FEEDBACK_CHARS


def fewshot_chars_per_token() -> int:
    """Configured chars-per-token approximation (``FEWSHOT_CHARS_PER_TOKEN``)."""
    try:
        value = int(getattr(settings, "FEWSHOT_CHARS_PER_TOKEN", FEWSHOT_CHARS_PER_TOKEN))
    except (TypeError, ValueError):
        return FEWSHOT_CHARS_PER_TOKEN
    return value if value > 0 else FEWSHOT_CHARS_PER_TOKEN


# -- Token counting (explicit, stubbed in tests) ----------------------

#: Override for :func:`estimate_tokens`, installed by
#: :func:`set_token_counter` (tests). ``None`` means "use the default".
_TOKEN_COUNTER = None


def set_token_counter(fn) -> None:
    """Install (or, with ``None``, remove) a custom token counter.

    The counter takes ``str`` and returns ``int``. Used by tests to stub
    token counting; production code uses the documented approximation.
    """
    global _TOKEN_COUNTER
    _TOKEN_COUNTER = fn


def estimate_tokens(text: str) -> int:
    """Token count for *text* used by the budget check.

    Default: ``ceil(len(text) / FEWSHOT_CHARS_PER_TOKEN)`` - the documented
    ~4-chars-per-token approximation for the Claude family (Anthropic's
    rule of thumb; exact counts are server-side). A custom counter
    installed via :func:`set_token_counter` takes precedence (tests).
    """
    if _TOKEN_COUNTER is not None:
        return max(0, int(_TOKEN_COUNTER(text or "")))
    if not text:
        return 0
    denom = fewshot_chars_per_token()
    return (len(text) + denom - 1) // denom


# -- Relevance scoring --------------------------------------------------

#: Pluggable embedding backend: ``fn(list[str]) -> list[list[float] | None]``,
#: installed by :func:`set_feedback_embed_fn` or the ``FEWSHOT_EMBED_FN``
#: setting (dotted path). ``None`` per text means "unembeddable - skip".
#: ``None`` globally means "use the offline token-overlap default".
_FEEDBACK_EMBED_FN = None

#: In-memory caches (see the module docstring above for the persistence
#: story). ``_FEEDBACK_TOKEN_CACHE`` maps
#: ``(feedback_id, front, back, reason)`` -> word set; ``_EMBED_CACHE``
#: maps text -> vector for a plugged-in backend, per run.
_FEEDBACK_TOKEN_CACHE: dict = {}
_EMBED_CACHE: dict = {}


def set_feedback_embed_fn(fn) -> None:
    """Install (or, with ``None``, remove) the embedding backend hook.

    ``fn`` takes a list of texts and returns a parallel list of vectors
    (``list[float]``) or ``None`` for texts that cannot be embedded. Any
    exception raised by ``fn`` is treated as "embeddings unavailable" and
    the selector degrades to the #10 recency cap instead of crashing.
    """
    global _FEEDBACK_EMBED_FN
    _FEEDBACK_EMBED_FN = fn


def get_feedback_embed_fn():
    """Return the configured embedding backend, if any.

    The in-memory hook from :func:`set_feedback_embed_fn` wins; otherwise
    the ``FEWSHOT_EMBED_FN`` setting (a dotted ``"pkg.mod:attr"``/``"pkg.mod.attr"``
    path) is imported lazily. ``None`` means "no backend - use the
    token-overlap default". Import/resolution failures return ``None`` (and
    are logged) so generation never crashes on a bad setting value.
    """
    if _FEEDBACK_EMBED_FN is not None:
        return _FEEDBACK_EMBED_FN
    path = getattr(settings, "FEWSHOT_EMBED_FN", None)
    if not path:
        return None
    try:
        import importlib

        module_path, _, attr = str(path).replace(":", ".").rpartition(".")
        module = importlib.import_module(module_path)
        fn = getattr(module, attr)
        return fn if callable(fn) else None
    except Exception:  # noqa: BLE001 - bad setting must degrade, not crash
        logger.warning("could not import FEWSHOT_EMBED_FN %r; using overlap default", path)
        return None


def clear_fewshot_cache() -> None:
    """Drop all in-memory few-shot caches (re-embed path).

    Call after changing the embedding model/config so every feedback row is
    re-embedded on next use. Content edits need no call - cache keys include
    the content itself.
    """
    _FEEDBACK_TOKEN_CACHE.clear()
    _EMBED_CACHE.clear()


def _feedback_example_text(fb: Feedback) -> str:
    """Text a feedback row is ranked by: card content + rejection reason."""
    parts = [fb.front or "", fb.back or ""]
    if fb.decision == Feedback.Decision.REJECTED and (fb.reason or "").strip():
        parts.append(fb.reason or "")
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


def _word_set(text: str) -> set:
    """Lowercased alphanumeric word tokens of *text* (punctuation-insensitive)."""
    return set(_WORD_RE.findall((text or "").lower()))


def _cached_word_set(fb: Feedback) -> set:
    """Word set for a feedback row, cached per ``(id, content)``."""
    key = (fb.pk, fb.front or "", fb.back or "", fb.reason or "")
    words = _FEEDBACK_TOKEN_CACHE.get(key)
    if words is None:
        words = _word_set(_feedback_example_text(fb))
        _FEEDBACK_TOKEN_CACHE[key] = words
    return words


def _overlap_score(fb_words: set, page_words: set) -> float:
    """Offline-safe relevance: |intersection| / |feedback words|.

    Asymmetric recall: a short example fully covered by the page scores 1.0
    even when the page is long. Empty feedback word set scores 0.0 (and such
    rows are normally skipped earlier as too short).
    """
    if not fb_words:
        return 0.0
    return len(fb_words & page_words) / len(fb_words)


def _cosine(a: list, b: list) -> float:
    """Cosine similarity of two vectors (0.0 when either is degenerate)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _ranked_for_decision(
    decision: str, page_text: str, budget_tokens: int
) -> list[Feedback]:
    """Top-ranked ``Feedback`` rows for *decision* within *budget_tokens*.

    Candidates are every stored row for the decision, scored against
    *page_text* (plugged-in embedding backend when configured, else the
    token-overlap default), sorted by ``(-score, created_at, id)`` for a
    documented deterministic order, then taken in rank order until the next
    example would exceed the category budget. Short (``< min chars``) and
    unembeddable rows are skipped gracefully (logged, next candidate used).
    A tiny set that fits simply injects all of it - low scores never drop
    usable examples.
    """
    if budget_tokens <= 0:
        return []
    rows = list(Feedback.objects.filter(decision=decision).order_by("created_at", "id"))
    if not rows:
        return []
    min_chars = fewshot_min_feedback_chars()
    page_words = _word_set(page_text or "")
    embed_fn = get_feedback_embed_fn()

    scored: list[tuple[float, Feedback]] = []
    if embed_fn is not None:
        # One batched call (page + all candidates); per-row None = skip.
        try:
            texts = [page_text or ""] + [_feedback_example_text(fb) for fb in rows]
            vectors: list = []
            for text in texts:
                if text in _EMBED_CACHE:
                    vectors.append(_EMBED_CACHE[text])
                else:
                    vectors.append(None)  # filled by the single batch call below
            if any(v is None for v in vectors):
                fresh_texts = [t for t, v in zip(texts, vectors) if v is None]
                fresh = embed_fn(fresh_texts)
                if fresh is None or len(fresh) != len(fresh_texts):
                    raise ValueError("embedding backend returned a bad batch")
                for text, vec in zip(fresh_texts, fresh):
                    _EMBED_CACHE[text] = vec
                vectors = [_EMBED_CACHE[t] for t in texts]
            page_vec = vectors[0]
            for fb, vec in zip(rows, vectors[1:]):
                example_text = _feedback_example_text(fb)
                if _non_whitespace_len(example_text) < min_chars:
                    logger.debug("skipping feedback %s: example text too short", fb.pk)
                    continue
                if page_vec is None or vec is None:
                    logger.debug("skipping feedback %s: embedding unavailable", fb.pk)
                    continue
                scored.append((_cosine(page_vec, vec), fb))
        except Exception:  # noqa: BLE001 - degrade to recency, never crash
            logger.warning("feedback embedding failed; degrading to recency selection",
                           exc_info=True)
            return _budgeted_recency(decision, budget_tokens)
    else:
        for fb in rows:
            example_text = _feedback_example_text(fb)
            if _non_whitespace_len(example_text) < min_chars:
                logger.debug("skipping feedback %s: example text too short", fb.pk)
                continue
            scored.append((_overlap_score(_cached_word_set(fb), page_words), fb))

    # Deterministic rank: best score first; ties -> oldest row first, then id.
    scored.sort(key=lambda item: (-item[0], item[1].created_at, item[1].pk or 0))

    picked: list[Feedback] = []
    used = 0
    for _, fb in scored:
        cost = estimate_tokens(_format_feedback_example(fb))
        if used + cost > budget_tokens:
            continue  # too big now; a later (shorter) example may still fit
        picked.append(fb)
        used += cost
    return picked


def _budgeted_recency(decision: str, budget_tokens: int) -> list[Feedback]:
    """Most-recent rows for *decision* (oldest-first), within *budget_tokens*."""
    recent = list(
        Feedback.objects.filter(decision=decision).order_by("-created_at", "-id")[
            :FEWSHOT_EXAMPLES_PER_CATEGORY
        ]
    )
    recent.reverse()
    picked: list[Feedback] = []
    used = 0
    for fb in recent:
        cost = estimate_tokens(_format_feedback_example(fb))
        if used + cost > budget_tokens:
            continue
        picked.append(fb)
        used += cost
    return picked


def select_feedback_for_page(
    page_text: str | None = None, *, embed_fn=None,
) -> tuple[list[Feedback], list[Feedback]]:
    """Select ``(accepted, rejected)`` few-shot examples for *page_text*.

    * Cold start (zero feedback): returns ``([], [])`` with NO embedding
      call (the count check short-circuits first).
    * ``FEWSHOT_SELECTION_MODE == "recency"`` (or empty *page_text*):
      #10's most-recent-N-per-category pick, additionally budget-capped.
    * Otherwise: relevance-ranked pick per category (see
      :func:`_ranked_for_decision`), with the total budget split
      ``FEWSHOT_ACCEPTED_SHARE`` / remainder so one category cannot crowd
      out the other. Only-accepted / only-rejected stores yield whatever
      exists. Any embedding failure degrades to the recency cap (logged),
      never a crash.
    * *embed_fn* (optional) temporarily overrides the configured backend
      for one call - tests use it to stub embeddings offline.
    """
    if not Feedback.objects.exists():
        return [], []
    if embed_fn is not None:
        previous = _FEEDBACK_EMBED_FN
        set_feedback_embed_fn(embed_fn)
        try:
            return select_feedback_for_page(page_text)
        finally:
            set_feedback_embed_fn(previous)
    try:
        total = fewshot_token_budget()
        if total <= 0:
            return [], []
        share = fewshot_accepted_share()
        accepted_budget = int(round(total * share))
        rejected_budget = total - accepted_budget
        if not (page_text or "").strip() or fewshot_selection_mode() == "recency":
            return (
                _budgeted_recency(Feedback.Decision.ACCEPTED, accepted_budget),
                _budgeted_recency(Feedback.Decision.REJECTED, rejected_budget),
            )
        return (
            _ranked_for_decision(Feedback.Decision.ACCEPTED, page_text or "", accepted_budget),
            _ranked_for_decision(Feedback.Decision.REJECTED, page_text or "", rejected_budget),
        )
    except Exception:  # noqa: BLE001 - selection must never break generation
        logger.warning("smart few-shot selection failed; degrading to recency",
                       exc_info=True)
        total = FEWSHOT_TOKEN_BUDGET
        half = total // 2
        return (
            _budgeted_recency(Feedback.Decision.ACCEPTED, half),
            _budgeted_recency(Feedback.Decision.REJECTED, total - half),
        )


def _render_fewshot_section(
    accepted: list[Feedback], rejected: list[Feedback]
) -> str:
    """Render the picked examples as the prompt section ("" when empty)."""
    if not accepted and not rejected:
        return ""
    out = ["", _FEWSHOT_HEADER]
    if accepted:
        out.append("")
        out.append("Accepted examples:")
        out.extend(_format_feedback_example(fb) for fb in accepted)
    if rejected:
        out.append("")
        out.append("Rejected examples:")
        out.extend(_format_feedback_example(fb) for fb in rejected)
    return "\n".join(out) + "\n"


def build_fewshot_section(page_text: str | None = None) -> str:
    """Build the few-shot block injected into the generation system prompt.

    With *page_text* (and the default ``FEWSHOT_SELECTION_MODE``), examples
    are relevance-ranked within a token budget (issue #25); without it - or
    with mode ``"recency"`` - this is #10's most-recent-N-per-category pick
    (oldest-first). Returns ``""`` when no feedback exists (no error, and no
    embedding call is made on the cold-start path).
    """
    accepted, rejected = select_feedback_for_page(page_text)
    return _render_fewshot_section(accepted, rejected)


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
    """
    language = analogy_language()
    base = SYSTEM_PROMPT
    if language.lower() != "python":
        base = base.replace(
            "When you use a programming analogy, use python.",
            f"When you use a programming analogy, use {language}.",
        )
    return base + build_fewshot_section(page_text)


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
        # Attribute the call for LLM observability (#28): the client reads
        # this context when it records its LLMCall row. A context manager
        # (rather than new kwargs) keeps this call signature unchanged.
        with llm.call_context(
            batch=submitted_url.batch, submitted_url=submitted_url
        ):
            result = llm.generate(
                system=build_system_prompt(content),
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

    # Final step: local semantic dedup (#7). A missing embedding model must
    # not fail generation - the standalone ``dedup_cards`` command is the
    # place that hard-fails and tells the engineer to run the download.
    try:
        dedup.dedup_cards(cards)
    except dedup.ModelLoadError as exc:
        logger.warning(
            "skipped post-generation dedup for %s: %s", submitted_url.url, exc
        )
    except Exception:  # noqa: BLE001 - dedup is best-effort here
        logger.exception("post-generation dedup failed for %s", submitted_url.url)

    # Live-deck semantic dedup (#29): compare the new cards against the notes
    # currently in the target Anki deck. Best-effort like local dedup - an
    # unreachable Anki falls back to local-only; the returned
    # ``AnkiDedupResult`` is stored on the ``GenerationResult`` (never
    # discarded) so the command output surfaces the matched note / warning.
    # Generation always completes.
    anki_duplicates = 0
    anki_matches: list = []
    anki_warnings: list[str] = []
    try:
        from submissions import anki as _anki

        anki_result = _anki.dedup_cards_against_anki(cards)
        anki_duplicates = int(anki_result.duplicates or 0)
        anki_matches = [
            {
                "note_id": m.note_id,
                "note_text": m.note_text,
                "similarity": m.similarity,
            }
            for m in (anki_result.matches or [])
        ]
        if getattr(anki_result, "warning", ""):
            anki_warnings.append(anki_result.warning)
    except Exception as exc:  # noqa: BLE001 - live-deck dedup is best-effort here
        logger.exception("live-deck dedup failed for %s", submitted_url.url)
        anki_warnings.append(
            f"Anki deck dedup skipped (live-deck dedup failed: {exc}); "
            "local-only dedup applied."
        )

    # Per-card images (#12): source-page image first, Draw Things fallback,
    # otherwise no image. Best-effort - image trouble (unreachable Draw
    # Things, a failed fetch) never fails or aborts card generation.
    try:
        images.attach_images(submitted_url, cards)
    except Exception:  # noqa: BLE001 - images are strictly best-effort
        logger.exception("image attachment failed for %s", submitted_url.url)

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
