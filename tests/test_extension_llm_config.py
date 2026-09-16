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


def catalog_models():
    """Curated models per extension-visible catalog entry (issue #129):
    derived from ``llm.PROVIDER_CATALOG`` - no independent duplicate."""
    return {
        entry["name"]: list(entry["curated_models"])
        for entry in llm_module.PROVIDER_CATALOG
        if entry["extension_visible"]
    }


def expected_names():
    """Order from llm.PROVIDER_CATALOG, minus any entry whose registry
    key is absent from _PROVIDERS (opencode-zen until #104)."""
    names = []
    for entry in llm_module.PROVIDER_CATALOG:
        if not entry["extension_visible"]:
            continue
        if entry["registry_key"] in llm_module._PROVIDERS:
            names.append(entry["name"])
    return names


def test_catalog_registry_and_curated_invariants():
    """Issue #129: every catalog entry's registry key exists in
    ``_PROVIDERS``, and every extension-exposed entry has a non-empty
    curated model list."""
    assert llm_module.PROVIDER_CATALOG
    for entry in llm_module.PROVIDER_CATALOG:
        assert entry["registry_key"] in llm_module._PROVIDERS
        if entry["extension_visible"]:
            assert entry["curated_models"]
    # Named OpenAI-compatible catalog entries stay in agreement with
    # _NAMED_OPENAI_COMPATIBLE_DEFAULTS.
    for name in ("grok", "openrouter", "opencode-zen"):
        entry = next(e for e in llm_module.PROVIDER_CATALOG if e["name"] == name)
        expected = llm_module._NAMED_OPENAI_COMPATIBLE_DEFAULTS[name]
        assert entry["base_url"] == expected["base_url"]
        assert entry["default_key_env_var"] == expected["api_key_env_var"]


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
    expected = catalog_models()
    assert llm_module.EXTENSION_LLM_CURATED_MODELS == expected
    resp = client.get(llm_config_url, **auth_header(token))
    by_name = {p["name"]: p["models"] for p in resp.json()["providers"]}
    for name, models in by_name.items():
        assert models == expected[name]


def test_opencode_zen_curated_models_are_chat_completions_ids(
    client, llm_config_url, token, monkeypatch
):
    """Issue #115: the ``opencode-zen`` curated list holds only ids that
    live on Zen's ``/v1/chat/completions`` route (checked 2026-09-16; none
    in the deprecated-models table)."""
    expected = ["kimi-k2.6", "glm-5.3", "deepseek-v4-pro"]
    entry = next(e for e in llm_module.PROVIDER_CATALOG if e["name"] == "opencode-zen")
    assert list(entry["curated_models"]) == expected
    for retired in ("claude-sonnet-4-5", "gpt-5.1", "grok-code"):
        for catalog_entry in llm_module.PROVIDER_CATALOG:
            assert retired not in catalog_entry["curated_models"]
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "sk-test-not-a-real-key")
    resp = client.get(llm_config_url, **auth_header(token))
    assert resp.status_code == 200
    zen = next(p for p in resp.json()["providers"] if p["name"] == "opencode-zen")
    assert zen["models"] == expected
    # Presence boolean only: no key material anywhere in the response.
    assert "sk-test-not-a-real-key" not in resp.content.decode()


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
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    resp = client.get(llm_config_url, **auth_header(token))
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()["providers"]]
    assert names == ["anthropic", "openai", "grok", "opencode-zen"]
    zen = resp.json()["providers"][-1]
    assert zen["models"] == catalog_models()["opencode-zen"]
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
