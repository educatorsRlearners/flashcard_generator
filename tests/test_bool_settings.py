"""Tests for the shared boolean-setting parser (issue #149).

``submissions.settings_utils.parse_bool_setting`` is the shared
isinstance/None/string-normalization helper used by default-on boolean
settings such as ``FEWSHOT_ENABLED``.
"""

import pytest

from submissions import feedback, settings_utils

pytestmark = pytest.mark.django_db


# --- the shared helper -------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["0", "false", "FALSE", "no", "NO", "off", "OFF", "", "   ", "  False  "],
)
def test_parse_bool_setting_falsy_forms_disable(raw):
    assert settings_utils.parse_bool_setting(raw) is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "  True  "])
def test_parse_bool_setting_truthy_forms_enable(raw):
    assert settings_utils.parse_bool_setting(raw) is True


def test_parse_bool_setting_bool_passthrough_and_none_default():
    assert settings_utils.parse_bool_setting(True) is True
    assert settings_utils.parse_bool_setting(False) is False
    assert settings_utils.parse_bool_setting(None) is True
    assert settings_utils.parse_bool_setting(None, default=False) is False


def test_parse_bool_setting_non_string_raw():
    assert settings_utils.parse_bool_setting(0) is False
    assert settings_utils.parse_bool_setting(1) is True


# --- the fewshot call site honors the shared helper ---------------------


@pytest.mark.parametrize(
    "raw",
    [False, "0", "false", "no", "off", "", "  False  "],
)
def test_fewshot_call_site_disables_on_falsy_forms(settings, raw):
    settings.FEWSHOT_ENABLED = raw
    assert feedback.fewshot_enabled() is False


@pytest.mark.parametrize("raw", [True, "1", "true", "yes", "on", "  True  "])
def test_fewshot_call_site_enables_on_truthy_forms(settings, raw):
    settings.FEWSHOT_ENABLED = raw
    assert feedback.fewshot_enabled() is True


def test_fewshot_default_on_when_absent_or_none(settings):
    del settings.FEWSHOT_ENABLED
    assert feedback.fewshot_enabled() is True
    settings.FEWSHOT_ENABLED = None
    assert feedback.fewshot_enabled() is True


def test_fewshot_call_site_delegates_to_the_shared_helper(settings, monkeypatch):
    """No duplicated parser: the switch calls the one shared helper."""
    calls = []

    def spy(raw, *, default=True):
        calls.append((raw, default))
        return "parsed"

    monkeypatch.setattr(settings_utils, "parse_bool_setting", spy)
    settings.FEWSHOT_ENABLED = "raw-fewshot"
    assert feedback.fewshot_enabled() == "parsed"
    assert calls == [("raw-fewshot", True)]


# --- real env -> Django settings wiring --------------------------------
# Same reload pattern as test_llm.py (issue #121): an env var reaching
# config/settings.py via os.environ.get, with Django settings re-derived
# from the reloaded module — no override_settings() short-circuit.


def _reload_settings_with_env(monkeypatch, env):
    import importlib
    from contextlib import contextmanager

    import django.conf

    import config.settings as settings_module

    @contextmanager
    def _ctx():
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        try:
            importlib.reload(settings_module)
            django.conf.settings._wrapped = django.conf.empty
            yield django.conf.settings
        finally:
            for key in env:
                monkeypatch.delenv(key, raising=False)
            importlib.reload(settings_module)
            django.conf.settings._wrapped = django.conf.empty

    return _ctx()


def test_fewshot_enabled_env_still_wires_through_real_settings_pipeline(
    monkeypatch,
):
    with _reload_settings_with_env(monkeypatch, {"FEWSHOT_ENABLED": "0"}) as s:
        assert s.FEWSHOT_ENABLED is False
        assert feedback.fewshot_enabled() is False
    with _reload_settings_with_env(monkeypatch, {"FEWSHOT_ENABLED": None}) as s:
        assert s.FEWSHOT_ENABLED is True
        assert feedback.fewshot_enabled() is True
