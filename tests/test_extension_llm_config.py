"""Tests for GET /api/extension/llm-config/ (issue #105).

Mirrors the auth/CORS pattern of ``tests/test_extension_api.py``: Django's
test ``Client`` cannot enforce real browser CORS, so these tests only
assert the ``Access-Control-*`` response headers are present/absent and
correctly valued.
"""

import pytest
from django.test import override_settings
from django.urls import reverse

from submissions import llm as llm_module
from submissions.extension_auth import mint_token

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


@pytest.fixture
def llm_config_url():
    return reverse("extension:llm_config")


def auth_header(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


EXPECTED_MODELS = {
    "anthropic": ["claude-sonnet-4-6", "claude-haiku-4-5"],
    "openai": ["gpt-4o", "gpt-4o-mini"],
    "grok": ["grok-4", "grok-3-mini"],
    "opencode-zen": ["claude-sonnet-4-5", "gpt-5.1", "grok-code"],
}


def expected_names():
    """Order from llm.EXTENSION_LLM_PROVIDER_ORDER, minus any name whose
    registry key is absent from _PROVIDERS (opencode-zen until #104)."""
    names = []
    for name in llm_module.EXTENSION_LLM_PROVIDER_ORDER:
        registry_key = llm_module.EXTENSION_LLM_REGISTRY_KEYS.get(name, name)
        if registry_key in llm_module._PROVIDERS:
            names.append(name)
    return names


def test_exact_shape_and_provider_order(client, llm_config_url, token):
    resp = client.get(llm_config_url, **auth_header(token))
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload.keys()) == {"providers", "default"}
    assert set(payload["default"].keys()) == {"provider", "model"}
    assert [p["name"] for p in payload["providers"]] == expected_names()
    for entry in payload["providers"]:
        assert set(entry.keys()) == {"name", "models", "key_configured"}
        assert isinstance(entry["key_configured"], bool)


def test_fixed_order_is_spec_order(client, llm_config_url, token):
    assert list(llm_module.EXTENSION_LLM_PROVIDER_ORDER) == [
        "anthropic",
        "openai",
        "grok",
        "opencode-zen",
    ]
    resp = client.get(llm_config_url, **auth_header(token))
    names = [p["name"] for p in resp.json()["providers"]]
    assert names == sorted(
        names, key=list(llm_module.EXTENSION_LLM_PROVIDER_ORDER).index
    )


def test_curated_models_match_spec_table(client, llm_config_url, token):
    assert llm_module.EXTENSION_LLM_CURATED_MODELS == EXPECTED_MODELS
    resp = client.get(llm_config_url, **auth_header(token))
    by_name = {p["name"]: p["models"] for p in resp.json()["providers"]}
    for name, models in by_name.items():
        assert models == EXPECTED_MODELS[name]


def test_forbidden_names_never_appear(client, llm_config_url, token):
    resp = client.get(llm_config_url, **auth_header(token))
    names = [p["name"] for p in resp.json()["providers"]]
    assert "openai-compatible" not in names
    assert "gemini" not in names
    assert "openrouter" not in names
    # "openai" is the only OpenAI entry.
    assert names.count("openai") == 1


def test_opencode_zen_skipped_when_unregistered(client, llm_config_url, token, monkeypatch):
    providers = dict(llm_module._PROVIDERS)
    providers.pop("opencode-zen", None)
    monkeypatch.setattr(llm_module, "_PROVIDERS", providers)
    resp = client.get(llm_config_url, **auth_header(token))
    names = [p["name"] for p in resp.json()["providers"]]
    assert "opencode-zen" not in names
    assert names == ["anthropic", "openai", "grok"]


def test_opencode_zen_appears_when_registered(client, llm_config_url, token, monkeypatch):
    providers = dict(llm_module._PROVIDERS)
    providers["opencode-zen"] = llm_module.OpenAICompatibleProvider
    monkeypatch.setattr(llm_module, "_PROVIDERS", providers)
    monkeypatch.setattr(
        llm_module,
        "_resolve_opencode_zen_api_key_env_var",
        lambda: "OPENCODE_ZEN_API_KEY",
        raising=False,
    )
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    resp = client.get(llm_config_url, **auth_header(token))
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()["providers"]]
    assert names == ["anthropic", "openai", "grok", "opencode-zen"]
    zen = resp.json()["providers"][-1]
    assert zen["models"] == EXPECTED_MODELS["opencode-zen"]
    assert zen["key_configured"] is False


def test_key_configured_true_false_per_provider(
    client, llm_config_url, token, monkeypatch, settings
):
    settings.LLM_API_KEY_ENV_VAR = "TEST_ANTHROPIC_KEY_105"
    settings.LLM_OPENAI_API_KEY_ENV_VAR = "TEST_OPENAI_KEY_105"
    settings.LLM_GROK_API_KEY_ENV_VAR = "TEST_GROK_KEY_105"
    assert llm_module._resolve_api_key_env_var() == "TEST_ANTHROPIC_KEY_105"
    assert llm_module._resolve_openai_api_key_env_var() == "TEST_OPENAI_KEY_105"
    assert llm_module._resolve_grok_api_key_env_var() == "TEST_GROK_KEY_105"

    monkeypatch.setenv("TEST_ANTHROPIC_KEY_105", "sk-ant-secret")
    monkeypatch.delenv("TEST_OPENAI_KEY_105", raising=False)
    monkeypatch.setenv("TEST_GROK_KEY_105", "   ")

    resp = client.get(llm_config_url, **auth_header(token))
    assert resp.status_code == 200
    by_name = {p["name"]: p["key_configured"] for p in resp.json()["providers"]}
    assert by_name["anthropic"] is True
    assert by_name["openai"] is False
    # Whitespace-only counts as unset (.strip() is falsy).
    assert by_name["grok"] is False
    # Presence boolean only: no key material anywhere in the response.
    assert "sk-ant-secret" not in resp.content.decode()


def test_no_provider_construction_or_network(client, llm_config_url, token, monkeypatch):
    monkeypatch.setattr(
        llm_module,
        "OpenAICompatibleProvider",
        None,
        raising=False,
    )
    resp = client.get(llm_config_url, **auth_header(token))
    assert resp.status_code == 200
    assert [p["name"] for p in resp.json()["providers"]] == expected_names()


def test_default_echoes_settings_verbatim(client, llm_config_url, token, settings):
    settings.LLM_PROVIDER = "anthropic"
    settings.LLM_MODEL = "claude-sonnet-4-6"
    resp = client.get(llm_config_url, **auth_header(token))
    assert resp.json()["default"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }


@override_settings(LLM_PROVIDER="bogus-provider", LLM_MODEL="some-unknown-model")
def test_unknown_default_provider_still_200_verbatim(client, llm_config_url, token):
    resp = client.get(llm_config_url, **auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["default"] == {
        "provider": "bogus-provider",
        "model": "some-unknown-model",
    }


def test_missing_auth_401(client, llm_config_url):
    resp = client.get(llm_config_url)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_wrong_token_401(client, llm_config_url, token):
    resp = client.get(llm_config_url, **auth_header("wrong-" + token))
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_non_get_405(client, llm_config_url, token):
    resp = client.post(llm_config_url, **auth_header(token))
    assert resp.status_code == 405


def test_options_preflight(client, llm_config_url):
    resp = client.options(llm_config_url)
    assert resp.status_code == 200
    assert not resp.content
    assert resp["Access-Control-Allow-Origin"] == "chrome-extension://abcdefghijklmnop"
    assert resp["Access-Control-Allow-Methods"] == "GET, OPTIONS"
    assert resp["Access-Control-Allow-Headers"] == "Authorization, Content-Type"


def test_cors_header_present_when_extension_id_set(client, llm_config_url, token):
    resp = client.get(llm_config_url, **auth_header(token))
    assert resp["Access-Control-Allow-Origin"] == "chrome-extension://abcdefghijklmnop"


def test_cors_header_absent_when_extension_id_unset(
    client, llm_config_url, token, settings
):
    settings.EXTENSION_ID = ""
    resp = client.get(llm_config_url, **auth_header(token))
    assert "Access-Control-Allow-Origin" not in resp
