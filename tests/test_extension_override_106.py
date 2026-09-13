"""Tests for issue #106: optional provider/model override in extension submit."""

import json
import os

import pytest
from django.urls import reverse

from submissions import extension_tasks, generation, llm
from submissions.extension_auth import mint_token
from submissions.models import Batch, BatchRequest, LLMCall, SubmittedURL

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def token_file(tmp_path, settings):
    settings.EXTENSION_TOKEN_FILE = tmp_path / ".extension_token"


@pytest.fixture
def token(token_file):
    return mint_token()


@pytest.fixture(autouse=True)
def extension_id(settings):
    settings.EXTENSION_ID = "abcdefghijklmnop"


def auth_header(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


LONG_TEXT = "word " * 60

SUBMIT_URL = reverse("extension:submit")


class CaptureLLM:
    def __init__(self):
        self.calls = []

    def __call__(self, *, system, prompt, response_format=None, max_tokens=None,
                 **kw):
        self.calls.append({"provider": kw.get("provider"), "model": kw.get("model")})
        payload = {
            "cards": [
                dict(
                    note_type="basic",
                    front="What is X?",
                    back="X is Y.",
                    source_term="X",
                    topic="t",
                )
            ]
        }
        return llm.LLMResult(text=json.dumps(payload), parsed=payload)


def _post(client, token, body):
    return client.post(
        SUBMIT_URL,
        data=json.dumps(body),
        content_type="application/json",
        **auth_header(token),
    )


def test_no_override_fields_null_and_generate_kwargs_none(client, token, monkeypatch):
    cap = CaptureLLM()
    monkeypatch.setattr(generation.llm, "generate", cap)
    resp = _post(client, token, {"url": "https://example.com/no-override", "text": LONG_TEXT})
    assert resp.status_code == 202
    su = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert su.llm_provider_override is None
    assert su.llm_model_override is None
    assert len(cap.calls) == 1
    assert cap.calls[0] == {"provider": None, "model": None}


def test_valid_override_persisted_normalized_and_reaches_provider(client, token, monkeypatch):
    cap = CaptureLLM()
    monkeypatch.setattr(generation.llm, "generate", cap)
    resp = _post(
        client, token,
        {"url": "https://example.com/ovr", "text": LONG_TEXT,
         "provider": " Anthropic ", "model": " my-model-1 "},
    )
    assert resp.status_code == 202
    su = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert su.llm_provider_override == "anthropic"
    assert su.llm_model_override == "my-model-1"
    assert len(cap.calls) == 1
    assert cap.calls[0] == {"provider": "anthropic", "model": "my-model-1"}


def test_unknown_provider_400_writes_nothing(client, token):
    resp = _post(
        client, token,
        {"url": "https://example.com/bad-prov", "text": LONG_TEXT,
         "provider": "nope"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body
    for name in llm.SUPPORTED_PROVIDERS:
        assert name in body["error"]
    assert Batch.objects.count() == 0
    assert SubmittedURL.objects.count() == 0
    assert BatchRequest.objects.count() == 0


def test_non_string_provider_model_400(client, token):
    for bad in (123, {"a": 1}, ["x"]):
        resp = _post(client, token, {"url": "https://example.com/ns", "text": LONG_TEXT, "provider": bad})
        assert resp.status_code == 400
        resp2 = _post(client, token, {"url": "https://example.com/ns2", "text": LONG_TEXT, "model": bad})
        assert resp2.status_code == 400
    assert SubmittedURL.objects.count() == 0


def test_empty_whitespace_treated_as_absent(client, token, monkeypatch):
    cap = CaptureLLM()
    monkeypatch.setattr(generation.llm, "generate", cap)
    resp = _post(
        client, token,
        {"url": "https://example.com/empty", "text": LONG_TEXT,
         "provider": "   ", "model": ""},
    )
    assert resp.status_code == 202
    su = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert su.llm_provider_override is None
    assert su.llm_model_override is None
    assert cap.calls[0] == {"provider": None, "model": None}


def test_null_treated_as_absent(client, token, monkeypatch):
    cap = CaptureLLM()
    monkeypatch.setattr(generation.llm, "generate", cap)
    resp = _post(
        client, token,
        {"url": "https://example.com/nulls", "text": LONG_TEXT,
         "provider": None, "model": None},
    )
    assert resp.status_code == 202
    su = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    assert su.llm_provider_override is None
    assert su.llm_model_override is None


def test_model_too_long_400(client, token):
    resp = _post(
        client, token,
        {"url": "https://example.com/long-model", "text": LONG_TEXT, "model": "m" * 256},
    )
    assert resp.status_code == 400
    assert SubmittedURL.objects.count() == 0


def test_model_255_ok(client, token, monkeypatch):
    cap = CaptureLLM()
    monkeypatch.setattr(generation.llm, "generate", cap)
    resp = _post(
        client, token,
        {"url": "https://example.com/m255", "text": LONG_TEXT, "model": "m" * 255},
    )
    assert resp.status_code == 202
    assert SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"]).llm_model_override == "m" * 255


def test_resubmit_replaces_and_clears(client, token, monkeypatch):
    cap = CaptureLLM()
    monkeypatch.setattr(generation.llm, "generate", cap)
    url = "https://example.com/resub"
    r1 = _post(client, token, {"url": url, "text": LONG_TEXT, "provider": "gemini", "model": "m1"})
    assert r1.status_code == 202
    su = SubmittedURL.objects.get(pk=r1.json()["submitted_url_id"])
    assert (su.llm_provider_override, su.llm_model_override) == ("gemini", "m1")
    # Overwrite with new values.
    r2 = _post(client, token, {"url": url, "text": LONG_TEXT, "provider": "grok", "model": "m2"})
    assert r2.status_code == 202
    assert r2.json()["submitted_url_id"] == r1.json()["submitted_url_id"]
    su.refresh_from_db()
    assert (su.llm_provider_override, su.llm_model_override) == ("grok", "m2")
    # Absent clears.
    r3 = _post(client, token, {"url": url, "text": LONG_TEXT})
    assert r3.status_code == 202
    su.refresh_from_db()
    assert su.llm_provider_override is None
    assert su.llm_model_override is None


def test_missing_key_fails_async_not_400(client, token, monkeypatch, settings):
    settings.LLM_PROVIDER = "anthropic"
    settings.LLM_API_KEY_ENV_VAR = "DEFINITELY_MISSING_KEY_XYZ_106"
    monkeypatch.delenv("DEFINITELY_MISSING_KEY_XYZ_106", raising=False)
    resp = _post(
        client, token,
        {"url": "https://example.com/missing-key", "text": LONG_TEXT, "provider": "anthropic"},
    )
    assert resp.status_code == 202
    su = SubmittedURL.objects.get(pk=resp.json()["submitted_url_id"])
    # Huey immediate mode: worker already ran and failed async.
    assert su.generation_status == SubmittedURL.GenerationStatus.FAILED
    assert su.generation_error


def test_get_provider_model_override_wins_no_settings_mutation(settings):
    settings.LLM_PROVIDER = "anthropic"
    settings.LLM_MODEL = "base-model"
    settings.LLM_GEMINI_MODEL = "gemini-base"
    before = (settings.LLM_MODEL, settings.LLM_GEMINI_MODEL)
    p = llm.get_provider(provider="gemini", model="override-m")
    assert p.name == "gemini"
    assert p.model == "override-m"
    assert (settings.LLM_MODEL, settings.LLM_GEMINI_MODEL) == before
    p2 = llm.get_provider()
    assert p2.model == "base-model"


def test_llmcall_records_actual_provider_model(monkeypatch, settings):
    from submissions.llm import AnthropicProvider

    seen = {}

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                seen.update(kwargs)

                class Block:
                    type = "text"
                    text = '{"cards": []}'

                class Usage:
                    input_tokens = 1
                    output_tokens = 2

                class Resp:
                    content = [Block()]
                    stop_reason = "end_turn"
                    usage = Usage()

                return Resp()

    monkeypatch.setattr("submissions.llm._new_anthropic_client", lambda api_key: FakeClient())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("submissions.llm._sleep", lambda s: None)
    result = llm.generate(system="s", prompt="p", provider="anthropic", model="custom-m123")
    assert result.model == "custom-m123"
    row = LLMCall.objects.order_by("-id").first()
    assert row is not None
    assert row.provider == "anthropic"
    assert row.model == "custom-m123"
