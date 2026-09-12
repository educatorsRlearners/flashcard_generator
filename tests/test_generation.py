"""Tests for card generation (issue #6).

The #5 LLM client (``submissions.llm.generate``) is stubbed via monkeypatch:
no network, no ``ANTHROPIC_API_KEY``, no ``anthropic`` import.
"""

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from submissions import generation, llm
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


class FakeLLM:
    """Drop-in for ``submissions.llm.generate``.

    ``behaviour`` is either a list of card dicts (returned for every call), a
    callable ``(system, prompt) -> list[dict] | Exception``, or an Exception
    instance to raise.
    """

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def __call__(self, *, system, prompt, response_format=None, max_tokens=None):
        self.calls.append(
            dict(
                system=system,
                prompt=prompt,
                response_format=response_format,
                max_tokens=max_tokens,
            )
        )
        result = self.behaviour
        if callable(result) and not isinstance(result, Exception):
            result = result(system, prompt)
        if isinstance(result, Exception):
            raise result
        payload = {"cards": result}
        return llm.LLMResult(text=json.dumps(payload), parsed=payload)


@pytest.fixture
def install_llm(monkeypatch):
    def _install(behaviour):
        fake = FakeLLM(behaviour)
        monkeypatch.setattr(generation.llm, "generate", fake)
        return fake

    return _install


def _card(note_type="basic", front="What is X?", back="X is a thing.",
          source_term="X", topic="biology"):
    return dict(
        note_type=note_type, front=front, back=back,
        source_term=source_term, topic=topic,
    )


def _run(*args, **kwargs):
    from io import StringIO

    out = StringIO()
    call_command("generate_cards", *args, stdout=out, **kwargs)
    return out.getvalue()


# --- happy path -----------------------------------------------------


def test_happy_path_produces_basic_and_cloze_mix(install_llm):
    install_llm([
        _card("basic", "What is chlorophyll?", "The green pigment.", "chlorophyll"),
        _card("cloze", "{{c1::Chlorophyll}} absorbs light in the chloroplast.",
              "", "chlorophyll"),
        _card("basic", "What is the Calvin cycle?", "Carbon fixation.", "Calvin cycle"),
    ])
    su = _make_url()

    out = _run("--url", su.url)

    cards = list(su.cards.all())
    assert len(cards) == 3
    note_types = {c.note_type for c in cards}
    assert note_types == {"basic", "cloze"}
    cloze = su.cards.get(note_type="cloze")
    assert "{{c1::" in cloze.front
    for c in cards:
        assert c.front.strip()
        assert c.source_term.strip()
        assert c.tags["source_url"] == su.url
    su.refresh_from_db()
    assert su.generation_status == SubmittedURL.GenerationStatus.OK
    assert "created: 2 basic, 1 cloze" in out


def test_structured_output_is_requested(install_llm):
    fake = install_llm([_card()])
    su = _make_url()

    _run("--url", su.url)

    schema = fake.calls[0]["response_format"]
    assert schema is not None
    assert schema["type"] == "object"
    assert schema["properties"]["cards"]["type"] == "array"


def test_tags_carry_source_url_and_iso_date(install_llm):
    install_llm([_card(topic="")])
    su = _make_url()

    _run("--url", su.url)

    tags = su.cards.get().tags
    assert tags["source_url"] == su.url
    assert tags["topic"] == ""  # blank, not a crash
    # ISO date (YYYY-MM-DD)
    import datetime

    datetime.date.fromisoformat(tags["date_added"])


# --- content-driven count ----------------------------------------


def test_card_count_is_content_driven(install_llm):
    # The fake returns one card per ~120 chars of page text: proves the
    # generator persists whatever the model returns, with no hard-coded N.
    def behaviour(system, prompt):
        body = prompt.split("Page text:\n", 1)[1]
        n = max(1, len(body) // 120)
        return [_card(front=f"Q{i}", source_term=f"term{i}") for i in range(n)]

    install_llm(behaviour)
    sparse = _make_url("https://example.com/sparse", text="word " * 60)  # ~300 chars
    rich = _make_url("https://example.com/rich", text=RICH_TEXT)

    _run("--id", str(sparse.pk))
    _run("--id", str(rich.pk))

    assert 0 < sparse.cards.count() < rich.cards.count()


def test_safety_cap_limits_cards(install_llm, monkeypatch):
    monkeypatch.setattr(generation, "MAX_CARDS_PER_URL", 5)
    install_llm([_card(front=f"Q{i}", source_term=f"t{i}") for i in range(20)])
    su = _make_url()

    _run("--url", su.url)

    assert su.cards.count() == 5


# --- insufficient content --------------------------------------


def test_insufficient_content_is_skipped(install_llm):
    fake = install_llm([_card()])
    su = _make_url(text="too short")

    out = _run("--url", su.url)

    assert su.cards.count() == 0
    assert fake.calls == []  # never called the model
    assert generation.INSUFFICIENT_CONTENT in out
    su.refresh_from_db()
    assert su.generation_error == generation.INSUFFICIENT_CONTENT


# --- LLM errors ----------------------------------------------


def test_transient_error_marks_url_failed_and_batch_continues(install_llm):
    batch = Batch.objects.create()
    first = _make_url("https://example.com/a", batch=batch)
    second = _make_url("https://example.com/b", batch=batch)

    calls = {"n": 0}

    def behaviour(system, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return llm.LLMTransientError("connection error", reason="connection")
        return [_card()]

    install_llm(behaviour)

    out = _run("--batch", str(batch.pk))  # exits 0 (no CommandError)

    first.refresh_from_db()
    assert first.generation_status == SubmittedURL.GenerationStatus.FAILED
    assert "connection error" in first.generation_error
    assert first.cards.count() == 0
    assert second.cards.count() > 0
    assert "failed:" in out


def test_rate_limit_error_marks_url_failed(install_llm):
    install_llm(llm.LLMRateLimitError("rate limited"))
    su = _make_url()

    out = _run("--url", su.url)

    su.refresh_from_db()
    assert su.generation_status == SubmittedURL.GenerationStatus.FAILED
    assert "failed:" in out


def test_auth_error_aborts_with_nonzero_exit(install_llm):
    batch = Batch.objects.create()
    _make_url("https://example.com/a", batch=batch)
    _make_url("https://example.com/b", batch=batch)
    install_llm(llm.LLMAuthError("no API key found"))

    with pytest.raises(CommandError):
        _run("--batch", str(batch.pk))

    assert Card.objects.count() == 0


def test_bad_response_error_skips_url_and_continues(install_llm):
    install_llm(llm.LLMBadResponseError("provider refused the request", reason="refusal"))
    su = _make_url()

    out = _run("--url", su.url)

    assert su.cards.count() == 0
    assert "skipped:" in out


# --- malformed model output --------------------------------


def test_invalid_cards_are_filtered_out(install_llm):
    install_llm([
        _card("basic", "Valid?", "Yes.", "valid"),
        _card("basic", "   ", "empty front", "bad"),          # empty front
        _card("cloze", "No markers here at all.", "", "bad"),  # cloze w/o {{cN::}}
        _card("mystery", "weird type", "x", "bad"),            # bad note_type
        _card("basic", "Also valid?", "Yes.", "  "),           # empty source_term
    ])
    su = _make_url()

    out = _run("--url", su.url)

    assert su.cards.count() == 1
    assert su.cards.get().source_term == "valid"
    assert "rejected" in out


def test_all_invalid_cards_records_no_valid_cards_produced(install_llm):
    install_llm([
        _card("basic", "", "x", "a"),
        _card("cloze", "no marker", "", "b"),
    ])
    su = _make_url()

    out = _run("--url", su.url)

    assert su.cards.count() == 0
    su.refresh_from_db()
    assert su.generation_status == SubmittedURL.GenerationStatus.FAILED
    assert su.generation_error == generation.NO_VALID_CARDS
    assert generation.NO_VALID_CARDS in out


def test_malformed_json_shape_records_no_valid_cards(install_llm, monkeypatch):
    # llm raises a schema-violation style bad response -> "no valid cards produced"
    fake = FakeLLM(llm.LLMBadResponseError("not a card list", reason="schema_violation"))
    monkeypatch.setattr(generation.llm, "generate", fake)
    su = _make_url()

    out = _run("--url", su.url)

    assert su.cards.count() == 0
    assert generation.NO_VALID_CARDS in out


# --- re-run / --force ----------------------------------------


def test_plain_rerun_skips_urls_with_existing_cards(install_llm):
    install_llm([_card(), _card(front="Q2", source_term="t2")])
    su = _make_url()

    _run("--url", su.url)
    assert su.cards.count() == 2

    out = _run("--url", su.url)
    assert su.cards.count() == 2  # no duplicates
    assert generation.ALREADY_HAS_CARDS in out


def test_no_selector_run_skips_urls_that_already_have_cards(install_llm):
    install_llm([_card()])
    done = _make_url("https://example.com/done")
    Card.objects.create(
        submitted_url=done, note_type="basic", front="x", source_term="x", tags={}
    )
    fresh = _make_url("https://example.com/fresh")

    _run()

    assert fresh.cards.count() == 1
    assert done.cards.count() == 1  # untouched


def test_force_regenerates_without_duplicates(install_llm):
    fake = install_llm([_card(), _card(front="Q2", source_term="t2")])
    su = _make_url()

    _run("--url", su.url)
    original_ids = set(su.cards.values_list("pk", flat=True))
    assert len(original_ids) == 2

    _run("--url", su.url, force=True)

    assert su.cards.count() == 2
    assert set(su.cards.values_list("pk", flat=True)).isdisjoint(original_ids)
    assert len(fake.calls) == 2


# --- batch link -------------------------------------------


def test_card_batch_copies_url_batch_including_null(install_llm):
    install_llm([_card()])
    batched = _make_url("https://example.com/batched", batch=Batch.objects.create())
    unbatched = _make_url("https://example.com/loose", batch=None)

    _run("--id", str(batched.pk))
    _run("--id", str(unbatched.pk))

    assert batched.cards.get().batch_id == batched.batch_id
    assert unbatched.cards.get().batch_id is None


# --- issue #32: paraphrase + analogy language ---------------------------


def test_system_prompt_contains_rephrase_instruction():
    prompt = generation.build_system_prompt()
    assert "own words" in prompt.lower()
    assert "verbatim" in prompt.lower()


def test_system_prompt_contains_configured_analogy_language():
    from django.conf import settings as dj_settings

    prompt = generation.build_system_prompt()
    assert dj_settings.CARD_ANALOGY_LANGUAGE in prompt
    # Default is python (keeps analogies off Java unless configured).
    assert "python" in prompt.lower()


def test_default_analogy_language_is_python():
    from django.conf import settings as dj_settings

    assert dj_settings.CARD_ANALOGY_LANGUAGE == "python"


def test_analogy_language_switch_is_reflected(settings):
    settings.CARD_ANALOGY_LANGUAGE = "javascript"
    prompt = generation.build_system_prompt()
    assert "javascript" in prompt.lower()


def test_verbatim_check_flags_near_copy_and_passes_reworded():
    source = (
        "The behavior defining layer around the model includes the system prompt "
        "and tool descriptions and how responses get parsed and what the model "
        "remembers across steps for context management over many long sessions today"
    )
    near_copy = (
        "The behavior defining layer around the model includes the system prompt "
        "and tool descriptions and how responses get parsed and what the model "
        "remembers across steps for context management"
    )
    reworded = "Scaffolding is everything surrounding a model that steers its actions."
    assert generation.is_close_to_source(near_copy, source) is True
    assert generation.is_close_to_source(reworded, source) is False
    assert (
        generation.longest_verbatim_run(near_copy, source)
        > generation.MAX_VERBATIM_WORDS
    )
    assert (
        generation.longest_verbatim_run(reworded, source)
        <= generation.MAX_VERBATIM_WORDS
    )


def test_verbatim_boundary_twelve_words_ok_thirteen_flagged():
    words = (
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
        "nu xi omicron pi rho sigma tau upsilon"
    )
    twelve = " ".join(words.split()[:12])
    thirteen = " ".join(words.split()[:13])
    assert generation.is_close_to_source(twelve, words) is False
    assert generation.is_close_to_source(thirteen, words) is True


def test_close_to_source_cards_are_kept_but_logged(install_llm):
    source_bits = (
        "The behavior defining layer around the model includes the system prompt "
        "and tool descriptions and how responses get parsed and what the model "
        "remembers across steps for context management over many long sessions today. "
    )
    su = _make_url(text=source_bits * 4)
    near_copy = (
        "The behavior defining layer around the model includes the system prompt "
        "and tool descriptions and how responses get parsed and what the model "
        "remembers across steps for context management"
    )
    install_llm([_card("basic", "What is scaffolding?", near_copy, "scaffolding")])

    out = _run("--url", su.url)

    assert su.cards.count() == 1  # kept, not dropped/regenerated
    assert "1 cards close to source wording" in out


# --- issue #29: live-deck dedup result surfaced in GenerationResult -------


def _stub_anki_dedup(monkeypatch, fake_fn):
    from submissions import anki as _anki

    monkeypatch.setattr(_anki, "dedup_cards_against_anki", fake_fn)
    # Isolate from local dedup embeddings + image backends.
    from submissions import dedup as _dedup
    from submissions import images as _images

    monkeypatch.setattr(_dedup, "dedup_cards", lambda cards: None)
    monkeypatch.setattr(_images, "attach_images", lambda su, cards: None)


def test_generate_for_surfaces_anki_match_in_result_and_summary(
    install_llm, monkeypatch
):
    from submissions import anki as _anki

    install_llm([_card("basic", "What is chlorophyll?", "The green pigment.", "chlorophyll")])
    su = _make_url()

    def fake_dedup(cards, deck_name=None, *a, **k):
        card = list(cards)[0]
        card.dedup_status = Card.DedupStatus.DUPLICATE
        card.similarity_score = 0.95
        card.save(update_fields=["dedup_status", "duplicate_of", "similarity_score"])
        return _anki.AnkiDedupResult(
            matches=[
                _anki.AnkiDedupMatch(
                    card=card,
                    note_id=11,
                    note_text="mitochondria powerhouse of cell",
                    similarity=0.95,
                )
            ],
            deck_notes=5,
        )

    _stub_anki_dedup(monkeypatch, fake_dedup)

    result = generation.generate_for(su)

    assert result.outcome == "created"
    assert result.anki_duplicates == 1
    assert result.anki_matches[0]["note_id"] == 11
    assert "mitochondria" in result.anki_matches[0]["note_text"]
    summary = result.summary_line(su.url)
    assert "1 skipped as already in Anki deck" in summary
    assert "mitochondria powerhouse of cell" in summary
    # Duplicates persist but stay out of the review scope.
    assert Card.objects.for_review().filter(pk=list(su.cards.all())[0].pk).count() == 0
    assert Card.objects.count() == 1


def test_generate_for_truncates_long_matched_note_text(install_llm, monkeypatch):
    from submissions import anki as _anki

    install_llm([_card()])
    su = _make_url()
    long_text = "x" * (generation.ANKI_MATCH_TEXT_PREVIEW_CHARS + 50)

    def fake_dedup(cards, deck_name=None, *a, **k):
        card = list(cards)[0]
        return _anki.AnkiDedupResult(
            matches=[
                _anki.AnkiDedupMatch(
                    card=card, note_id=1, note_text=long_text, similarity=0.9
                )
            ]
        )

    _stub_anki_dedup(monkeypatch, fake_dedup)

    result = generation.generate_for(su)
    summary = result.summary_line(su.url)
    assert long_text not in summary  # truncated
    assert "…" in summary


def test_generate_for_propagates_unreachable_anki_warning(
    install_llm, monkeypatch
):
    from submissions import anki as _anki

    install_llm([_card()])
    su = _make_url()
    warning = "Anki deck dedup skipped (Anki unreachable: boom); local-only dedup applied."

    def fake_dedup(cards, deck_name=None, *a, **k):
        return _anki.AnkiDedupResult(warning=warning)

    _stub_anki_dedup(monkeypatch, fake_dedup)

    result = generation.generate_for(su)

    assert result.outcome == "created"
    assert result.anki_duplicates == 0
    assert result.anki_warnings == [warning]
    assert warning in result.summary_line(su.url)


# --- issue #78: dedup_ready gates the extension status endpoint's terminal


def _stub_best_effort_extras(monkeypatch):
    """Isolate generate_for from Anki live-dedup + image attachment so tests
    below exercise only the local-dedup / dedup_ready behaviour."""
    from submissions import anki as _anki
    from submissions import images as _images

    monkeypatch.setattr(
        _anki, "dedup_cards_against_anki", lambda cards, deck_name=None, *a, **k: _anki.AnkiDedupResult()
    )
    monkeypatch.setattr(_images, "attach_images", lambda su, cards: None)


def test_generate_for_sets_dedup_ready_after_dedup_cards_returns(
    install_llm, monkeypatch
):
    """Reproduces the #78 race window: dedup_ready must be False while
    dedup.dedup_cards() is still running, and True only once it returns -
    this is exactly what submission_status's terminal flag reads."""
    from submissions import dedup as _dedup

    _stub_best_effort_extras(monkeypatch)
    install_llm([_card(), _card(front="What is Y?", source_term="Y")])
    su = _make_url()

    observed = {}

    def blocking_dedup(cards):
        # At this point generation_status is already "ok" (set inside the
        # earlier atomic block) but dedup_ready must still be False - the
        # exact window that let the review grid render prematurely.
        su.refresh_from_db()
        observed["generation_status_during_dedup"] = su.generation_status
        observed["dedup_ready_during_dedup"] = su.dedup_ready

    monkeypatch.setattr(_dedup, "dedup_cards", blocking_dedup)

    generation.generate_for(su)

    assert observed["generation_status_during_dedup"] == SubmittedURL.GenerationStatus.OK
    assert observed["dedup_ready_during_dedup"] is False

    su.refresh_from_db()
    assert su.dedup_ready is True


def test_generate_for_sets_dedup_ready_even_when_dedup_cards_raises_model_load_error(
    install_llm, monkeypatch
):
    """A missing embedding model must not poll forever (#78): dedup_ready
    still flips True, matching generate_for's existing best-effort handling
    of ModelLoadError."""
    from submissions import dedup as _dedup

    _stub_best_effort_extras(monkeypatch)
    install_llm([_card()])
    su = _make_url()

    def raising_dedup(cards):
        raise _dedup.ModelLoadError("model not downloaded")

    monkeypatch.setattr(_dedup, "dedup_cards", raising_dedup)

    result = generation.generate_for(su)

    assert result.outcome == "created"
    su.refresh_from_db()
    assert su.dedup_ready is True


def test_generate_for_sets_dedup_ready_even_when_dedup_cards_raises_other_error(
    install_llm, monkeypatch
):
    """Any other dedup exception is also best-effort (#78): dedup_ready
    still flips True so the status endpoint doesn't wait forever."""
    from submissions import dedup as _dedup

    _stub_best_effort_extras(monkeypatch)
    install_llm([_card()])
    su = _make_url()

    def raising_dedup(cards):
        raise RuntimeError("boom")

    monkeypatch.setattr(_dedup, "dedup_cards", raising_dedup)

    result = generation.generate_for(su)

    assert result.outcome == "created"
    su.refresh_from_db()
    assert su.dedup_ready is True


def test_zero_valid_cards_never_sets_dedup_ready(install_llm):
    """generation_status == "failed" (no valid cards) never runs dedup at
    all, so dedup_ready stays at its default False - unaffected by #78
    (submission_status already treats generation_status == "failed" as
    terminal without consulting dedup_ready)."""
    install_llm([{"note_type": "bogus", "front": "", "back": "", "source_term": "", "topic": ""}])
    su = _make_url()

    result = generation.generate_for(su)

    assert result.outcome == "failed"
    su.refresh_from_db()
    assert su.generation_status == SubmittedURL.GenerationStatus.FAILED
    assert su.dedup_ready is False


def test_force_regeneration_resets_dedup_ready(install_llm, monkeypatch):
    """A force re-run must not leave a stale dedup_ready=True from an
    earlier run visible before this run's dedup has actually finished."""
    from submissions import dedup as _dedup

    _stub_best_effort_extras(monkeypatch)
    install_llm([_card()])
    su = _make_url()
    generation.generate_for(su)
    su.refresh_from_db()
    assert su.dedup_ready is True

    observed = {}

    def blocking_dedup(cards):
        su.refresh_from_db()
        observed["dedup_ready_during_second_run"] = su.dedup_ready

    monkeypatch.setattr(_dedup, "dedup_cards", blocking_dedup)
    generation.generate_for(su, force=True)

    assert observed["dedup_ready_during_second_run"] is False
    su.refresh_from_db()
    assert su.dedup_ready is True
