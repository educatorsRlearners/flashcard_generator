"""Tests for the ``check_llm`` management command (issue #86).

``submissions.llm.get_provider`` / ``generate`` are monkeypatched, so these
tests make no real network calls and need no real API key.
"""

import types

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from submissions import llm


class FakeProvider:
    name = "fake-provider"
    model = "fake-model-1"


def _fake_result(text="OK"):
    return llm.LLMResult(text=text, model="fake-model-1")


def test_success_prints_provider_and_ok(monkeypatch, capsys):
    monkeypatch.setattr(llm, "get_provider", lambda: FakeProvider())
    monkeypatch.setattr(llm, "generate", lambda **kwargs: _fake_result())

    call_command("check_llm")

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert lines[0] == "Provider: fake-provider (model: fake-model-1)"
    assert "OK" in lines[1]


def test_config_error_from_get_provider(monkeypatch, capsys):
    def boom():
        raise llm.LLMConfigError("Unknown LLM_PROVIDER 'bogus'.")

    monkeypatch.setattr(llm, "get_provider", boom)

    with pytest.raises(CommandError) as exc:
        call_command("check_llm")

    assert "LLMConfigError" in str(exc.value)
    assert "Unknown LLM_PROVIDER" in str(exc.value)
    out = capsys.readouterr().out
    # No provider line, since no Provider instance was ever built.
    assert "Provider:" not in out
    assert "Traceback" not in out


@pytest.mark.parametrize(
    "exc_class, message",
    [
        (llm.LLMAuthError, "No API key found. Set the ANTHROPIC_API_KEY environment variable."),
        (llm.LLMRateLimitError, "rate limited"),
        (llm.LLMTransientError, "request timed out"),
        (llm.LLMBadResponseError, "provider returned an empty response"),
    ],
)
def test_generate_failure_per_exception_type(monkeypatch, capsys, exc_class, message):
    monkeypatch.setattr(llm, "get_provider", lambda: FakeProvider())

    def boom(**kwargs):
        raise exc_class(message)

    monkeypatch.setattr(llm, "generate", boom)

    with pytest.raises(CommandError) as exc:
        call_command("check_llm")

    assert exc_class.__name__ in str(exc.value)
    assert message in str(exc.value)

    captured = capsys.readouterr()
    assert "Provider: fake-provider (model: fake-model-1)" in captured.out
    assert exc_class.__name__ in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_exits_zero_on_success(monkeypatch):
    monkeypatch.setattr(llm, "get_provider", lambda: FakeProvider())
    monkeypatch.setattr(llm, "generate", lambda **kwargs: _fake_result())

    # No exception raised means Command.handle() returned normally, i.e.
    # the process would exit 0.
    call_command("check_llm")
