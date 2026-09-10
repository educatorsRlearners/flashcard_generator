"""Tests for durable review feedback + few-shot injection (issue #10).

Feedback rows are written by ``card_review_decision`` when a card is accepted
or rejected, survive deletion of the batch/cards they came from, and are
injected (capped) into the generation system prompt as few-shot examples.
"""

import pytest
from django.urls import reverse

from submissions import generation
from submissions.generation import (
    FEWSHOT_EXAMPLES_PER_CATEGORY,
    build_fewshot_section,
    build_system_prompt,
)
from submissions.models import (
    Batch,
    BatchRequest,
    Card,
    Feedback,
    SubmittedURL,
)

pytestmark = pytest.mark.django_db


def _make_card(batch, *, url="https://example.com/a", front="What is X?",
               back="X is a thing.", note_type="basic", source_term="X"):
    su = SubmittedURL.objects.create(
        url=url,
        status=SubmittedURL.Status.OK,
        extracted_text="text",
        batch=batch,
    )
    BatchRequest.objects.create(batch=batch, submitted_url=su)
    return Card.objects.create(
        submitted_url=su,
        batch=batch,
        note_type=note_type,
        front=front,
        back=back,
        source_term=source_term,
        tags={"source_url": url, "date_added": "2026-01-01", "topic": "t"},
    )


def _decide(client, batch, card, decision, reason=""):
    resp = client.post(
        reverse(
            "submissions:card_review_decision",
            kwargs={"batch_pk": batch.pk, "card_pk": card.pk},
        ),
        {"decision": decision, "reason": reason},
    )
    assert resp.status_code in (200, 302), resp.content
    return resp


# --- persistence -----------------------------------------------------


def test_accept_decision_persists_feedback_row(client):
    batch = Batch.objects.create()
    card = _make_card(batch, front="What is chlorophyll?", back="Green pigment.")

    _decide(client, batch, card, "accepted")

    fb = Feedback.objects.get()
    assert fb.decision == Feedback.Decision.ACCEPTED
    assert fb.front == "What is chlorophyll?"
    assert fb.back == "Green pigment."
    assert fb.note_type == "basic"
    assert fb.source_url == "https://example.com/a"
    assert fb.reason == ""


def test_reject_decision_persists_reason(client):
    batch = Batch.objects.create()
    card = _make_card(batch)

    _decide(client, batch, card, "rejected", reason="too vague")

    fb = Feedback.objects.get()
    assert fb.decision == Feedback.Decision.REJECTED
    assert fb.reason == "too vague"


def test_undecided_writes_no_feedback(client):
    batch = Batch.objects.create()
    card = _make_card(batch)

    _decide(client, batch, card, "undecided")

    assert Feedback.objects.count() == 0


def test_feedback_survives_batch_deletion(client):
    batch = Batch.objects.create()
    card = _make_card(batch)
    _decide(client, batch, card, "rejected", reason="bad card")
    assert Feedback.objects.count() == 1

    # Tear down everything the card came from.
    SubmittedURL.objects.all().delete()  # cascades the Card
    batch.delete()

    assert Card.objects.count() == 0
    assert SubmittedURL.objects.count() == 0
    assert Batch.objects.count() == 0
    fb = Feedback.objects.get()
    assert fb.front == "What is X?"
    assert fb.reason == "bad card"


# --- few-shot assembly ----------------------------------------------


def test_zero_feedback_no_section(client):
    assert build_fewshot_section() == ""
    assert build_system_prompt() == generation.SYSTEM_PROMPT


def test_prompt_contains_known_examples_and_reasons():
    Feedback.objects.create(
        note_type="basic", front="Accepted front A", back="Accepted back A",
        decision=Feedback.Decision.ACCEPTED,
    )
    Feedback.objects.create(
        note_type="cloze", front="Rejected front R", back="",
        decision=Feedback.Decision.REJECTED, reason="reason-R-here",
    )

    section = build_fewshot_section()

    assert "Accepted examples:" in section
    assert "Rejected examples:" in section
    assert "Accepted front A" in section
    assert "Accepted back A" in section
    assert "Rejected front R" in section
    assert "reason-R-here" in section
    assert build_system_prompt().endswith(section)


def test_cap_is_honoured_when_over_cap_feedback_exists():
    over = FEWSHOT_EXAMPLES_PER_CATEGORY + 4
    for i in range(over):
        Feedback.objects.create(
            note_type="basic", front=f"acc-{i}",
            decision=Feedback.Decision.ACCEPTED,
        )
        Feedback.objects.create(
            note_type="basic", front=f"rej-{i}",
            decision=Feedback.Decision.REJECTED, reason=f"r-{i}",
        )

    section = build_fewshot_section()

    acc_shown = [i for i in range(over) if f"acc-{i}" in section]
    rej_shown = [i for i in range(over) if f"rej-{i}" in section]
    assert len(acc_shown) == FEWSHOT_EXAMPLES_PER_CATEGORY
    assert len(rej_shown) == FEWSHOT_EXAMPLES_PER_CATEGORY
    # Most-recent kept (highest ids), ordered oldest-first in the text.
    assert acc_shown == sorted(range(over))[-FEWSHOT_EXAMPLES_PER_CATEGORY:]
    assert section.index("acc-{}".format(acc_shown[0])) < section.index(
        "acc-{}".format(acc_shown[-1])
    )


def test_only_accepted_feedback():
    Feedback.objects.create(
        note_type="basic", front="only-accepted",
        decision=Feedback.Decision.ACCEPTED,
    )
    section = build_fewshot_section()
    assert "Accepted examples:" in section
    assert "Rejected examples:" not in section
    assert "only-accepted" in section


def test_only_rejected_feedback():
    Feedback.objects.create(
        note_type="basic", front="only-rejected",
        decision=Feedback.Decision.REJECTED, reason="nope",
    )
    section = build_fewshot_section()
    assert "Rejected examples:" in section
    assert "Accepted examples:" not in section
    assert "only-rejected" in section
    assert "nope" in section


def test_reasonless_rejection_still_used():
    Feedback.objects.create(
        note_type="basic", front="reasonless-reject",
        decision=Feedback.Decision.REJECTED, reason="",
    )
    section = build_fewshot_section()
    assert "reasonless-reject" in section
    assert "(no reason given)" in section


def test_generation_call_uses_feedback_prompt(client, monkeypatch):
    """End-to-end: a stored rejection shows up in the system prompt handed to
    the LLM client during generation."""
    import json

    from submissions import llm

    Feedback.objects.create(
        note_type="basic", front="known-example-front",
        decision=Feedback.Decision.REJECTED, reason="known-example-reason",
    )

    calls = []

    def fake_generate(*, system, prompt, response_format=None, max_tokens=None):
        calls.append(system)
        payload = {"cards": [dict(
            note_type="basic", front="F", back="B", source_term="T", topic="",
        )]}
        return llm.LLMResult(text=json.dumps(payload), parsed=payload)

    monkeypatch.setattr(generation.llm, "generate", fake_generate)

    su = SubmittedURL.objects.create(
        url="https://example.com/gen",
        status=SubmittedURL.Status.OK,
        extracted_text="word " * 200,
        extracted_title="t",
    )
    generation.generate_for(su)

    assert calls
    assert "known-example-front" in calls[0]
    assert "known-example-reason" in calls[0]
