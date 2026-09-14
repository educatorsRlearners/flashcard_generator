"""Tests for the post-generation stage runner (issue #130).

``generation.generate_for`` persists cards first and then delegates local
semantic dedup (plus ``dedup_ready`` bookkeeping), live-deck Anki dedup and
image attachment to ``submissions.post_generation.run_post_generation``.
These tests pin the moved behavior: identical log messages, identical
``GenerationResult`` fields, unchanged persistence ordering, and the two
seams for disabling/replacing dedup without editing ``generation.py``.
"""

import inspect
import logging

import pytest

from submissions import dedup, generation, llm, post_generation
from submissions.models import Batch, BatchRequest, Card, SubmittedURL

pytestmark = pytest.mark.django_db


# --- fixtures / helpers ------------------------------------------------

RICH_TEXT = (
    "Photosynthesis is the process by which green plants convert light energy "
    "into chemical energy. Chlorophyll is the pigment that absorbs light in "
    "the chloroplast. The Calvin cycle fixes carbon dioxide into glucose. "
    "Stomata are pores in the leaf epidermis that regulate gas exchange. "
) * 6


def _make_url(url="https://example.com/bio", *, batch=None, text=RICH_TEXT, **kw):
    opts = dict(
        status=SubmittedURL.Status.OK,
        extraction_method=SubmittedURL.ExtractionMethod.STATIC,
        extracted_text=text,
        extracted_title="Biology notes",
        batch=batch,
    )
    opts.update(kw)
    su = SubmittedURL.objects.create(url=url, **opts)
    if batch is not None:
        BatchRequest.objects.create(batch=batch, submitted_url=su)
    return su


def _card(note_type="basic", front="What is X?", back="X is a thing.",
          source_term="X", topic="biology"):
    return dict(
        note_type=note_type, front=front, back=back,
        source_term=source_term, topic=topic,
    )


def _llm_result(cards):
    import json

    payload = {"cards": cards}
    return llm.LLMResult(text=json.dumps(payload), parsed=payload)


@pytest.fixture
def install_llm(monkeypatch):
    def _install(cards):
        monkeypatch.setattr(
            generation.llm, "generate",
            lambda **kw: _llm_result(cards),
        )

    return _install


@pytest.fixture
def stub_anki(monkeypatch):
    """Isolate from live Anki; returns the installed fake for inspection."""
    from submissions import anki as _anki

    calls = {}

    def _install(fake=None):
        def default(cards, deck_name=None, *a, **k):
            calls["deck_name"] = deck_name
            return _anki.AnkiDedupResult()

        monkeypatch.setattr(
            _anki, "dedup_cards_against_anki", fake or default
        )
        return calls

    return _install


@pytest.fixture
def stub_dedup(monkeypatch):
    """Replace local dedup with a no-op (real one needs the embed model)."""
    monkeypatch.setattr(dedup, "dedup_cards", lambda cards: None)


# --- the DEDUP_ENABLED seam --------------------------------------------


def test_dedup_enabled_defaults_on_and_parses_string_forms(settings):
    assert post_generation.dedup_enabled() is True

    settings.DEDUP_ENABLED = False
    assert post_generation.dedup_enabled() is False
    settings.DEDUP_ENABLED = True
    assert post_generation.dedup_enabled() is True

    for raw in ("0", "false", "no", "off", "", "  False  "):
        settings.DEDUP_ENABLED = raw
        assert post_generation.dedup_enabled() is False, raw
    for raw in ("1", "true", "yes", "on"):
        settings.DEDUP_ENABLED = raw
        assert post_generation.dedup_enabled() is True, raw


def test_disabled_dedup_still_creates_persists_and_marks_ready(
    install_llm, stub_anki, settings, monkeypatch
):
    """With dedup disabled, generation still returns created, persists the
    cards, never calls the embedding path, and still sets dedup_ready."""
    settings.DEDUP_ENABLED = False
    stub_anki()
    install_llm([_card()])

    def boom(cards):
        raise AssertionError("dedup_cards must not run while disabled")

    monkeypatch.setattr(dedup, "dedup_cards", boom)
    su = _make_url()

    result = generation.generate_for(su)

    assert result.outcome == "created"
    assert su.cards.count() == 1
    su.refresh_from_db()
    assert su.dedup_ready is True


# --- the injected-stages seam ------------------------------------------


def test_default_stages_run_in_generation_order():
    assert post_generation.DEFAULT_STAGES == (
        post_generation.local_dedup_stage,
        post_generation.anki_live_deck_stage,
        post_generation.image_attachment_stage,
    )


def test_injected_stages_replace_the_pipeline(
    install_llm, stub_anki, monkeypatch
):
    """A caller-supplied stage list runs instead of the defaults, with no
    edit to generation code and no local-dedup call."""
    stub_anki()
    install_llm([_card()])
    su = _make_url()

    calls = []

    def stub_stage(submitted_url, cards, outcome):
        calls.append("stub")
        outcome.anki_warnings.append("stubbed")

    def boom(cards):
        raise AssertionError("default dedup stage must not run")

    monkeypatch.setattr(dedup, "dedup_cards", boom)

    result = generation.generate_for(su, stages=[stub_stage])

    assert calls == ["stub"]
    assert result.outcome == "created"
    assert result.anki_warnings == ["stubbed"]
    assert su.cards.count() == 1


def test_runner_runs_stages_in_order():
    order = []
    su_like = type("SU", (), {"url": "https://example.com/x", "pk": 1})()

    def first(su, cards, outcome):
        order.append("first")

    def second(su, cards, outcome):
        order.append("second")

    post_generation.run_post_generation(su_like, [], stages=[first, second])

    assert order == ["first", "second"]


# --- generate_for no longer calls the stages directly ------------------


def test_generate_for_calls_only_the_stage_runner():
    """Mirrors the issue's rg check: no direct stage calls inside the
    generation entry point."""
    body = inspect.getsource(generation.generate_for)
    assert "dedup." not in body
    assert "anki." not in body
    assert "images." not in body
    assert "run_post_generation" in body


def test_mark_dedup_ready_alias_survives():
    assert generation._mark_dedup_ready is post_generation._mark_dedup_ready


# --- preserved best-effort paths ----------------------------------------


def test_model_load_error_warns_and_marks_ready(
    install_llm, stub_anki, stub_dedup, monkeypatch, caplog
):
    from submissions import dedup as _dedup

    stub_anki()
    install_llm([_card()])
    su = _make_url()

    def raising(cards):
        raise _dedup.ModelLoadError("model not downloaded")

    monkeypatch.setattr(_dedup, "dedup_cards", raising)

    with caplog.at_level(logging.WARNING, logger="submissions.post_generation"):
        result = generation.generate_for(su)

    assert result.outcome == "created"
    assert "skipped post-generation dedup" in caplog.text
    su.refresh_from_db()
    assert su.dedup_ready is True


def test_generic_dedup_error_still_runs_anki_and_image_stages(
    install_llm, stub_anki, monkeypatch, caplog
):
    """A local-dedup exception is logged, dedup_ready still flips, and the
    later stages still run (generation still returns created)."""
    from submissions import anki as _anki
    from submissions import dedup as _dedup
    from submissions import images as _images

    ran = []

    def raising_dedup(cards):
        raise RuntimeError("boom")

    def recording_anki(cards, deck_name=None, *a, **k):
        ran.append("anki")
        return _anki.AnkiDedupResult()

    def recording_images(submitted_url, cards):
        ran.append("images")

    monkeypatch.setattr(_dedup, "dedup_cards", raising_dedup)
    monkeypatch.setattr(_anki, "dedup_cards_against_anki", recording_anki)
    monkeypatch.setattr(_images, "attach_images", recording_images)
    install_llm([_card()])
    su = _make_url()

    with caplog.at_level(logging.WARNING, logger="submissions.post_generation"):
        result = generation.generate_for(su)

    assert result.outcome == "created"
    assert "post-generation dedup failed" in caplog.text
    assert ran == ["anki", "images"]
    su.refresh_from_db()
    assert su.dedup_ready is True


def test_anki_stage_uses_batch_deck_and_reports_unreachable(
    install_llm, stub_dedup, monkeypatch
):
    """An unreachable Anki appends the live-deck warning and generation
    still completes; the stored deck (not the default) is compared."""
    from submissions import anki as _anki

    install_llm([_card()])
    batch = Batch.objects.create(deck_name="Batch Deck")

    seen = {}

    def raising_anki(cards, deck_name=None, *a, **k):
        seen["deck_name"] = deck_name
        raise _anki.AnkiError("boom")

    monkeypatch.setattr(_anki, "dedup_cards_against_anki", raising_anki)
    su = _make_url(batch=batch)

    result = generation.generate_for(su)

    assert result.outcome == "created"
    assert seen["deck_name"] == "Batch Deck"
    assert len(result.anki_warnings) == 1
    assert result.anki_warnings[0].startswith(
        "Anki deck dedup skipped (live-deck dedup failed:"
    )


def test_image_failure_is_logged_and_generation_completes(
    install_llm, stub_anki, stub_dedup, monkeypatch, caplog
):
    from submissions import images as _images

    stub_anki()
    install_llm([_card()])
    su = _make_url()

    def raising_images(submitted_url, cards):
        raise RuntimeError("draw things down")

    monkeypatch.setattr(_images, "attach_images", raising_images)

    with caplog.at_level(logging.WARNING, logger="submissions.post_generation"):
        result = generation.generate_for(su)

    assert result.outcome == "created"
    assert "image attachment failed" in caplog.text
    assert su.cards.count() == 1


# --- unchanged persistence ordering + ready protocol --------------------


def test_persistence_happens_before_any_stage_runs(
    install_llm, stub_anki, monkeypatch
):
    """bulk_create + the generation-OK mark (which resets dedup_ready to
    False) happen before the first stage runs."""
    stub_anki()
    install_llm([_card(), _card(front="What is Y?", source_term="Y")])
    su = _make_url()

    observed = {}

    def blocking_dedup(cards):
        observed["persisted"] = su.cards.count()
        su.refresh_from_db()
        observed["generation_status"] = su.generation_status
        observed["dedup_ready_during_stage"] = su.dedup_ready

    monkeypatch.setattr(dedup, "dedup_cards", blocking_dedup)

    generation.generate_for(su)

    assert observed["persisted"] == 2
    assert observed["generation_status"] == SubmittedURL.GenerationStatus.OK
    assert observed["dedup_ready_during_stage"] is False
    su.refresh_from_db()
    assert su.dedup_ready is True


def test_runner_outcome_carries_anki_matches(stub_dedup, monkeypatch):
    """The runner merges the live-deck result (matches + warning) into its
    outcome instead of discarding it."""
    from submissions import anki as _anki

    su = _make_url()
    card = Card.objects.create(
        submitted_url=su, note_type="basic", front="Q", source_term="t", tags={}
    )

    def fake_anki(cards, deck_name=None, *a, **k):
        return _anki.AnkiDedupResult(
            matches=[
                _anki.AnkiDedupMatch(
                    card=card, note_id=7, note_text="some note", similarity=0.9
                )
            ],
            warning="w",
        )

    monkeypatch.setattr(_anki, "dedup_cards_against_anki", fake_anki)

    outcome = post_generation.run_post_generation(su, [card])

    assert outcome.anki_duplicates == 1
    assert outcome.anki_matches[0]["note_id"] == 7
    assert outcome.anki_warnings == ["w"]
