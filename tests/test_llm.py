"""Tests for the provider-agnostic LLM client (``submissions/llm.py``).

The Anthropic SDK / HTTP layer is stubbed with ``monkeypatch`` - these tests
import no ``anthropic`` symbols, make no network calls, and pass with
``ANTHROPIC_API_KEY`` unset.
"""

import json
import types

import pytest
from django.test import override_settings

from submissions import llm

# Every llm.generate call records an LLMCall row (issue #28), so these
# tests need database access even though they stub the network.
pytestmark = pytest.mark.django_db


# --- fakes ------------------------------------------------------------


def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _response(text="hello", stop_reason="end_turn"):
    return types.SimpleNamespace(
        content=[_text_block(text)],
        stop_reason=stop_reason,
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class FakeAPIError(Exception):
    """Shaped like ``anthropic.APIStatusError``: carries ``status_code`` and
    an HTTP ``response`` with headers."""

    def __init__(self, status_code, headers=None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = types.SimpleNamespace(headers=headers or {})


class FakeTimeoutError(Exception):
    pass  # class name contains "Timeout" -> mapped to transient/timeout


class FakeMessages:
    def __init__(self, results):
        # results: list of (response | Exception). Last entry repeats.
        self._results = results
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._results) - 1)
        item = self._results[idx]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, results):
        self.messages = FakeMessages(results)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(llm, "_sleep", lambda seconds: None)


@pytest.fixture
def anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")


def _install_client(monkeypatch, results):
    client = FakeClient(results)
    monkeypatch.setattr(llm, "_new_anthropic_client", lambda api_key: client)
    return client


# --- happy paths ----------------------------------------------------


def test_generate_text_happy_path(monkeypatch, anthropic_key):
    client = _install_client(monkeypatch, [_response("Paris is the capital.")])

    result = llm.generate(system="be terse", prompt="capital of France?")

    assert result.text == "Paris is the capital."
    assert result.parsed is None
    assert result.model == "claude-sonnet-5"
    assert result.stop_reason == "end_turn"
    assert client.messages.calls[0]["model"] == "claude-sonnet-5"
    assert client.messages.calls[0]["messages"] == [
        {"role": "user", "content": "capital of France?"}
    ]


def test_generate_structured_output_happy_path(monkeypatch, anthropic_key):
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["title", "count"],
        "additionalProperties": False,
    }
    payload = json.dumps({"title": "Mitochondria", "count": 3})
    client = _install_client(monkeypatch, [_response(payload)])

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.parsed == {"title": "Mitochondria", "count": 3}
    assert client.messages.calls[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": schema}
    }


# --- config / auth -------------------------------------------------


@override_settings(LLM_PROVIDER="does-not-exist")
def test_unknown_provider_raises_config_error():
    with pytest.raises(llm.LLMConfigError) as exc:
        llm.generate(system="s", prompt="p")
    assert "does-not-exist" in str(exc.value)
    assert "anthropic" in str(exc.value)


def test_missing_api_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = _install_client(monkeypatch, [_response()])

    with pytest.raises(llm.LLMAuthError) as exc:
        llm.generate(system="s", prompt="p")

    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert called.messages.calls == []  # never reached the provider


def test_check_missing_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMAuthError):
        llm.check()


def test_rejected_api_key_maps_to_auth_error(monkeypatch, anthropic_key):
    client = _install_client(monkeypatch, [FakeAPIError(401)])

    with pytest.raises(llm.LLMAuthError):
        llm.generate(system="s", prompt="p")

    assert len(client.messages.calls) == 1  # auth errors are not retried


# --- error mapping + retries -------------------------------------


def test_rate_limit_retries_then_raises(monkeypatch, anthropic_key):
    client = _install_client(
        monkeypatch, [FakeAPIError(429, headers={"retry-after": "2"})]
    )

    with pytest.raises(llm.LLMRateLimitError) as exc:
        llm.generate(system="s", prompt="p")

    assert exc.value.retry_after == 2.0
    assert len(client.messages.calls) == llm.MAX_ATTEMPTS


def test_server_error_retried_then_transient(monkeypatch, anthropic_key):
    client = _install_client(monkeypatch, [FakeAPIError(503)])

    with pytest.raises(llm.LLMTransientError) as exc:
        llm.generate(system="s", prompt="p")

    assert exc.value.reason == "server_error"
    assert len(client.messages.calls) == llm.MAX_ATTEMPTS


def test_server_error_then_success_recovers(monkeypatch, anthropic_key):
    client = _install_client(
        monkeypatch, [FakeAPIError(500), _response("recovered")]
    )

    result = llm.generate(system="s", prompt="p")

    assert result.text == "recovered"
    assert len(client.messages.calls) == 2


def test_timeout_maps_to_transient_timeout(monkeypatch, anthropic_key):
    client = _install_client(monkeypatch, [FakeTimeoutError("timed out")])

    with pytest.raises(llm.LLMTransientError) as exc:
        llm.generate(system="s", prompt="p")

    assert exc.value.reason == "timeout"
    assert len(client.messages.calls) == llm.MAX_ATTEMPTS


def test_bad_request_not_retried(monkeypatch, anthropic_key):
    client = _install_client(monkeypatch, [FakeAPIError(400)])

    with pytest.raises(llm.LLMBadResponseError):
        llm.generate(system="s", prompt="p")

    assert len(client.messages.calls) == 1


# --- bad responses ----------------------------------------------


def test_malformed_structured_response_raises_bad_response(monkeypatch, anthropic_key):
    schema = {"type": "object", "properties": {}, "required": []}
    _install_client(monkeypatch, [_response("not json at all")])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p", response_format=schema)
    assert exc.value.reason == "malformed_json"


def test_structured_response_missing_required_field(monkeypatch, anthropic_key):
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    _install_client(monkeypatch, [_response(json.dumps({"other": 1}))])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p", response_format=schema)
    assert exc.value.reason == "schema_violation"


def test_refusal_stop_reason_raises_bad_response(monkeypatch, anthropic_key):
    _install_client(monkeypatch, [_response("", stop_reason="refusal")])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "refusal"


def test_truncated_response_raises_bad_response(monkeypatch, anthropic_key):
    _install_client(monkeypatch, [_response("half an answ", stop_reason="max_tokens")])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "truncated"


def test_empty_response_raises_bad_response(monkeypatch, anthropic_key):
    _install_client(monkeypatch, [_response("   ")])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "empty"


# --- constants + registry -------------------------------------


def test_tunables_are_named_constants():
    assert isinstance(llm.REQUEST_TIMEOUT_SECONDS, float)
    assert isinstance(llm.MAX_ATTEMPTS, int) and llm.MAX_ATTEMPTS >= 2
    assert isinstance(llm.RETRY_BACKOFF_BASE_SECONDS, float)


def test_provider_registry_is_keyed_by_provider_name():
    assert "anthropic" in llm._PROVIDERS
    assert llm.SUPPORTED_PROVIDERS == ("anthropic",)


@override_settings(LLM_MODEL="claude-opus-5")
def test_model_is_configuration(monkeypatch, anthropic_key):
    client = _install_client(monkeypatch, [_response("hi")])
    result = llm.generate(system="s", prompt="p")
    assert result.model == "claude-opus-5"
    assert client.messages.calls[0]["model"] == "claude-opus-5"
