"""Static checks for the extension popup LLM selector (#107/#108/#109).

The popup runs in a real browser (see _docs/extension_manual_checklist.md),
outside what pytest can execute — these tests pin the markup/CSS/JS
contracts (element ids, endpoint, storage key, banner copy, fail-open
wiring) so backend-side refactors can't silently break them.
"""

from pathlib import Path

from django.conf import settings


def _html():
    return Path(settings.BASE_DIR, "extension/popup.html").read_text()


def _js():
    return Path(settings.BASE_DIR, "extension/popup.js").read_text()


# --- #107: markup ----------------------------------------------------------


def test_provider_model_markup_with_loading_placeholders():
    html = _html()
    assert '<label class="deck-label" for="provider-select">Provider:</label>' in html
    assert 'id="provider-select"' in html
    assert "Loading providers…" in html
    assert '<label class="deck-label" for="model-select">Model:</label>' in html
    assert 'id="model-select"' in html
    assert "Loading models…" in html


def test_popup_stays_260px_and_reuses_deck_select_rules():
    html = _html()
    assert "width: 260px" in html
    assert "#deck-select, #deck-new, #provider-select, #model-select" in html
    assert "outline: 2px solid #2f6fed" in html
    assert "#provider-select:focus-visible" in html
    assert "#model-select:focus-visible" in html


# --- #109: banner element ----------------------------------------------------


def test_banner_above_generate_with_status_role():
    html = _html()
    assert 'id="llm-key-banner"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    banner = html.index('id="llm-key-banner"')
    generate = html.index('id="generate"')
    provider = html.index('id="provider-select"')
    # Spec §2 diagram order: dropdowns -> warning -> Generate.
    assert provider < banner < generate
    # Hidden by default; visibility toggles via the hidden attribute.
    assert '<p id="llm-key-banner" role="status" aria-live="polite" hidden></p>' in html
    # Banner error color reuses the existing #status error color.
    assert "#llm-key-banner" in html and "#b00020" in html


# --- #107: config fetch ------------------------------------------------------


def test_load_llm_config_reuses_backend_helpers():
    js = _js()
    assert "function loadLlmConfig()" in js
    assert "/api/extension/llm-config/" in js
    assert "ensureBackend()" in js
    assert "fetchOrNetworkError(" in js
    # Called on popup open alongside loadDecks().
    assert "loadDecks();" in js
    assert "loadLlmConfig();" in js
    # Bearer token auth, same as the deck picker.
    assert 'Authorization: "Bearer " + backend.token' in js


def test_submit_includes_provider_model_field_names():
    js = _js()
    assert "payload.provider = " in js
    assert "payload.model = " in js
    # Degraded selects (disabled) omit the fields so the backend falls
    # back to .env-implicit behavior.
    assert "chosenLlm()" in js


def test_no_hardcoded_provider_or_model_names():
    js = _js()
    for name in ("anthropic", "claude-sonnet", "gpt-4o", "grok-4", "openai"):
        assert name not in js


# --- #108: persistence ---------------------------------------------------------


def test_storage_key_and_save_on_change():
    js = _js()
    assert '"llmSelection"' in js
    assert "chrome.storage.local" in js
    assert "chrome.storage.sync" not in js
    assert "providerSelect.addEventListener" in js
    assert "modelSelect.addEventListener" in js


# --- #109: banner logic -----------------------------------------------------------


def test_banner_copy_and_cache_reuse():
    js = _js()
    assert "No API key configured for " in js
    assert "add it to your backend `.env` and restart." in js
    assert "key_configured" in js
    assert "llmConfigCache" in js
    # Provider changes only do a cache lookup plus banner/button update.
    assert js.count("/api/extension/llm-config/") == 1


def test_banner_fail_open_paths():
    js = _js()
    assert "flowRunning" in js
    assert "degradeLlmConfig" in js
