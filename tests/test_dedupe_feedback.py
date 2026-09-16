"""Tests for the one-off dedupe_feedback cleanup command (issue #146)."""

import pytest
from django.core.management import call_command

from submissions.models import Feedback

pytestmark = pytest.mark.django_db


def _make_feedback(**kwargs):
    defaults = {
        "note_type": "basic",
        "front": "What is X?",
        "back": "X is a thing.",
        "source_url": "https://example.com/a",
        "decision": Feedback.Decision.REJECTED,
        "reason": "too vague",
        "was_edited": False,
    }
    defaults.update(kwargs)
    return Feedback.objects.create(**defaults)


def test_exact_dupe_keeps_oldest():
    first = _make_feedback()
    second = _make_feedback()
    third = _make_feedback()

    call_command("dedupe_feedback")

    remaining = list(Feedback.objects.order_by("id"))
    assert [fb.id for fb in remaining] == [first.id]
    assert second.id not in [fb.id for fb in remaining]
    assert third.id not in [fb.id for fb in remaining]


def test_accepted_blank_reason_dedups():
    first = _make_feedback(decision=Feedback.Decision.ACCEPTED, reason="")
    _make_feedback(decision=Feedback.Decision.ACCEPTED, reason="")
    _make_feedback(decision=Feedback.Decision.ACCEPTED, reason="")

    call_command("dedupe_feedback")

    remaining = list(Feedback.objects.order_by("id"))
    assert [fb.id for fb in remaining] == [first.id]


def test_dry_run_deletes_nothing(capsys):
    _make_feedback()
    _make_feedback()
    before = Feedback.objects.count()

    call_command("dedupe_feedback", "--dry-run")

    assert Feedback.objects.count() == before == 2
    out = capsys.readouterr().out
    assert "duplicate group(s)" in out


def test_different_reason_rows_both_kept(capsys):
    _make_feedback(reason="too vague")
    _make_feedback(reason="wrong answer")

    call_command("dedupe_feedback")

    assert Feedback.objects.count() == 2
    out = capsys.readouterr().out
    assert "No duplicate" in out or "0 duplicate group(s)" in out


def test_was_edited_differing_rows_both_kept():
    _make_feedback(was_edited=False)
    _make_feedback(was_edited=True)

    call_command("dedupe_feedback")

    assert Feedback.objects.count() == 2


def test_idempotent_second_run(capsys):
    # Mirror of the dev DB grooming data: one pair + one triple = 3 excess.
    _make_feedback(front="pair", reason="same reason")
    _make_feedback(front="pair", reason="same reason")
    _make_feedback(
        front="triple", decision=Feedback.Decision.ACCEPTED, reason=""
    )
    _make_feedback(
        front="triple", decision=Feedback.Decision.ACCEPTED, reason=""
    )
    _make_feedback(
        front="triple", decision=Feedback.Decision.ACCEPTED, reason=""
    )
    assert Feedback.objects.count() == 5

    call_command("dedupe_feedback")
    assert Feedback.objects.count() == 2

    call_command("dedupe_feedback")
    assert Feedback.objects.count() == 2
    out = capsys.readouterr().out
    assert "duplicate group(s)" in out
