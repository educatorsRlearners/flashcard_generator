"""Tests for issue #36: chaining card generation after an extension submission.

``submissions.extension_tasks.process_extension_submission`` (a #35 no-op
stub until now) is exercised in isolation here, patching the
``run_generation`` seam the same way ``tests/test_background_processing.py``
patches ``tasks.run_extraction`` (see that module's docstring / AGENTS.md
constraints). The final test in this file is the important one: it wires up
the *real* ``generation.generate_for`` (only the LLM call itself stubbed,
via the same ``submissions.llm.generate`` seam ``tests/test_generation.py``
uses) and drives the #35 submit + status endpoints together end-to-end in
Huey immediate mode - the first test that exercises #35 and #36 together
rather than each in isolation with the other mocked away.
"""

import json

import pytest
from django.urls import reverse

from submissions import extension_tasks, generation, llm
from submissions.extension_auth import mint_token
from submissions.generation import GenerationResult
from submissions.models import Batch, BatchRequest, Card, SubmittedURL

pytestmark = pytest.mark.django_db


def _make_url(url="https://example.com/ext", **kw):
    opts = dict(
        status=SubmittedURL.Status.OK,
        extraction_method=SubmittedURL.ExtractionMethod.EXTENSION,
        extracted_text="word " * 60,
        extracted_title="Ext Title",
    )
    opts.update(kw)
    return SubmittedURL.objects.create(url=url, **opts)


# --- Unit-level: process_extension_submission in isolation -------------


def test_successful_generation_leaves_row_ok_and_terminal(monkeypatch):
    submitted_url = _make_url()

    def fake_run_generation(su):
        su.generation_status = SubmittedURL.GenerationStatus.OK
        su.generation_error = ""
        su.save(update_fields=["generation_status", "generation_error"])
        return GenerationResult(outcome="created", counts={"basic": 1})

    monkeypatch.setattr(extension_tasks, "run_generation", fake_run_generation)

    extension_tasks.process_extension_submission(submitted_url.pk)

    submitted_url.refresh_from_db()
    assert submitted_url.generation_status == SubmittedURL.GenerationStatus.OK
    assert submitted_url.generation_error == ""
    Generation = SubmittedURL.GenerationStatus
    terminal = (
        submitted_url.status == SubmittedURL.Status.FAILED
        or submitted_url.generation_status in (Generation.OK, Generation.FAILED)
    )
    assert terminal is True


def test_generation_failure_isolated_marks_row_failed(monkeypatch):
    submitted_url = _make_url()

    def fake_run_generation(su):
        raise llm.LLMAuthError("bad key")

    monkeypatch.setattr(extension_tasks, "run_generation", fake_run_generation)

    # Must not raise out of the task - this is what keeps the Huey worker
    # thread alive.
    extension_tasks.process_extension_submission(submitted_url.pk)

    submitted_url.refresh_from_db()
    assert submitted_url.generation_status == SubmittedURL.GenerationStatus.FAILED
    assert submitted_url.generation_error
    assert "bad key" in submitted_url.generation_error


def test_does_not_exist_is_a_silent_no_op():
    missing_pk = 999999
    assert not SubmittedURL.objects.filter(pk=missing_pk).exists()

    # Must return cleanly, no exception.
    extension_tasks.process_extension_submission(missing_pk)


def test_force_true_always_passed(monkeypatch):
    submitted_url = _make_url()
    seen = {}

    def fake_generate_for(su, *, force=False):
        seen["force"] = force
        return GenerationResult(outcome="created", counts={"basic": 1})

    monkeypatch.setattr(generation, "generate_for", fake_generate_for)

    extension_tasks.process_extension_submission(submitted_url.pk)

    assert seen["force"] is True


# --- Integration: #35 submit + status endpoints, real generate_for -----


class FakeLLM:
    """Drop-in for ``submissions.llm.generate`` (mirrors test_generation.py)."""

    def __call__(self, *, system, prompt, response_format=None, max_tokens=None):
        payload = {
            "cards": [
                dict(
                    note_type="basic",
                    front="What is the mitochondria?",
                    back="The powerhouse of the cell.",
                    source_term="mitochondria",
                    topic="biology",
                )
            ]
        }
        return llm.LLMResult(text=json.dumps(payload), parsed=payload)


@pytest.fixture(autouse=True)
def token_file(tmp_path, settings):
    settings.EXTENSION_TOKEN_FILE = tmp_path / ".extension_token"


@pytest.fixture
def token(token_file):
    return mint_token()


def auth_header(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


LONG_TEXT = (
    "Mitochondria are membrane-bound organelles found in the cytoplasm of "
    "almost all eukaryotic cells. They generate most of the cell's supply "
    "of adenosine triphosphate, used as a source of chemical energy. "
) * 3


def test_submit_then_status_end_to_end_real_generate_for(client, token, monkeypatch):
    """#35 (submit/status endpoints) + #36 (generation task) together.

    Real ``generation.generate_for`` runs (nothing about it mocked away
    except the outbound LLM call itself, via the same
    ``submissions.llm.generate`` seam ``tests/test_generation.py`` uses -
    so this exercises the full extension_api -> extension_tasks ->
    generation chain, not each piece in isolation with the others mocked.
    """
    monkeypatch.setattr(generation.llm, "generate", FakeLLM())

    submit_body = {
        "url": "https://example.com/mitochondria",
        "title": "Mitochondria",
        "text": LONG_TEXT,
    }
    submit_resp = client.post(
        reverse("extension:submit"),
        data=json.dumps(submit_body),
        content_type="application/json",
        **auth_header(token),
    )
    assert submit_resp.status_code == 202
    submitted_url_id = submit_resp.json()["submitted_url_id"]

    # Huey immediate mode means the task already ran synchronously by the
    # time the POST returned - one status call is exactly what a real poll
    # loop's final, terminal iteration would see.
    status_resp = client.get(
        reverse("extension:submission_status", kwargs={"submitted_url_id": submitted_url_id}),
        **auth_header(token),
    )
    assert status_resp.status_code == 200
    payload = status_resp.json()

    assert payload["terminal"] is True
    assert payload["generation_status"] == "ok"
    assert payload["review_url"] is not None

    submitted_url = SubmittedURL.objects.get(pk=submitted_url_id)
    assert submitted_url.generation_status == SubmittedURL.GenerationStatus.OK
    assert submitted_url.generation_error == ""
    assert Card.objects.filter(submitted_url=submitted_url).count() == 1
