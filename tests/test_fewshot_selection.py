"""Tests for smart few-shot feedback selection (issue #25).

Relevance-ranked, token-budgeted selection of stored feedback examples for
the current page text. Everything here is offline and deterministic: the
default similarity is a local token-overlap score (no network), and tests
stub the embedding hook (:func:`generation.set_feedback_embed_fn`) and the
token counter (:func:`generation.set_token_counter`) where they need to
control those seams.
"""

import pytest

from submissions import generation
from submissions.generation import (
    build_fewshot_section,
    estimate_tokens,
    select_feedback_for_page,
)
from submissions.models import Feedback

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_fewshot_hooks():
    generation.set_feedback_embed_fn(None)
    generation.set_token_counter(None)
    generation.clear_fewshot_cache()
    yield
    generation.set_feedback_embed_fn(None)
    generation.set_token_counter(None)
    generation.clear_fewshot_cache()


def _fb(decision, front, back="Some supporting definition text here.", reason=""):
    return Feedback.objects.create(
        note_type="basic",
        front=front,
        back=back,
        decision=decision,
        reason=reason,
    )


ACC = Feedback.Decision.ACCEPTED
REJ = Feedback.Decision.REJECTED

PHOTOSYNTHESIS_PAGE = (
    "Photosynthesis in green plants converts sunlight, water and carbon "
    "dioxide into glucose. Chlorophyll pigments capture light energy in "
    "the chloroplasts of leaf cells."
)


# --- relevance ordering ------------------------------------------------


def test_topical_match_outranks_unrelated_example():
    _fb(ACC, "Unrelated accepted card about maritime shipping routes and cargo.")
    topical = _fb(
        ACC,
        "Accepted card about photosynthesis and chlorophyll capturing sunlight.",
    )
    accepted, _ = select_feedback_for_page(PHOTOSYNTHESIS_PAGE)
    assert [fb.pk for fb in accepted][0] == topical.pk


def test_rejected_ranking_is_independent_per_category():
    _fb(REJ, "Rejected card about maritime shipping routes and cargo.", reason="vague wording here")
    topical_rej = _fb(
        REJ,
        "Rejected card about photosynthesis and chlorophyll pigments.",
        reason="copied the page sentence verbatim here",
    )
    _, rejected = select_feedback_for_page(PHOTOSYNTHESIS_PAGE)
    assert [fb.pk for fb in rejected][0] == topical_rej.pk


def test_stub_embedding_backend_drives_ranking():
    # Stub: first candidate close to page vector, second far away.
    first = _fb(ACC, "Accepted candidate one with plenty of words here.")
    second = _fb(ACC, "Accepted candidate two with plenty of words here.")

    def fake_embed(texts):
        page, *rest = texts
        assert page == PHOTOSYNTHESIS_PAGE
        assert len(rest) == 2
        return [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]

    accepted, _ = select_feedback_for_page(PHOTOSYNTHESIS_PAGE, embed_fn=fake_embed)
    assert [fb.pk for fb in accepted] == [first.pk, second.pk]


def test_unembeddable_rows_skipped_gracefully():
    good = _fb(ACC, "Accepted candidate with plenty of descriptive words here.")
    _fb(ACC, "Accepted candidate that cannot be embedded at all here.")

    def partial_embed(texts):
        # Page embeds fine; second candidate returns None (unembeddable).
        vectors = []
        for i, _ in enumerate(texts):
            vectors.append([1.0, 0.0] if i != 2 else None)
        return vectors

    accepted, _ = select_feedback_for_page(PHOTOSYNTHESIS_PAGE, embed_fn=partial_embed)
    assert [fb.pk for fb in accepted] == [good.pk]


def test_failing_backend_degrades_to_recency_without_crashing():
    old = _fb(ACC, "Older accepted candidate with plenty of words here.")
    new = _fb(ACC, "Newer accepted candidate with plenty of words here.")

    def broken_embed(texts):
        raise RuntimeError("embedding service down")

    accepted, _ = select_feedback_for_page(PHOTOSYNTHESIS_PAGE, embed_fn=broken_embed)
    # Recency cap: most-recent first in rank terms -> newest is last/only-ordered
    # oldest-first; both fit so both present, newest last.
    assert [fb.pk for fb in accepted] == [old.pk, new.pk]


# --- token budget + split -----------------------------------------------


def test_token_budget_never_exceeded(settings):
    settings.FEWSHOT_TOKEN_BUDGET = 30
    generation.set_token_counter(lambda text: 10)
    for i in range(6):
        _fb(ACC, f"Accepted candidate number {i} with enough words.")
        _fb(REJ, f"Rejected candidate number {i} with enough words.", reason="too vague here")

    section = build_fewshot_section(PHOTOSYNTHESIS_PAGE)
    assert estimate_tokens(section) <= 30


def test_accepted_rejected_split_is_honoured(settings):
    # Budget 40, share 0.5 -> 20 tokens per side; 10 tokens per example
    # -> exactly 2 accepted + 2 rejected even though many more exist.
    settings.FEWSHOT_TOKEN_BUDGET = 40
    settings.FEWSHOT_ACCEPTED_SHARE = 0.5
    generation.set_token_counter(lambda text: 10)
    for i in range(6):
        _fb(ACC, f"Accepted candidate number {i} with enough words.")
        _fb(REJ, f"Rejected candidate number {i} with enough words.", reason="too vague here")

    accepted, rejected = select_feedback_for_page(PHOTOSYNTHESIS_PAGE)
    assert len(accepted) == 2
    assert len(rejected) == 2
    section = build_fewshot_section(PHOTOSYNTHESIS_PAGE)
    assert "Accepted examples:" in section
    assert "Rejected examples:" in section


def test_share_is_configurable(settings):
    settings.FEWSHOT_TOKEN_BUDGET = 40
    settings.FEWSHOT_ACCEPTED_SHARE = 0.75  # 30 accepted / 10 rejected
    generation.set_token_counter(lambda text: 10)
    for i in range(6):
        _fb(ACC, f"Accepted candidate number {i} with enough words.")
        _fb(REJ, f"Rejected candidate number {i} with enough words.", reason="too vague here")

    accepted, rejected = select_feedback_for_page(PHOTOSYNTHESIS_PAGE)
    assert len(accepted) == 3
    assert len(rejected) == 1


# --- cold start / edge cases ----------------------------------------------


def test_cold_start_empty_section_and_no_embedding_call():
    calls = []

    def spy_embed(texts):
        calls.append(texts)
        return [[1.0]] * len(texts)

    generation.set_feedback_embed_fn(spy_embed)
    assert build_fewshot_section(PHOTOSYNTHESIS_PAGE) == ""
    assert calls == []
    accepted, rejected = select_feedback_for_page(PHOTOSYNTHESIS_PAGE)
    assert (accepted, rejected) == ([], [])


def test_only_accepted_or_only_rejected_works():
    _fb(ACC, "Only accepted candidate with plenty of words here.")
    section = build_fewshot_section(PHOTOSYNTHESIS_PAGE)
    assert "Accepted examples:" in section
    assert "Rejected examples:" not in section

    Feedback.objects.all().delete()
    generation.clear_fewshot_cache()
    _fb(REJ, "Only rejected candidate with plenty of words here.", reason="vague text here")
    section = build_fewshot_section(PHOTOSYNTHESIS_PAGE)
    assert "Rejected examples:" in section
    assert "Accepted examples:" not in section


def test_short_feedback_skipped_gracefully(settings):
    settings.FEWSHOT_MIN_FEEDBACK_CHARS = 20
    _fb(ACC, "ok", back="")
    good = _fb(ACC, "A properly long accepted candidate with many words.")
    accepted, _ = select_feedback_for_page(PHOTOSYNTHESIS_PAGE)
    assert [fb.pk for fb in accepted] == [good.pk]


def test_tiny_set_injects_everything_despite_low_scores():
    acc = _fb(ACC, "Accepted candidate about deep sea anglerfish biology.")
    rej = _fb(
        REJ,
        "Rejected candidate about deep sea anglerfish biology.",
        reason="unrelated rejection reason words",
    )
    accepted, rejected = select_feedback_for_page(PHOTOSYNTHESIS_PAGE)
    assert [fb.pk for fb in accepted] == [acc.pk]
    assert [fb.pk for fb in rejected] == [rej.pk]


# --- determinism + recency fallback -----------------------------------------


def test_determinism_across_runs():
    for i in range(5):
        _fb(ACC, f"Accepted candidate number {i} about light and leaves.")
        _fb(REJ, f"Rejected candidate number {i} about light and leaves.", reason="reason text here")
    first = build_fewshot_section(PHOTOSYNTHESIS_PAGE)
    generation.clear_fewshot_cache()
    second = build_fewshot_section(PHOTOSYNTHESIS_PAGE)
    assert first == second
    assert first != ""


def test_tiebreak_is_stable_oldest_first():
    _fb(ACC, "Identical accepted candidate words repeated here.")
    _fb(ACC, "Identical accepted candidate words repeated here.")
    accepted, _ = select_feedback_for_page("totally unrelated page about plumbing valves")
    # Equal scores -> oldest (lowest id) first.
    assert [fb.pk for fb in accepted] == sorted(fb.pk for fb in accepted)


def test_recency_fallback_flag_ignores_relevance(settings):
    settings.FEWSHOT_SELECTION_MODE = "recency"
    old_topical = _fb(ACC, "Old accepted card about photosynthesis chlorophyll light.")
    for i in range(generation.FEWSHOT_EXAMPLES_PER_CATEGORY):
        _fb(ACC, f"Newer accepted card {i} about shipping cargo routes.")
    accepted, _ = select_feedback_for_page(PHOTOSYNTHESIS_PAGE)
    assert old_topical.pk not in [fb.pk for fb in accepted]
    assert len(accepted) == generation.FEWSHOT_EXAMPLES_PER_CATEGORY


def test_no_page_text_keeps_recency_behavior():
    old = _fb(ACC, "Older accepted candidate with plenty of words here.")
    new = _fb(ACC, "Newer accepted candidate with plenty of words here.")
    accepted, _ = select_feedback_for_page(None)
    assert [fb.pk for fb in accepted] == [old.pk, new.pk]
