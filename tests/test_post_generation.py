"""Tests for the post-generation stage runner (issue #130).

``generation.generate_for`` persists cards first and then delegates
image attachment to ``submissions.post_generation.run_post_generation``.
These tests pin the moved behavior: identical log messages, identical
``GenerationResult`` fields, unchanged persistence ordering, and the
injection seam for disabling/replacing stages without editing
``generation.py``.
"""

import inspect
import logging

import pytest

from submissions import generation, llm, post_generation
from submissions.models import BatchRequest, SubmittedURL

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


# --- the injected-stages seam ------------------------------------------


def test_default_stages_run_in_generation_order():
    assert post_generation.DEFAULT_STAGES == (
        post_generation.image_attachment_stage,
    )


def test_injected_stages_replace_the_pipeline(install_llm, monkeypatch):
    """A caller-supplied stage list runs instead of the defaults, with no
    edit to generation code and no image-attachment call."""
    install_llm([_card()])
    su = _make_url()

    calls = []

    def stub_stage(submitted_url, cards, outcome):
        calls.append("stub")

    from submissions import images as _images

    def boom(submitted_url, cards):
        raise AssertionError("default image stage must not run")

    monkeypatch.setattr(_images, "attach_images", boom)

    result = generation.generate_for(su, stages=[stub_stage])

    assert calls == ["stub"]
    assert result.outcome == "created"
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
    assert "images." not in body
    assert "run_post_generation" in body


# --- preserved best-effort paths ----------------------------------------


def test_image_failure_is_logged_and_generation_completes(
    install_llm, monkeypatch, caplog
):
    from submissions import images as _images

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


# --- unchanged persistence ordering --------------------------------------


def test_persistence_happens_before_any_stage_runs(install_llm, monkeypatch):
    """bulk_create + the generation-OK mark happen before the first stage
    runs."""
    install_llm([_card(), _card(front="What is Y?", source_term="Y")])
    su = _make_url()

    observed = {}

    from submissions import images as _images

    def blocking_stage(submitted_url, cards):
        observed["persisted"] = su.cards.count()
        su.refresh_from_db()
        observed["generation_status"] = su.generation_status

    monkeypatch.setattr(_images, "attach_images", blocking_stage)

    generation.generate_for(su)

    assert observed["persisted"] == 2
    assert observed["generation_status"] == SubmittedURL.GenerationStatus.OK
