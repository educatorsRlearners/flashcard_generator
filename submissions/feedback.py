"""Durable review feedback + few-shot selection (issue #131).

Owns all three feedback responsibilities so ``submissions/generation.py``
only composes the system prompt and ``submissions/views.py`` only records
the review decision:

* :func:`record_feedback` - snapshot storage of an accept/reject decision.
* :func:`select_feedback_for_page` - relevance-ranked, token-budgeted pick
  (or the #10 recency pick when no page text / recency mode / disabled).
* :func:`render_fewshot_section` / :func:`build_fewshot_section` -
  rendering of the picked examples as the prompt section.

Moved verbatim out of ``submissions/generation.py`` (issues #10, #25):
prompt strings, example order, ranking, budget, stored fields and the
broad except-degrade are byte-identical to the previous inline code.
Pure move - any behavior tweak belongs in a new issue.

Edge cases preserved visibly here:

* ``undecided`` decisions are never recorded (see :func:`record_feedback`).
* Accepted rows store a blank reason; rejected rows with a blank reason
  render as ``(no reason given)`` (see :func:`_format_feedback_example`).
* Feedback shorter than ``FEWSHOT_MIN_FEEDBACK_CHARS`` non-whitespace chars
  is skipped without error (see :func:`_ranked_for_decision`).

Snapshot semantics (see :func:`record_feedback`): the stored row copies the
card's current fields at decision time (so inline edits from #24 are
captured), sets ``was_edited`` from ``card.is_edited``, and falls back to
``""`` when ``card.tags["source_url"]`` is missing. Snapshots hold no FK
to ``Card``/``Batch`` (see ``submissions/models.py`` ``Feedback``), so they
survive batch/card deletion.

Selection failure (bad ``FEWSHOT_EMBED_FN`` backend, bad mode value,
scoring/token-counter exception) degrades to the recency pick and never
raises out of prompt building, matching the previous broad except-degrade.

In-memory caches (``_FEEDBACK_TOKEN_CACHE``, ``_EMBED_CACHE``,
``set_feedback_embed_fn``, ``set_token_counter``, ``clear_fewshot_cache``)
moved with the selection code; a content change still naturally misses the
cache and no migration is added.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings

from submissions.models import Card, Feedback

logger = logging.getLogger(__name__)

# --- Named constants (no magic literals in the logic below) -------------

#: Few-shot cap (issue #10). The generation prompt embeds at most this many
#: most-recent *accepted* ``Feedback`` rows and, separately, at most this many
#: most-recent *rejected* rows - so the few-shot section never grows past
#: ``2 * FEWSHOT_EXAMPLES_PER_CATEGORY`` examples no matter how much feedback
#: accumulates. Selection is "most recent N per category" by timestamp;
#: within the section the examples are ordered oldest-first for a stable,
#: deterministic prompt string.
FEWSHOT_EXAMPLES_PER_CATEGORY: int = 3

# --- Smart few-shot selection (issue #25) ------------------------------
#
# Relevance-ranked, token-budgeted selection when a page text is available.
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
# * ``FEWSHOT_ENABLED`` (default True) - master switch; when disabled no
#   ``Feedback`` DB query and no embedding/similarity call is made.
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

_WORD_RE = re.compile(r"[a-z0-9]+")


def _non_whitespace_len(text: str) -> int:
    return len("".join(text.split()))


def fewshot_enabled() -> bool:
    """Master switch for the few-shot section (``FEWSHOT_ENABLED``).

    Default True. Accepts ``True``/``False`` booleans as well as common
    string forms (``"0"``/``"false"``/``"no"``/``"off"`` disable). When
    disabled, selection returns ``([], [])`` and rendering returns ``""``
    without any ``Feedback`` DB query or embedding/similarity call.
    """
    raw = getattr(settings, "FEWSHOT_ENABLED", True)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True
    text = str(raw).strip().lower()
    return text not in ("0", "false", "no", "off", "")


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


# -- Storage ------------------------------------------------------------


def record_feedback(card: Card, decision: str) -> Feedback | None:
    """Persist a durable snapshot of one accept/reject decision.

    Only ``"accepted"`` / ``"rejected"`` are recorded - ``"undecided"`` (or
    any other value) returns ``None`` and writes nothing. The snapshot
    copies the card's *current* fields (so #24 inline edits are captured),
    sets ``was_edited`` from ``card.is_edited``, falls back to ``""`` when
    ``card.tags["source_url"]`` is missing, and always stores a blank
    ``reason`` for acceptances (the rejection reason lives on the card for
    rejections).
    """
    if decision not in (Card.ReviewStatus.ACCEPTED, Card.ReviewStatus.REJECTED):
        return None
    tags = card.tags if isinstance(card.tags, dict) else {}
    reason = "" if decision == Card.ReviewStatus.ACCEPTED else (card.rejection_reason or "")
    return Feedback.objects.create(
        note_type=card.note_type,
        front=card.front,
        back=card.back,
        source_url=tags.get("source_url", "") or "",
        decision=decision,
        reason=reason,
        was_edited=card.is_edited,
    )


# -- Formatting / selection / rendering ---------------------------------


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

    * Disabled (``FEWSHOT_ENABLED=0``) or zero token budget: returns
      ``([], [])`` with NO ``Feedback`` DB query and NO embedding call.
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
    if not fewshot_enabled():
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
        if not Feedback.objects.exists():
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


def render_fewshot_section(
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


# Backwards-compatible private alias (previous location in generation.py).
_render_fewshot_section = render_fewshot_section


def build_fewshot_section(page_text: str | None = None) -> str:
    """Build the few-shot block injected into the generation system prompt.

    With *page_text* (and the default ``FEWSHOT_SELECTION_MODE``), examples
    are relevance-ranked within a token budget (issue #25); without it - or
    with mode ``"recency"`` - this is #10's most-recent-N-per-category pick
    (oldest-first). Returns ``""`` when few-shot is disabled or no feedback
    exists (no error, and no embedding call is made on the cold-start or
    disabled paths).
    """
    accepted, rejected = select_feedback_for_page(page_text)
    return render_fewshot_section(accepted, rejected)


#: One-line composition alias used by ``generation.build_system_prompt``.
render_section = build_fewshot_section
