"""Tests for the provider-agnostic LLM client (``submissions/llm.py``).

The Anthropic SDK / HTTP layer is stubbed with ``monkeypatch`` - these tests
import no ``anthropic`` symbols, make no network calls, and pass with
``ANTHROPIC_API_KEY`` unset.
"""

import json
import types

import httpx
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
    from submissions.models import LLMCall

    with pytest.raises(llm.LLMConfigError) as exc:
        llm.generate(system="s", prompt="p")
    assert "does-not-exist" in str(exc.value)
    assert "anthropic" in str(exc.value)

    # No adapter exists to ask for a canonical name, so the failed row
    # records the normalized, attempted provider name (issue #91).
    row = LLMCall.objects.get()
    assert row.provider == "does-not-exist"


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


def test_fenced_structured_response_parses_without_retry(monkeypatch, anthropic_key):
    schema = {"type": "object", "properties": {}, "required": []}
    fenced = "```json\n" + json.dumps({"a": 1}) + "\n```"
    client = _install_client(monkeypatch, [_response(fenced)])

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.parsed == {"a": 1}
    assert len(client.messages.calls) == 1


def test_unlabeled_fenced_structured_response_parses(monkeypatch, anthropic_key):
    schema = {"type": "object", "properties": {}, "required": []}
    fenced = "```\n" + json.dumps({"a": 1}) + "\n```"
    client = _install_client(monkeypatch, [_response(fenced)])

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.parsed == {"a": 1}
    assert len(client.messages.calls) == 1


def test_malformed_json_retries_once_then_succeeds(monkeypatch, anthropic_key):
    schema = {"type": "object", "properties": {}, "required": []}
    client = _install_client(
        monkeypatch,
        [_response("not json at all"), _response(json.dumps({"a": 1}))],
    )

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.parsed == {"a": 1}
    assert len(client.messages.calls) == 2
    second_call_content = client.messages.calls[1]["messages"][0]["content"]
    assert llm.JSON_RETRY_INSTRUCTION in second_call_content
    assert "not json at all" in second_call_content


def test_schema_violation_retries_once_then_succeeds(monkeypatch, anthropic_key):
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    client = _install_client(
        monkeypatch,
        [
            _response(json.dumps({"other": 1})),
            _response(json.dumps({"title": "ok"})),
        ],
    )

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.parsed == {"title": "ok"}
    assert len(client.messages.calls) == 2


def test_malformed_json_on_retry_too_raises(monkeypatch, anthropic_key):
    schema = {"type": "object", "properties": {}, "required": []}
    client = _install_client(
        monkeypatch, [_response("still not json"), _response("still not json")]
    )

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p", response_format=schema)

    assert exc.value.reason == "malformed_json"
    assert len(client.messages.calls) == 2


def test_json_retry_records_only_successful_call_usage(monkeypatch, anthropic_key):
    from submissions.models import LLMCall

    schema = {"type": "object", "properties": {}, "required": []}
    bad = types.SimpleNamespace(
        content=[_text_block("nope")],
        stop_reason="end_turn",
        usage=types.SimpleNamespace(input_tokens=100, output_tokens=100),
    )
    good = types.SimpleNamespace(
        content=[_text_block(json.dumps({"a": 1}))],
        stop_reason="end_turn",
        usage=types.SimpleNamespace(input_tokens=7, output_tokens=3),
    )
    _install_client(monkeypatch, [bad, good])

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.usage == {"input_tokens": 7, "output_tokens": 3}
    row = LLMCall.objects.get()
    assert row.prompt_tokens == 7
    assert row.completion_tokens == 3


def test_transient_error_on_json_retry_recovers(monkeypatch, anthropic_key):
    schema = {"type": "object", "properties": {}, "required": []}
    client = _install_client(
        monkeypatch,
        [
            _response("not json at all"),
            FakeAPIError(500),
            _response(json.dumps({"a": 1})),
        ],
    )

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.parsed == {"a": 1}
    assert len(client.messages.calls) == 3


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
    assert "openai-compatible" in llm._PROVIDERS
    assert "openai" in llm._PROVIDERS
    assert "grok" in llm._PROVIDERS
    assert "openrouter" in llm._PROVIDERS
    assert "gemini" in llm._PROVIDERS
    assert llm.SUPPORTED_PROVIDERS == (
        "anthropic",
        "gemini",
        "grok",
        "openai",
        "openai-compatible",
        "openrouter",
    )


@override_settings(LLM_MODEL="claude-opus-5")
def test_model_is_configuration(monkeypatch, anthropic_key):
    client = _install_client(monkeypatch, [_response("hi")])
    result = llm.generate(system="s", prompt="p")
    assert result.model == "claude-opus-5"
    assert client.messages.calls[0]["model"] == "claude-opus-5"


# --- OpenAI-compatible adapter (issue #27) -------------------------------
# Network stubbed via _new_openai_client, mirroring the Anthropic fakes
# above: no openai import, no network, key via monkeypatched env.


def _openai_response(
    text="hello",
    finish_reason="stop",
    refusal=None,
    in_tok=10,
    out_tok=5,
):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=text, refusal=refusal),
                finish_reason=finish_reason,
            )
        ],
        usage=types.SimpleNamespace(
            prompt_tokens=in_tok, completion_tokens=out_tok
        ),
    )


class FakeCompletions:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._results) - 1)
        item = self._results[idx]
        if isinstance(item, Exception):
            raise item
        return item


class FakeChat:
    def __init__(self, results):
        self.completions = FakeCompletions(results)


class FakeOpenAIClient:
    def __init__(self, results):
        self.chat = FakeChat(results)


@pytest.fixture
def openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")


def _install_openai_client(monkeypatch, results, seen=None):
    client = FakeOpenAIClient(results)

    def _factory(api_key, base_url):
        if seen is not None:
            seen.update(api_key=api_key, base_url=base_url)
        return client

    monkeypatch.setattr(llm, "_new_openai_client", _factory)
    return client


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_MODEL="gpt-4o-mini",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_text_happy_path(monkeypatch, openai_key):
    seen: dict = {}
    client = _install_openai_client(monkeypatch, [_openai_response("hi")], seen)

    result = llm.generate(system="be terse", prompt="say hi")

    assert result.text == "hi"
    assert result.parsed is None
    assert result.model == "gpt-4o-mini"
    assert result.stop_reason == "stop"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}
    call = client.chat.completions.calls[0]
    assert call["model"] == "gpt-4o-mini"
    assert call["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "say hi"},
    ]
    assert "response_format" not in call
    assert seen["api_key"] == "sk-test-not-a-real-key"
    assert seen["base_url"] == "https://api.openai.com/v1"


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_MODEL="gpt-4o-mini",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_structured_output_happy_path(monkeypatch, openai_key):
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["title", "count"],
        "additionalProperties": False,
    }
    payload = json.dumps({"title": "Mitochondria", "count": 3})
    client = _install_openai_client(monkeypatch, [_openai_response(payload)])

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.parsed == {"title": "Mitochondria", "count": 3}
    assert client.chat.completions.calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "response", "schema": schema},
    }


@override_settings(LLM_PROVIDER="openai")
def test_openai_alias_routes_to_openai_adapter(monkeypatch, anthropic_key):
    from submissions.models import LLMCall

    client = _install_openai_client(monkeypatch, [_openai_response("hi")])
    result = llm.generate(system="s", prompt="p")
    assert result.text == "hi"
    assert client.chat.completions.calls[0]["model"] == "claude-sonnet-5"

    # The row records the adapter's canonical name, not the "openai" alias
    # used to select it (issue #91).
    row = LLMCall.objects.get()
    assert row.provider == "openai-compatible"


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_OPENAI_BASE_URL="http://127.0.0.1:11434/v1",
    LLM_OPENAI_MODEL="qwen3:8b",
    LLM_OPENAI_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_base_url_model_and_key_env_var_are_configuration(
    monkeypatch, openai_key
):
    seen: dict = {}
    client = _install_openai_client(monkeypatch, [_openai_response("hi")], seen)

    result = llm.generate(system="s", prompt="p")

    assert result.model == "qwen3:8b"
    assert client.chat.completions.calls[0]["model"] == "qwen3:8b"
    assert seen["base_url"] == "http://127.0.0.1:11434/v1"
    assert seen["api_key"] == "sk-test-not-a-real-key"


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_MODEL="gpt-4o-mini",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_missing_api_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    installed = _install_openai_client(monkeypatch, [_openai_response()])

    with pytest.raises(llm.LLMAuthError) as exc:
        llm.generate(system="s", prompt="p")

    assert "OPENAI_API_KEY" in str(exc.value)
    assert installed.chat.completions.calls == []


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_rejected_api_key_maps_to_auth_error(monkeypatch, openai_key):
    client = _install_openai_client(monkeypatch, [FakeAPIError(401)])

    with pytest.raises(llm.LLMAuthError):
        llm.generate(system="s", prompt="p")

    assert len(client.chat.completions.calls) == 1


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_rate_limit_retries_then_raises(monkeypatch, openai_key):
    client = _install_openai_client(
        monkeypatch, [FakeAPIError(429, headers={"retry-after": "2"})]
    )

    with pytest.raises(llm.LLMRateLimitError) as exc:
        llm.generate(system="s", prompt="p")

    assert exc.value.retry_after == 2.0
    assert len(client.chat.completions.calls) == llm.MAX_ATTEMPTS


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_server_error_retried_then_transient(monkeypatch, openai_key):
    client = _install_openai_client(monkeypatch, [FakeAPIError(503)])

    with pytest.raises(llm.LLMTransientError) as exc:
        llm.generate(system="s", prompt="p")

    assert exc.value.reason == "server_error"
    assert len(client.chat.completions.calls) == llm.MAX_ATTEMPTS


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_server_error_then_success_recovers(monkeypatch, openai_key):
    client = _install_openai_client(
        monkeypatch, [FakeAPIError(500), _openai_response("recovered")]
    )

    result = llm.generate(system="s", prompt="p")

    assert result.text == "recovered"
    assert len(client.chat.completions.calls) == 2


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_timeout_maps_to_transient_timeout(monkeypatch, openai_key):
    client = _install_openai_client(monkeypatch, [FakeTimeoutError("timed out")])

    with pytest.raises(llm.LLMTransientError) as exc:
        llm.generate(system="s", prompt="p")

    assert exc.value.reason == "timeout"
    assert len(client.chat.completions.calls) == llm.MAX_ATTEMPTS


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_bad_request_not_retried(monkeypatch, openai_key):
    client = _install_openai_client(monkeypatch, [FakeAPIError(400)])

    with pytest.raises(llm.LLMBadResponseError):
        llm.generate(system="s", prompt="p")

    assert len(client.chat.completions.calls) == 1


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_malformed_structured_response(monkeypatch, openai_key):
    schema = {"type": "object", "properties": {}, "required": []}
    _install_openai_client(monkeypatch, [_openai_response("not json at all")])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p", response_format=schema)
    assert exc.value.reason == "malformed_json"


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_schema_violation(monkeypatch, openai_key):
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    _install_openai_client(
        monkeypatch, [_openai_response(json.dumps({"other": 1}))]
    )

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p", response_format=schema)
    assert exc.value.reason == "schema_violation"


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_malformed_json_retries_once_then_succeeds(monkeypatch, openai_key):
    schema = {"type": "object", "properties": {}, "required": []}
    client = _install_openai_client(
        monkeypatch,
        [
            _openai_response("not json at all"),
            _openai_response(json.dumps({"a": 1})),
        ],
    )

    result = llm.generate(system="s", prompt="p", response_format=schema)

    assert result.parsed == {"a": 1}
    assert len(client.chat.completions.calls) == 2
    second_call_messages = client.chat.completions.calls[1]["messages"]
    user_content = second_call_messages[-1]["content"]
    assert llm.JSON_RETRY_INSTRUCTION in user_content
    assert "not json at all" in user_content


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_malformed_json_on_retry_too_raises(monkeypatch, openai_key):
    schema = {"type": "object", "properties": {}, "required": []}
    client = _install_openai_client(
        monkeypatch,
        [_openai_response("still not json"), _openai_response("still not json")],
    )

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p", response_format=schema)

    assert exc.value.reason == "malformed_json"
    assert len(client.chat.completions.calls) == 2


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
@pytest.mark.parametrize(
    "response",
    [
        _openai_response("", finish_reason="refusal"),
        _openai_response("no", refusal="refused for policy"),
        _openai_response("no", finish_reason="content_filter"),
    ],
)
def test_openai_refusal_raises_bad_response(monkeypatch, openai_key, response):
    _install_openai_client(monkeypatch, [response])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "refusal"


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_truncated_response_raises_bad_response(monkeypatch, openai_key):
    _install_openai_client(
        monkeypatch, [_openai_response("half an answ", finish_reason="length")]
    )

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "truncated"


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_empty_response_raises_bad_response(monkeypatch, openai_key):
    _install_openai_client(monkeypatch, [_openai_response("   ")])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "empty"


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_MODEL="gpt-4o-mini",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_success_records_llm_call_row(monkeypatch, openai_key):
    from submissions.models import LLMCall

    _install_openai_client(
        monkeypatch, [_openai_response("hi", in_tok=12, out_tok=7)]
    )

    result = llm.generate(system="s", prompt="p")
    assert result.text == "hi"

    row = LLMCall.objects.get()
    assert row.status == LLMCall.Status.OK
    assert row.model == "gpt-4o-mini"
    assert row.provider == "openai-compatible"
    assert (row.prompt_tokens, row.completion_tokens) == (12, 7)
    assert row.estimated_cost_usd == llm.estimate_cost_usd(
        "gpt-4o-mini", 12, 7
    )


@override_settings(
    LLM_PROVIDER="openai-compatible",
    LLM_API_KEY_ENV_VAR="OPENAI_API_KEY",
)
def test_openai_failed_call_records_llm_call_row_with_provider(
    monkeypatch, openai_key
):
    from submissions.models import LLMCall

    _install_openai_client(monkeypatch, [FakeAPIError(401)])

    with pytest.raises(llm.LLMAuthError):
        llm.generate(system="s", prompt="p")

    row = LLMCall.objects.get()
    assert row.status == LLMCall.Status.FAILED
    assert row.provider == "openai-compatible"


# --- cross-provider contract -------------------------------------------
# The same behavioural contract, stubbed transport, both adapters: if one
# provider drifts, this is where it shows.


def _contract_settings(provider_name):
    if provider_name == "anthropic":
        return {"LLM_PROVIDER": "anthropic"}
    return {
        "LLM_PROVIDER": provider_name,
        "LLM_API_KEY_ENV_VAR": "OPENAI_API_KEY",
    }


@pytest.mark.parametrize("provider_name", ["anthropic", "openai-compatible"])
def test_contract_text_round_trip(monkeypatch, provider_name):
    from django.test import override_settings as _override

    settings = _contract_settings(provider_name)
    with _override(**settings):
        if provider_name == "anthropic":
            monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
            client = _install_client(monkeypatch, [_response("Paris.")])
            result = llm.generate(system="be terse", prompt="capital?")
            assert result.text == "Paris."
            assert result.usage == {"input_tokens": 10, "output_tokens": 5}
            assert client.messages.calls[0]["messages"] == [
                {"role": "user", "content": "capital?"}
            ]
        else:
            monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
            client = _install_openai_client(
                monkeypatch, [_openai_response("Paris.")]
            )
            result = llm.generate(system="be terse", prompt="capital?")
            assert result.text == "Paris."
            assert result.usage == {"input_tokens": 10, "output_tokens": 5}
            assert client.chat.completions.calls[0]["messages"] == [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "capital?"},
            ]


@pytest.mark.parametrize("provider_name", ["anthropic", "openai-compatible"])
def test_contract_card_list_schema_round_trip(monkeypatch, provider_name):
    """The #6 card-list output contract parses through either adapter."""
    from django.test import override_settings as _override

    from submissions.generation import CARD_LIST_SCHEMA

    payload = json.dumps(
        {
            "cards": [
                {
                    "note_type": "basic",
                    "front": "What is X?",
                    "back": "X is Y.",
                    "source_term": "X",
                    "topic": "T",
                }
            ]
        }
    )
    with _override(**_contract_settings(provider_name)):
        if provider_name == "anthropic":
            monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
            _install_client(monkeypatch, [_response(payload)])
        else:
            monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
            _install_openai_client(monkeypatch, [_openai_response(payload)])
        result = llm.generate(
            system="s", prompt="p", response_format=CARD_LIST_SCHEMA
        )
    assert result.parsed["cards"][0]["front"] == "What is X?"


@pytest.mark.parametrize("provider_name", ["anthropic", "openai-compatible"])
def test_contract_error_taxonomy(monkeypatch, provider_name):
    """Auth/rate-limit/5xx/timeout/malformed map to the same exceptions."""
    from django.test import override_settings as _override

    cases = [
        (FakeAPIError(401), llm.LLMAuthError),
        (FakeAPIError(429), llm.LLMRateLimitError),
        (FakeAPIError(503), llm.LLMTransientError),
        (FakeTimeoutError("timed out"), llm.LLMTransientError),
        (FakeAPIError(400), llm.LLMBadResponseError),
    ]
    with _override(**_contract_settings(provider_name)):
        for exc, expected in cases:
            if provider_name == "anthropic":
                monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
                _install_client(monkeypatch, [exc])
            else:
                monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
                _install_openai_client(monkeypatch, [exc])
            with pytest.raises(expected):
                llm.generate(system="s", prompt="p")


# --- named OpenAI-compatible providers: grok / openrouter (issues #84, #98) -
# These are OpenAICompatibleProvider with a per-provider base_url/model/
# api_key_env_var: an LLM_GROK_*/LLM_OPENROUTER_* override wins when set,
# and an empty/unset override falls back to the hardcoded default
# (base_url/api_key_env_var) or the generic LLM_MODEL setting (model).


def test_get_provider_grok_builds_openai_compatible_with_hardcoded_defaults():
    provider = llm.get_provider("grok")

    assert isinstance(provider, llm.OpenAICompatibleProvider)
    assert provider.base_url == "https://api.x.ai/v1"
    assert provider.api_key_env_var == "XAI_API_KEY"
    assert provider.model == "claude-sonnet-5"  # the default LLM_MODEL


def test_get_provider_openrouter_builds_openai_compatible_with_hardcoded_defaults():
    provider = llm.get_provider("openrouter")

    assert isinstance(provider, llm.OpenAICompatibleProvider)
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.api_key_env_var == "OPENROUTER_API_KEY"
    assert provider.model == "claude-sonnet-5"  # the default LLM_MODEL


@override_settings(LLM_MODEL="grok-4")
def test_get_provider_grok_model_is_configuration_not_hardcoded():
    provider = llm.get_provider("grok")
    assert provider.model == "grok-4"


@override_settings(LLM_MODEL="anthropic/claude-3.5-sonnet")
def test_get_provider_openrouter_model_is_configuration_not_hardcoded():
    provider = llm.get_provider("openrouter")
    assert provider.model == "anthropic/claude-3.5-sonnet"


def test_get_provider_grok_overrides_all_three_settings():
    with override_settings(
        LLM_GROK_BASE_URL="http://127.0.0.1:11434/v1",
        LLM_GROK_MODEL="grok-code",
        LLM_GROK_API_KEY_ENV_VAR="MY_XAI_KEY",
    ):
        provider = llm.get_provider("grok")

    assert provider.base_url == "http://127.0.0.1:11434/v1"
    assert provider.model == "grok-code"
    assert provider.api_key_env_var == "MY_XAI_KEY"


def test_get_provider_grok_falls_back_to_hardcoded_defaults_when_blank():
    with override_settings(
        LLM_MODEL="claude-sonnet-5",
        LLM_GROK_BASE_URL="",
        LLM_GROK_MODEL="",
        LLM_GROK_API_KEY_ENV_VAR="",
    ):
        provider = llm.get_provider("grok")

    assert provider.base_url == "https://api.x.ai/v1"
    assert provider.model == "claude-sonnet-5"
    assert provider.api_key_env_var == "XAI_API_KEY"


def test_get_provider_openrouter_overrides_all_three_settings():
    with override_settings(
        LLM_OPENROUTER_BASE_URL="http://127.0.0.1:11434/v1",
        LLM_OPENROUTER_MODEL="openai/gpt-4o",
        LLM_OPENROUTER_API_KEY_ENV_VAR="MY_OPENROUTER_KEY",
    ):
        provider = llm.get_provider("openrouter")

    assert provider.base_url == "http://127.0.0.1:11434/v1"
    assert provider.model == "openai/gpt-4o"
    assert provider.api_key_env_var == "MY_OPENROUTER_KEY"


def test_get_provider_openrouter_falls_back_to_hardcoded_defaults_when_blank():
    with override_settings(
        LLM_MODEL="claude-sonnet-5",
        LLM_OPENROUTER_BASE_URL="",
        LLM_OPENROUTER_MODEL="",
        LLM_OPENROUTER_API_KEY_ENV_VAR="",
    ):
        provider = llm.get_provider("openrouter")

    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.model == "claude-sonnet-5"
    assert provider.api_key_env_var == "OPENROUTER_API_KEY"


def test_grok_override_settings_do_not_affect_openrouter():
    """Each provider's overrides are independent - setting one provider's
    override must not leak into the other's resolution."""
    with override_settings(
        LLM_GROK_BASE_URL="http://grok-only.example/v1",
        LLM_GROK_MODEL="grok-only-model",
        LLM_GROK_API_KEY_ENV_VAR="GROK_ONLY_KEY",
    ):
        openrouter = llm.get_provider("openrouter")

    assert openrouter.base_url == "https://openrouter.ai/api/v1"
    assert openrouter.model == "claude-sonnet-5"
    assert openrouter.api_key_env_var == "OPENROUTER_API_KEY"


def test_openrouter_override_settings_do_not_affect_grok():
    with override_settings(
        LLM_OPENROUTER_BASE_URL="http://openrouter-only.example/v1",
        LLM_OPENROUTER_MODEL="openrouter-only-model",
        LLM_OPENROUTER_API_KEY_ENV_VAR="OPENROUTER_ONLY_KEY",
    ):
        grok = llm.get_provider("grok")

    assert grok.base_url == "https://api.x.ai/v1"
    assert grok.model == "claude-sonnet-5"
    assert grok.api_key_env_var == "XAI_API_KEY"


def test_grok_and_openrouter_ignore_openai_override_settings():
    """Adding grok/openrouter must not route through the openai/
    openai-compatible branch or pick up its override settings."""
    with override_settings(
        LLM_OPENAI_BASE_URL="http://should-not-apply.example/v1",
        LLM_OPENAI_MODEL="should-not-apply",
        LLM_OPENAI_API_KEY_ENV_VAR="SHOULD_NOT_APPLY",
    ):
        grok = llm.get_provider("grok")
        openrouter = llm.get_provider("openrouter")

    assert grok.base_url == "https://api.x.ai/v1"
    assert grok.api_key_env_var == "XAI_API_KEY"
    assert grok.model == "claude-sonnet-5"
    assert openrouter.base_url == "https://openrouter.ai/api/v1"
    assert openrouter.api_key_env_var == "OPENROUTER_API_KEY"
    assert openrouter.model == "claude-sonnet-5"


@override_settings(LLM_PROVIDER="grok", LLM_MODEL="grok-4")
def test_grok_end_to_end_generate_happy_path(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "sk-test-not-a-real-key")
    seen: dict = {}
    client = _install_openai_client(monkeypatch, [_openai_response("hi")], seen)

    result = llm.generate(system="be terse", prompt="say hi")

    assert result.text == "hi"
    assert result.model == "grok-4"
    assert seen["base_url"] == "https://api.x.ai/v1"
    assert seen["api_key"] == "sk-test-not-a-real-key"

    from submissions.models import LLMCall

    row = LLMCall.objects.get()
    assert row.provider == "openai-compatible"


@override_settings(LLM_PROVIDER="openrouter", LLM_MODEL="openai/gpt-4o")
def test_openrouter_end_to_end_generate_happy_path(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-a-real-key")
    seen: dict = {}
    client = _install_openai_client(monkeypatch, [_openai_response("hi")], seen)

    result = llm.generate(system="be terse", prompt="say hi")

    assert result.text == "hi"
    assert result.model == "openai/gpt-4o"
    assert seen["base_url"] == "https://openrouter.ai/api/v1"
    assert seen["api_key"] == "sk-test-not-a-real-key"


def test_unknown_provider_still_unaffected_by_grok_openrouter_additions():
    """grok/openrouter are additions to _PROVIDERS, not replacements - the
    existing unknown-provider-name error path is unaffected."""
    with override_settings(LLM_PROVIDER="not-a-real-provider"):
        with pytest.raises(llm.LLMConfigError) as exc:
            llm.get_provider()
    assert "not-a-real-provider" in str(exc.value)
    for name in ("anthropic", "openai", "openai-compatible", "grok", "openrouter"):
        assert name in str(exc.value)


# --- Gemini adapter (issue #83) ---------------------------------------
# Network stubbed via _gemini_request, mirroring the Anthropic/OpenAI fakes
# above: no real httpx.Client instantiated, no network call. Responses are
# real httpx.Response objects so raise_for_status()/`.json()` in the
# production code paths exercise their real behaviour.

_GEMINI_REQUEST = httpx.Request("POST", "https://generativelanguage.googleapis.com/")


def _gemini_response(
    text="hello",
    finish_reason="STOP",
    in_tok=10,
    out_tok=5,
    candidates=None,
    include_usage=True,
):
    if candidates is None:
        candidates = [
            {
                "content": {"parts": [{"text": text}]},
                "finishReason": finish_reason,
            }
        ]
    body: dict = {"candidates": candidates}
    if include_usage:
        body["usageMetadata"] = {
            "promptTokenCount": in_tok,
            "candidatesTokenCount": out_tok,
        }
    return httpx.Response(200, json=body, request=_GEMINI_REQUEST)


def _gemini_error_response(status_code, error_status=None, error_message="", headers=None):
    body = {}
    if error_status is not None or error_message:
        body = {"error": {"status": error_status, "message": error_message}}
    return httpx.Response(
        status_code, json=body, request=_GEMINI_REQUEST, headers=headers or {}
    )


def _install_gemini_request(monkeypatch, results, seen=None):
    calls = []

    def _fake(*, url, headers, json_body):
        calls.append({"url": url, "headers": headers, "json_body": json_body})
        idx = min(len(calls) - 1, len(results) - 1)
        item = results[idx]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm, "_gemini_request", _fake)
    if seen is not None:
        seen["calls"] = calls
    return calls


@pytest.fixture
def gemini_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-test-not-a-real-key")


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_MODEL="gemini-2.5-flash",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_text_happy_path(monkeypatch, gemini_key):
    calls = _install_gemini_request(monkeypatch, [_gemini_response("hi")])

    result = llm.generate(system="be terse", prompt="say hi")

    assert result.text == "hi"
    assert result.parsed is None
    assert result.model == "gemini-2.5-flash"
    assert result.stop_reason == "STOP"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    call = calls[0]
    assert call["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-2.5-flash:generateContent"
    )
    assert call["headers"]["x-goog-api-key"] == "sk-test-not-a-real-key"
    assert "key" not in call["url"]  # never the ?key= query param (issue #83)
    assert call["json_body"]["systemInstruction"] == {
        "parts": [{"text": "be terse"}]
    }
    assert call["json_body"]["contents"] == [
        {"role": "user", "parts": [{"text": "say hi"}]}
    ]
    assert call["json_body"]["generationConfig"]["maxOutputTokens"] == 4096
    assert "responseMimeType" not in call["json_body"]["generationConfig"]


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_structured_output_happy_path(monkeypatch, gemini_key):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    payload = json.dumps({"answer": "Paris"})
    calls = _install_gemini_request(monkeypatch, [_gemini_response(payload)])

    result = llm.generate(
        system="s", prompt="capital of France?", response_format=schema
    )

    assert result.parsed == {"answer": "Paris"}
    config = calls[0]["json_body"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"] == schema


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_truncated_response_raises_bad_response(monkeypatch, gemini_key):
    _install_gemini_request(
        monkeypatch, [_gemini_response("half an answ", finish_reason="MAX_TOKENS")]
    )

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "truncated"


@pytest.mark.parametrize(
    "finish_reason",
    ["SAFETY", "RECITATION", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"],
)
@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_refusal_finish_reasons_raise_bad_response(
    monkeypatch, gemini_key, finish_reason
):
    _install_gemini_request(
        monkeypatch, [_gemini_response("", finish_reason=finish_reason)]
    )

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "refusal"


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_empty_candidates_raises_bad_response(monkeypatch, gemini_key):
    _install_gemini_request(monkeypatch, [_gemini_response(candidates=[])])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "empty"


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_missing_usage_metadata_yields_empty_usage(monkeypatch, gemini_key):
    calls = _install_gemini_request(
        monkeypatch, [_gemini_response("hi", include_usage=False)]
    )

    result = llm.generate(system="s", prompt="p")

    assert result.usage == {}
    assert len(calls) == 1


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_missing_api_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    calls = _install_gemini_request(monkeypatch, [_gemini_response()])

    with pytest.raises(llm.LLMAuthError) as exc:
        llm.generate(system="s", prompt="p")

    assert "GOOGLE_API_KEY" in str(exc.value)
    assert calls == []


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_403_maps_to_auth_error(monkeypatch, gemini_key):
    calls = _install_gemini_request(monkeypatch, [_gemini_error_response(403)])

    with pytest.raises(llm.LLMAuthError):
        llm.generate(system="s", prompt="p")
    assert len(calls) == 1  # not retried - auth errors are not transient


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_400_permission_denied_maps_to_auth_error(monkeypatch, gemini_key):
    _install_gemini_request(
        monkeypatch,
        [_gemini_error_response(400, error_status="PERMISSION_DENIED")],
    )

    with pytest.raises(llm.LLMAuthError):
        llm.generate(system="s", prompt="p")


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_400_invalid_api_key_maps_to_auth_error(monkeypatch, gemini_key):
    _install_gemini_request(
        monkeypatch,
        [
            _gemini_error_response(
                400,
                error_status="INVALID_ARGUMENT",
                error_message="API key not valid. Please pass a valid API key.",
            )
        ],
    )

    with pytest.raises(llm.LLMAuthError):
        llm.generate(system="s", prompt="p")


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_generic_400_raises_bad_response(monkeypatch, gemini_key):
    _install_gemini_request(
        monkeypatch,
        [
            _gemini_error_response(
                400,
                error_status="INVALID_ARGUMENT",
                error_message="request body is malformed",
            )
        ],
    )

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "bad_request"


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_400_with_no_body_raises_bad_response(monkeypatch, gemini_key):
    _install_gemini_request(monkeypatch, [_gemini_error_response(400)])

    with pytest.raises(llm.LLMBadResponseError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "bad_request"


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_429_retries_then_raises_with_retry_after(monkeypatch, gemini_key):
    calls = _install_gemini_request(
        monkeypatch,
        [_gemini_error_response(429, headers={"retry-after": "3"})],
    )

    with pytest.raises(llm.LLMRateLimitError) as exc:
        llm.generate(system="s", prompt="p")

    assert exc.value.retry_after == 3.0
    assert len(calls) == llm.MAX_ATTEMPTS


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_429_without_retry_after(monkeypatch, gemini_key):
    _install_gemini_request(monkeypatch, [_gemini_error_response(429)])

    with pytest.raises(llm.LLMRateLimitError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.retry_after is None


@pytest.mark.parametrize("status_code", [500, 503])
@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_server_error_retried_then_transient(monkeypatch, gemini_key, status_code):
    calls = _install_gemini_request(
        monkeypatch, [_gemini_error_response(status_code)]
    )

    with pytest.raises(llm.LLMTransientError) as exc:
        llm.generate(system="s", prompt="p")

    assert exc.value.reason == "server_error"
    assert len(calls) == llm.MAX_ATTEMPTS


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_server_error_then_success_recovers(monkeypatch, gemini_key):
    calls = _install_gemini_request(
        monkeypatch, [_gemini_error_response(500), _gemini_response("recovered")]
    )

    result = llm.generate(system="s", prompt="p")

    assert result.text == "recovered"
    assert len(calls) == 2


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_timeout_maps_to_transient_timeout(monkeypatch, gemini_key):
    _install_gemini_request(monkeypatch, [httpx.TimeoutException("timed out")])

    with pytest.raises(llm.LLMTransientError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "timeout"


@override_settings(
    LLM_PROVIDER="gemini",
    LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
)
def test_gemini_connect_error_maps_to_transient_connection(monkeypatch, gemini_key):
    _install_gemini_request(monkeypatch, [httpx.ConnectError("connection refused")])

    with pytest.raises(llm.LLMTransientError) as exc:
        llm.generate(system="s", prompt="p")
    assert exc.value.reason == "connection"


def test_get_provider_gemini_model_and_key_env_var_overrides():
    with override_settings(
        LLM_MODEL="gemini-generic",
        LLM_API_KEY_ENV_VAR="GENERIC_KEY",
        LLM_GEMINI_MODEL="gemini-2.5-pro",
        LLM_GEMINI_API_KEY_ENV_VAR="GOOGLE_API_KEY",
    ):
        provider = llm.get_provider("gemini")

    assert isinstance(provider, llm.GeminiProvider)
    assert provider.model == "gemini-2.5-pro"
    assert provider.api_key_env_var == "GOOGLE_API_KEY"


def test_get_provider_gemini_falls_back_to_generic_settings_when_blank():
    with override_settings(
        LLM_MODEL="gemini-generic",
        LLM_API_KEY_ENV_VAR="GENERIC_KEY",
        LLM_GEMINI_MODEL="",
        LLM_GEMINI_API_KEY_ENV_VAR="",
    ):
        provider = llm.get_provider("gemini")

    assert provider.model == "gemini-generic"
    assert provider.api_key_env_var == "GENERIC_KEY"


def test_gemini_success_records_llm_call_row(monkeypatch, gemini_key):
    with override_settings(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemini-2.5-flash",
        LLM_API_KEY_ENV_VAR="GOOGLE_API_KEY",
    ):
        _install_gemini_request(
            monkeypatch, [_gemini_response("hi", in_tok=12, out_tok=7)]
        )
        llm.generate(system="s", prompt="p")

    from submissions.models import LLMCall

    row = LLMCall.objects.get()
    assert row.provider == "gemini"
    assert row.status == "ok"
    assert row.prompt_tokens == 12
    assert row.completion_tokens == 7
