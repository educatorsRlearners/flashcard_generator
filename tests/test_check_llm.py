"""Tests for the ``check_llm`` management command (issue #86, extended by
#99 with an optional ``--provider`` override).

``submissions.llm.get_provider`` is monkeypatched and the fake ``Provider``
instances it returns carry their own ``.generate``, so these tests make no
real network calls and need no real API key. The command must route the
actual round-trip call through the returned instance's own ``.generate``
(not the module-level ``submissions.llm.generate``), so ``get_provider`` is
the only monkeypatch target throughout.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from submissions import llm


class FakeProvider:
    name = "fake-provider"
    model = "fake-model-1"

    def __init__(self, *, generate_result=None, generate_exc=None):
        self._generate_result = generate_result or llm.LLMResult(
            text="OK", model="fake-model-1"
        )
        self._generate_exc = generate_exc
        self.generate_calls = []

    def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        if self._generate_exc is not None:
            raise self._generate_exc
        return self._generate_result


class OtherFakeProvider(FakeProvider):
    name = "other-provider"
    model = "other-model-1"


def test_success_prints_provider_and_ok(monkeypatch, capsys):
    fake = FakeProvider()
    monkeypatch.setattr(llm, "get_provider", lambda name=None: fake)

    call_command("check_llm")

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert lines[0] == "Provider: fake-provider (model: fake-model-1)"
    assert "OK" in lines[1]
    assert len(fake.generate_calls) == 1


def test_provider_omitted_calls_get_provider_with_no_name(monkeypatch, capsys):
    """Not passing --provider leaves today's behavior byte-for-byte
    unchanged: get_provider is called with name=None."""
    received = {}

    def fake_get_provider(name=None):
        received["name"] = name
        return FakeProvider()

    monkeypatch.setattr(llm, "get_provider", fake_get_provider)

    call_command("check_llm")

    assert received["name"] is None


def test_provider_flag_overrides_default(monkeypatch, capsys):
    """--provider <valid-other-name> overrides to a different registered
    provider than get_provider() would pick by default, and both the
    printed line and the exercised call reflect the override."""
    default_fake = FakeProvider()
    other_fake = OtherFakeProvider()

    def fake_get_provider(name=None):
        if name == "other":
            return other_fake
        return default_fake

    monkeypatch.setattr(llm, "get_provider", fake_get_provider)

    call_command("check_llm", "--provider", "other")

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert lines[0] == "Provider: other-provider (model: other-model-1)"
    assert "OK" in lines[1]
    # The actual round-trip call went to the overridden provider, not the
    # env-configured default.
    assert len(other_fake.generate_calls) == 1
    assert default_fake.generate_calls == []


def test_config_error_from_get_provider(monkeypatch, capsys):
    def boom(name=None):
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


def test_invalid_provider_flag_raises_config_error(monkeypatch, capsys):
    """--provider <invalid-name> raises the same CommandError shape as an
    unrecognized LLM_PROVIDER env var, with no separate validation of
    --provider added in the command itself: get_provider's own
    LLMConfigError is reused as-is."""
    received = {}

    def fake_get_provider(name=None):
        received["name"] = name
        raise llm.LLMConfigError(f"Unknown LLM_PROVIDER {name!r}.")

    monkeypatch.setattr(llm, "get_provider", fake_get_provider)

    with pytest.raises(CommandError) as exc:
        call_command("check_llm", "--provider", "bogus")

    assert received["name"] == "bogus"
    assert "LLMConfigError" in str(exc.value)
    assert "Unknown LLM_PROVIDER" in str(exc.value)
    out = capsys.readouterr().out
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
    fake = FakeProvider(generate_exc=exc_class(message))
    monkeypatch.setattr(llm, "get_provider", lambda name=None: fake)

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
    monkeypatch.setattr(llm, "get_provider", lambda name=None: FakeProvider())

    # No exception raised means Command.handle() returned normally, i.e.
    # the process would exit 0.
    call_command("check_llm")
