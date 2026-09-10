"""Tests for local semantic dedup (issue #7).

The embedding model is always stubbed: :func:`submissions.dedup.load_embedding_model`
is monkeypatched to return a deterministic fake encoder. No network, no torch
weights, no ``sentence-transformers`` model construction.
"""

from io import StringIO

import numpy as np
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from submissions import dedup, generation
from submissions.models import Card, SubmittedURL

pytestmark = pytest.mark.django_db


# --- deterministic fake encoder --------------------------------------

# Token -> unit-ish vector. Chosen so cosine similarity is predictable:
#   aaa vs aab  -> ~0.99  (duplicate)
#   aaa vs ccc  -> 0.0    (distinct)
#   anchor vs above -> 0.83  (just above the 0.82 threshold)
#   anchor vs below -> 0.81  (just below the 0.82 threshold)
VECTORS = {
    "aaa": [1.0, 0.0, 0.0],
    "aab": [0.99, 0.141, 0.0],
    "ccc": [0.0, 1.0, 0.0],
    "ddd": [0.0, 0.0, 1.0],
    "anchor": [1.0, 0.0, 0.0],
    "above": [0.83, 0.5577, 0.0],
    "below": [0.81, 0.5864, 0.0],
}


class FakeModel:
    def __init__(self):
        self.calls = []

    def encode(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        return np.asarray([self._vec(t) for t in texts], dtype=float)

    @staticmethod
    def _vec(text):
        tokens = set(text.lower().split())
        for key, vec in VECTORS.items():
            if key in tokens:
                return vec
        return [0.0, 0.0, 0.0]


@pytest.fixture
def fake_model(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(dedup, "load_embedding_model", lambda: model)
    return model


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    """Guarantee no test accidentally loads the real model."""
    def _boom():
        raise AssertionError("real embedding model loaded in a test")

    monkeypatch.setattr(dedup, "load_embedding_model", _boom)


# --- helpers -------------------------------------------------------

_SU = None


def _url():
    return SubmittedURL.objects.create(
        url=f"https://example.com/{SubmittedURL.objects.count()}",
        status=SubmittedURL.Status.OK,
    )


def _card(keyword, *, batch=None, su=None):
    su = su or _url()
    return Card.objects.create(
        submitted_url=su,
        batch=batch,
        note_type=Card.NoteType.BASIC,
        front=f"what is {keyword}",
        back=f"it is {keyword}",
        source_term=keyword,
        tags={},
    )


def _run(*args):
    out = StringIO()
    call_command("dedup_cards", *args, stdout=out)
    return out.getvalue()


# --- tests --------------------------------------------------------


def test_near_duplicate_across_runs_is_marked(fake_model):
    first = _card("aaa")
    dedup.dedup_cards([first])

    second = _card("aab")
    dedup.dedup_cards([second])

    second.refresh_from_db()
    assert second.dedup_status == Card.DedupStatus.DUPLICATE
    assert second.duplicate_of_id == first.pk
    assert second.similarity_score == pytest.approx(0.99, abs=1e-2)
    first.refresh_from_db()
    assert first.dedup_status == Card.DedupStatus.UNIQUE


def test_near_duplicate_within_batch_keeps_lowest_pk(fake_model):
    a = _card("aaa")
    b = _card("aab")

    dedup.dedup_cards([b, a])  # unsorted input on purpose

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.dedup_status == Card.DedupStatus.UNIQUE
    assert b.dedup_status == Card.DedupStatus.DUPLICATE
    assert b.duplicate_of_id == a.pk


def test_distinct_terms_both_unique(fake_model):
    a = _card("aaa")
    c = _card("ccc")

    dedup.dedup_cards([a, c])

    a.refresh_from_db()
    c.refresh_from_db()
    assert a.dedup_status == Card.DedupStatus.UNIQUE
    assert c.dedup_status == Card.DedupStatus.UNIQUE
    # distinct term records its (low) highest observed similarity
    assert c.similarity_score == pytest.approx(0.0, abs=1e-6)


def test_empty_comparison_set_all_unique(fake_model):
    a = _card("aaa")
    summary = dedup.dedup_cards([a])

    a.refresh_from_db()
    assert a.dedup_status == Card.DedupStatus.UNIQUE
    assert a.similarity_score == pytest.approx(0.0)
    assert a.embedding  # recorded
    assert summary.duplicates == 0
    assert len(fake_model.calls) == 1  # one encode call


def test_threshold_boundary(fake_model):
    anchor = _card("anchor")
    dedup.dedup_cards([anchor])

    above = _card("above")
    below = _card("below")
    dedup.dedup_cards([above, below])

    above.refresh_from_db()
    below.refresh_from_db()
    assert above.dedup_status == Card.DedupStatus.DUPLICATE
    assert below.dedup_status == Card.DedupStatus.UNIQUE


def test_rerun_reuses_cached_embeddings(fake_model):
    a = _card("aaa")
    b = _card("aab")
    dedup.dedup_cards([a, b])
    assert len(fake_model.calls) == 1

    b.refresh_from_db()
    assert b.dedup_status == Card.DedupStatus.DUPLICATE

    # Re-run over all cards; nothing new to encode.
    dedup.dedup_cards([a, b])
    assert len(fake_model.calls) == 1  # no second encode call

    b.refresh_from_db()
    assert b.dedup_status == Card.DedupStatus.DUPLICATE


def test_force_recomputes_embeddings(fake_model):
    a = _card("aaa")
    dedup.dedup_cards([a])
    assert len(fake_model.calls) == 1

    dedup.dedup_cards([a], force=True)
    assert len(fake_model.calls) == 2


def test_empty_card_text_left_unique_with_null_score(fake_model):
    su = _url()
    card = Card(
        submitted_url=su,
        note_type=Card.NoteType.BASIC,
        front="",
        back="",
        source_term="",
        tags={},
    )
    card.save()
    other = _card("aaa")

    summary = dedup.dedup_cards([card, other])

    card.refresh_from_db()
    assert card.dedup_status == Card.DedupStatus.UNIQUE
    assert card.similarity_score is None
    assert card.embedding == []
    assert any("empty text" in r.note for r in summary.results)


def test_duplicates_excluded_from_review_queryset(fake_model):
    first = _card("aaa")
    dedup.dedup_cards([first])
    second = _card("aab")
    dedup.dedup_cards([second])

    review = list(Card.objects.for_review())
    assert first in review
    assert second not in review
    # not deleted, still reachable
    assert Card.objects.count() == 2
    assert list(Card.objects.duplicates()) == [second]


def test_model_load_failure_exits_nonzero_and_marks_nothing(monkeypatch):
    def _fail():
        raise dedup.ModelLoadError("weights missing; run the download step")

    monkeypatch.setattr(dedup, "load_embedding_model", _fail)
    card = _card("aaa")

    with pytest.raises(CommandError):
        _run("--all")

    card.refresh_from_db()
    assert card.dedup_status == Card.DedupStatus.UNIQUE
    assert card.similarity_score is None
    assert card.embedding == []


def test_command_reports_per_card(fake_model):
    a = _card("aaa")
    dedup.dedup_cards([a])
    b = _card("aab")

    out = _run("--all")

    assert f"card {b.pk}" in out
    assert "duplicate of card" in out
    b.refresh_from_db()
    assert b.dedup_status == Card.DedupStatus.DUPLICATE


def test_command_include_duplicates_flag(fake_model):
    a = _card("aaa")
    dedup.dedup_cards([a])
    b = _card("aab")
    dedup.dedup_cards([b])

    # default: duplicate b is not re-checked / not listed
    out = _run("--all")
    assert f"card {b.pk}" not in out

    out = _run("--all", "--include-duplicates")
    assert f"card {b.pk}" in out


def test_batch_selector(fake_model):
    from submissions.models import Batch

    batch = Batch.objects.create()
    a = _card("aaa", batch=batch)
    b = _card("ccc", batch=batch)
    _card("ddd")  # different batch (none)

    out = _run("--batch", str(batch.pk))

    assert f"card {a.pk}" in out
    assert f"card {b.pk}" in out
    assert "2 card(s)" in out


def test_generation_runs_dedup_as_final_step(fake_model, monkeypatch):
    """generate_for calls dedup.dedup_cards on the freshly created cards."""
    seen = {}
    real = dedup.dedup_cards

    def spy(cards, **kw):
        cards = list(cards)
        seen["cards"] = cards
        return real(cards, **kw)

    monkeypatch.setattr(generation.dedup, "dedup_cards", spy)

    su = _url()
    su.extracted_text = "x " * 300
    su.save()

    def fake_llm(*, system, prompt, response_format=None, max_tokens=None):
        from submissions import llm

        payload = {
            "cards": [
                {"note_type": "basic", "front": "what is aaa", "back": "aaa",
                 "source_term": "aaa", "topic": ""},
                {"note_type": "basic", "front": "what is aab", "back": "aab",
                 "source_term": "aab", "topic": ""},
            ]
        }
        import json

        return llm.LLMResult(text=json.dumps(payload), parsed=payload)

    monkeypatch.setattr(generation.llm, "generate", fake_llm)

    generation.generate_for(su)

    assert "cards" in seen and len(seen["cards"]) == 2
    dup = su.cards.get(source_term="aab")
    assert dup.dedup_status == Card.DedupStatus.DUPLICATE
