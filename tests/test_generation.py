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
