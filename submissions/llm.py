"""Thin, provider-agnostic LLM client.

The rest of the app calls :func:`generate` (or constructs a provider via
:func:`get_provider`) and never imports a provider SDK directly.  Which
provider and model are used is read from Django settings:

* ``LLM_PROVIDER``      - registry key, default ``"anthropic"``;
*                             ``"openai-compatible"`` (alias ``"openai"``)
*                             selects the OpenAI-compatible adapter (issue #27)
* ``LLM_MODEL``         - model id, default ``"claude-sonnet-5"``
* ``LLM_API_KEY_ENV_VAR`` - name of the env var holding the API key
* ``LLM_MAX_TOKENS``    - default output-token ceiling
* ``LLM_OPENAI_BASE_URL`` - base URL for the OpenAI-compatible endpoint,
*                             default ``"https://api.openai.com/v1"``
* ``LLM_OPENAI_MODEL``  - optional override of ``LLM_MODEL`` for the
*                             OpenAI-compatible provider (empty = fall back)
* ``LLM_OPENAI_API_KEY_ENV_VAR`` - optional override of
*                             ``LLM_API_KEY_ENV_VAR`` for the OpenAI-compatible
*                             provider (empty = fall back)
* ``LLM_GEMINI_MODEL``    - optional override of ``LLM_MODEL`` for the
*                             ``gemini`` provider (empty = fall back)
* ``LLM_GEMINI_API_KEY_ENV_VAR`` - optional override of
*                             ``LLM_API_KEY_ENV_VAR`` for the ``gemini``
*                             provider (empty = fall back). No new env-var
*                             name is invented for the key itself:
*                             conventionally set this to ``GOOGLE_API_KEY``
*                             (or set the generic ``LLM_API_KEY_ENV_VAR``
*                             directly) and export ``GOOGLE_API_KEY``
*                             yourself (``_docs/llm_portability.md`` §4)
* ``LLM_GROK_BASE_URL``  - optional override of the ``grok`` provider's
*                             base URL, default ``"https://api.x.ai/v1"``
*                             (empty = fall back to that hardcoded default)
* ``LLM_GROK_MODEL``     - optional override of ``LLM_MODEL`` for the
*                             ``grok`` provider (empty = fall back)
* ``LLM_GROK_API_KEY_ENV_VAR`` - optional override of the ``grok``
*                             provider's key env var, default
*                             ``"XAI_API_KEY"`` (empty = fall back to that
*                             hardcoded default)
* ``LLM_OPENROUTER_BASE_URL`` - optional override of the ``openrouter``
*                             provider's base URL, default
*                             ``"https://openrouter.ai/api/v1"`` (empty =
*                             fall back to that hardcoded default)
* ``LLM_OPENROUTER_MODEL`` - optional override of ``LLM_MODEL`` for the
*                             ``openrouter`` provider (empty = fall back)
* ``LLM_OPENROUTER_API_KEY_ENV_VAR`` - optional override of the
*                             ``openrouter`` provider's key env var, default
*                             ``"OPENROUTER_API_KEY"`` (empty = fall back to
*                             that hardcoded default)

``LLM_PROVIDER=gemini`` (issue #83) talks to Google's Generative Language
API directly via ``httpx`` (no SDK dependency), against a fixed endpoint
(``https://generativelanguage.googleapis.com/v1beta``) - there is no
``LLM_GEMINI_BASE_URL`` setting, unlike the OpenAI-compatible provider.

``LLM_PROVIDER=grok`` and ``LLM_PROVIDER=openrouter`` (issue #84) are also
``OpenAICompatibleProvider`` under the hood, with a hardcoded default
``base_url`` and API-key env var per provider (``XAI_API_KEY`` /
``OPENROUTER_API_KEY``). Issue #98 added ``LLM_GROK_*``/``LLM_OPENROUTER_*``
override settings for both, following the same override-wins/empty-falls-
back pattern as ``LLM_OPENAI_*``/``LLM_GEMINI_*``; see
``_NAMED_OPENAI_COMPATIBLE_DEFAULTS``. ``LLM_PROVIDER=opencode-zen``
(issue #104) is a third named ``OpenAICompatibleProvider`` pointed at
Zen's OpenAI-compatible route (base ``https://opencode.ai/zen/v1`` - the
OpenAI SDK appends ``/chat/completions`` itself) with hardcoded default
key env var ``OPENCODE_ZEN_API_KEY`` and ``LLM_OPENCODE_ZEN_*`` overrides
following the same pattern.

Adding a third provider is one entry in ``_PROVIDERS`` plus a new
``Provider`` subclass in this file - nothing else in the codebase changes,
and ``import anthropic`` / ``import openai`` stay confined to this module.

Nothing here logs or embeds the API key in an exception message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterator, NamedTuple, Optional

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# --- Tunables (named constants, not literals scattered in code) ----------

#: Per-request wall-clock timeout handed to the provider SDK, in seconds.
REQUEST_TIMEOUT_SECONDS: float = 60.0

#: Total attempts for a single ``generate`` call: 1 initial try + retries.
MAX_ATTEMPTS: int = 4

#: Exponential backoff between transient retries: BASE * 2**(attempt-1),
#: capped at MAX. Only applied to transient failures (429 / 5xx / conn /
#: timeout); non-transient errors propagate on the first attempt.
RETRY_BACKOFF_BASE_SECONDS: float = 0.5
RETRY_BACKOFF_MAX_SECONDS: float = 8.0

#: Corrective follow-up sent once, per ``generate()`` call, when a
#: structured-output reply comes back as unparseable or schema-invalid JSON
#: (issue #85 / ``_docs/llm_portability.md`` §5).
JSON_RETRY_INSTRUCTION: str = (
    "Your previous response was not valid JSON matching the requested "
    "schema. Return only JSON matching the schema — no markdown, no "
    "code fences, no commentary."
)

#: ``LLMBadResponseError.reason`` values that qualify for the one JSON
#: retry; any other reason (refusal/truncated/empty) is not retried here.
_JSON_RETRYABLE_REASONS = frozenset({"malformed_json", "schema_violation"})


# --- Exception hierarchy ------------------------------------------------


class LLMError(Exception):
    """Base class for every error surfaced by this module.

    Callers in #6 catch these without importing any provider SDK classes.
    """


class LLMConfigError(LLMError):
    """The client is misconfigured (unknown provider, etc.)."""


class LLMAuthError(LLMError):
    """The API key is missing, invalid, or rejected by the provider."""


class LLMRateLimitError(LLMError):
    """HTTP 429 / rate limited. ``retry_after`` is seconds, if the provider said."""

    def __init__(self, message: str = "rate limited", *, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMTransientError(LLMError):
    """A retryable failure (5xx, connection error, timeout) that outlived its retries."""

    def __init__(self, message: str, *, reason: str = "transient"):
        super().__init__(message)
        self.reason = reason


class LLMBadResponseError(LLMError):
    """The provider replied, but the reply is unusable (empty, refused,
    truncated, or does not match the requested schema)."""

    def __init__(self, message: str, *, reason: str = "bad_response"):
        super().__init__(message)
        self.reason = reason


def _timeout_transient_error() -> LLMTransientError:
    """Shared timeout mapping used by the generic classifier and Gemini."""
    return LLMTransientError("request timed out", reason="timeout")


def _connection_transient_error() -> LLMTransientError:
    """Shared connection-error mapping used by the generic classifier and Gemini."""
    return LLMTransientError("connection error contacting provider", reason="connection")


def _unknown_transient_error(name: str) -> LLMTransientError:
    """Shared unknown-error mapping (single message template)."""
    return LLMTransientError(f"unexpected provider error ({name})", reason="unknown")


# --- Result type -------------------------------------------------------


@dataclass
class LLMResult:
    """What :func:`generate` returns."""

    text: str
    parsed: Optional[Any] = None
    model: str = ""
    stop_reason: Optional[str] = None
    usage: dict = field(default_factory=dict)


# --- Config resolution -------------------------------------------------


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _resolve_provider_name() -> str:
    return _setting("LLM_PROVIDER", "anthropic")


def _resolve_model() -> str:
    return _setting("LLM_MODEL", "claude-sonnet-5")


def _resolve_api_key_env_var() -> str:
    return _setting("LLM_API_KEY_ENV_VAR", "ANTHROPIC_API_KEY")


def _resolve_max_tokens(explicit: Optional[int]) -> int:
    if explicit is not None:
        return int(explicit)
    return int(_setting("LLM_MAX_TOKENS", 4096))


def _resolve_provider_setting(override_setting: str, fallback: Callable[[], str]) -> str:
    """Shared override-wins/empty-falls-back resolver (issue #126).

    ``override_setting`` is the per-provider ``LLM_*`` setting name; when its
    value is truthy it wins, otherwise ``fallback()`` supplies the generic
    setting or hardcoded default. Truthiness (``if override:``) is deliberate:
    a whitespace-only override counts as set, matching the pre-change bodies.
    """
    override = _setting(override_setting, "")
    if override:
        return override
    return fallback()


def _resolve_openai_base_url() -> str:
    return _setting("LLM_OPENAI_BASE_URL", "https://api.openai.com/v1")


def _resolve_openai_model() -> str:
    return _resolve_provider_setting("LLM_OPENAI_MODEL", _resolve_model)


def _resolve_openai_api_key_env_var() -> str:
    return _resolve_provider_setting("LLM_OPENAI_API_KEY_ENV_VAR", _resolve_api_key_env_var)


def _resolve_gemini_model() -> str:
    return _resolve_provider_setting("LLM_GEMINI_MODEL", _resolve_model)


def _resolve_gemini_api_key_env_var() -> str:
    return _resolve_provider_setting("LLM_GEMINI_API_KEY_ENV_VAR", _resolve_api_key_env_var)


def _resolve_grok_base_url() -> str:
    return _resolve_provider_setting("LLM_GROK_BASE_URL", lambda: _NAMED_OPENAI_COMPATIBLE_DEFAULTS["grok"]["base_url"])


def _resolve_grok_model() -> str:
    return _resolve_provider_setting("LLM_GROK_MODEL", _resolve_model)


def _resolve_grok_api_key_env_var() -> str:
    return _resolve_provider_setting("LLM_GROK_API_KEY_ENV_VAR", lambda: _NAMED_OPENAI_COMPATIBLE_DEFAULTS["grok"]["api_key_env_var"])


def _resolve_openrouter_base_url() -> str:
    return _resolve_provider_setting("LLM_OPENROUTER_BASE_URL", lambda: _NAMED_OPENAI_COMPATIBLE_DEFAULTS["openrouter"]["base_url"])


def _resolve_openrouter_model() -> str:
    return _resolve_provider_setting("LLM_OPENROUTER_MODEL", _resolve_model)


def _resolve_openrouter_api_key_env_var() -> str:
    return _resolve_provider_setting("LLM_OPENROUTER_API_KEY_ENV_VAR", lambda: _NAMED_OPENAI_COMPATIBLE_DEFAULTS["openrouter"]["api_key_env_var"])


def _resolve_opencode_zen_base_url() -> str:
    # Blank ("" / whitespace-only) counts as unset and falls back to the
    # hardcoded default; any other value passes through verbatim via the
    # shared override-wins resolver (issue #148 QA: _resolve_provider_setting
    # uses truthiness, so whitespace would otherwise count as set).
    override = _setting("LLM_OPENCODE_ZEN_BASE_URL", "")
    if isinstance(override, str) and not override.strip():
        return _NAMED_OPENAI_COMPATIBLE_DEFAULTS["opencode-zen"]["base_url"]
    return _resolve_provider_setting("LLM_OPENCODE_ZEN_BASE_URL", lambda: _NAMED_OPENAI_COMPATIBLE_DEFAULTS["opencode-zen"]["base_url"])


def _resolve_opencode_zen_model() -> str:
    return _resolve_provider_setting("LLM_OPENCODE_ZEN_MODEL", _resolve_model)


def _resolve_opencode_zen_api_key_env_var() -> str:
    return _resolve_provider_setting("LLM_OPENCODE_ZEN_API_KEY_ENV_VAR", lambda: _NAMED_OPENAI_COMPATIBLE_DEFAULTS["opencode-zen"]["api_key_env_var"])


# --- Provider interface ----------------------------------------------


class Provider:
    """A provider adapter. Subclasses implement :meth:`_call` and
    :meth:`_extract`; exception mapping defaults to
    :func:`_classify_provider_exception` (override :meth:`_map_exception`
    only for HTTP-status specifics, as Gemini does). The retry loop and
    response handling live here."""

    name = "base"

    def __init__(self, *, model: str, api_key_env_var: str):
        self.model = model
        self.api_key_env_var = api_key_env_var

    # -- public API --

    def check(self) -> None:
        """Raise if this provider could not make a call right now."""
        self._require_api_key()

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        response_format: Optional[dict] = None,
        max_tokens: int,
        batch: Any = None,
        submitted_url: Any = None,
    ) -> LLMResult:
        """Send one prompt to the provider, recording an ``LLMCall`` row
        (issue #28) for the outcome - ok or failed - before returning or
        raising. ``batch`` / ``submitted_url`` attribute the row; when
        omitted, the ambient :func:`call_context` applies."""
        batch, submitted_url = _resolve_attribution(batch, submitted_url)
        start = time.perf_counter()
        #: True once ``_retry_malformed_json`` has been invoked for this call
        #: (issue #100) - regardless of whether that retry ultimately
        #: succeeds; set before the retry call so a failure raised out of
        #: it still records ``json_retried=True`` on the failed row.
        json_retried = False
        try:
            self._require_api_key()

            raw = self._call_with_transient_retries(
                lambda: self._call(
                    system=system,
                    prompt=prompt,
                    response_format=response_format,
                    max_tokens=max_tokens,
                )
            )
            if response_format is not None:
                try:
                    result = self._build_result(raw, response_format)
                except LLMBadResponseError as exc:
                    if exc.reason not in _JSON_RETRYABLE_REASONS:
                        raise
                    json_retried = True
                    result = self._retry_malformed_json(
                        system=system,
                        prompt=prompt,
                        response_format=response_format,
                        max_tokens=max_tokens,
                        bad_raw=raw,
                    )
            else:
                result = self._build_result(raw, response_format)
        except LLMError as exc:
            _record_llm_call(
                model=self.model,
                input_tokens=0,
                output_tokens=0,
                latency_ms=int((time.perf_counter() - start) * 1000),
                status="failed",
                error_class=type(exc).__name__,
                provider=self.name,
                json_retried=json_retried,
                batch=batch,
                submitted_url=submitted_url,
            )
            raise
        usage = result.usage or {}
        _record_llm_call(
            model=self.model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=int((time.perf_counter() - start) * 1000),
            status="ok",
            provider=self.name,
            json_retried=json_retried,
            batch=batch,
            submitted_url=submitted_url,
        )
        return result

    # -- hooks for subclasses --

    def _require_api_key(self) -> str:
        key = os.environ.get(self.api_key_env_var)
        if not key:
            raise LLMAuthError(
                f"No API key found. Set the {self.api_key_env_var} environment variable."
            )
        return key

    def _call(self, *, system, prompt, response_format, max_tokens):  # pragma: no cover
        raise NotImplementedError

    def _map_exception(self, exc: BaseException) -> LLMError:
        """Default exception mapping shared by all providers (issue #128).

        Duck-typed SDK-error classifier; subclasses override only for
        HTTP-status specifics (as Gemini does).
        """
        return _classify_provider_exception(exc)

    def _call_with_transient_retries(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` with the shared transient-retry policy (issue #128).

        One implementation used by both :meth:`generate` and
        :meth:`_retry_malformed_json`: try ``fn``, map unknown exceptions via
        :meth:`_map_exception`, retry while :func:`_is_retryable` holds and
        the ``MAX_ATTEMPTS`` budget remains, sleeping
        :func:`_backoff_seconds`. An already-mapped :class:`LLMError`
        propagates unchanged (retried only if retryable).
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return fn()
            except LLMError:
                raise
            except BaseException as exc:  # noqa: BLE001 - provider SDK error
                mapped = self._map_exception(exc)
                if _is_retryable(mapped) and attempt < MAX_ATTEMPTS:
                    _sleep(_backoff_seconds(attempt, mapped))
                    continue
                raise mapped from None

    def _extract(self, raw: Any) -> tuple[str, Optional[str], dict]:
        """Return (text, stop_reason, usage) from a provider response."""
        raise NotImplementedError  # pragma: no cover

    def _retry_malformed_json(
        self,
        *,
        system: str,
        prompt: str,
        response_format: dict,
        max_tokens: int,
        bad_raw: Any,
    ) -> LLMResult:
        """One corrective follow-up when a structured-output reply is
        unparseable or schema-invalid JSON (issue #85).

        Sends a fresh, single-turn call (no prior assistant turn replayed,
        matching ``_call``'s ``messages=[{"role": "user", ...}]`` shape):
        the original prompt, the bad reply's raw text, and an explicit
        "return only JSON" instruction. The follow-up call gets its own
        transient-error retry budget (``MAX_ATTEMPTS``-sized, independent of
        the caller's attempt loop); if the re-parsed follow-up is itself
        malformed/schema-invalid, that exception propagates unchanged - this
        is exactly one retry, never a loop.
        """
        bad_text, _, _ = self._extract(bad_raw)
        follow_up_prompt = f"{prompt}\n\n{bad_text}\n\n{JSON_RETRY_INSTRUCTION}"

        retry_raw = self._call_with_transient_retries(
            lambda: self._call(
                system=system,
                prompt=follow_up_prompt,
                response_format=response_format,
                max_tokens=max_tokens,
            )
        )
        return self._build_result(retry_raw, response_format)

    # -- shared response handling --

    def _build_result(self, raw: Any, response_format: Optional[dict]) -> LLMResult:
        text, stop_reason, usage = self._extract(raw)

        if stop_reason == "refusal":
            raise LLMBadResponseError("provider refused the request", reason="refusal")
        if stop_reason == "max_tokens":
            raise LLMBadResponseError(
                "response truncated (hit max_tokens)", reason="truncated"
            )
        if not text or not text.strip():
            raise LLMBadResponseError("provider returned an empty response", reason="empty")

        parsed = None
        if response_format is not None:
            parsed = _parse_structured(text, response_format)

        return LLMResult(
            text=text,
            parsed=parsed,
            model=self.model,
            stop_reason=stop_reason,
            usage=usage or {},
        )


# --- Anthropic implementation --------------------------------------


def _new_anthropic_client(api_key: str) -> Any:
    """Construct the Anthropic SDK client.

    Isolated in its own function so tests can monkeypatch it and never touch
    the network or import ``anthropic``.
    """
    import anthropic  # local import: keeps the SDK dependency inside this module

    return anthropic.Anthropic(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,  # this module owns retry/backoff
    )


class AnthropicProvider(Provider):
    name = "anthropic"

    def _client(self) -> Any:
        return _new_anthropic_client(self._require_api_key())

    def _call(self, *, system, prompt, response_format, max_tokens):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if response_format is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": response_format}
            }
        return self._client().messages.create(**kwargs)

    def _extract(self, raw: Any) -> tuple[str, Optional[str], dict]:
        parts: list[str] = []
        for block in getattr(raw, "content", None) or []:
            if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                parts.append(block.text)
        stop_reason = getattr(raw, "stop_reason", None)
        usage_obj = getattr(raw, "usage", None)
        usage: dict = {}
        if usage_obj is not None:
            for attr in ("input_tokens", "output_tokens"):
                val = getattr(usage_obj, attr, None)
                if val is not None:
                    usage[attr] = val
        return "".join(parts), stop_reason, usage


# --- OpenAI-compatible implementation (issue #27) --------------------


def _new_openai_client(api_key: str, base_url: str) -> Any:
    """Construct the OpenAI SDK client.

    Isolated in its own function so tests can monkeypatch it and never touch
    the network or import ``openai``. ``base_url`` makes the client talk to
    any OpenAI-compatible gateway; ``max_retries=0`` because this module
    owns retry/backoff.
    """
    import openai  # local import: keeps the SDK dependency inside this module

    return openai.OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    )


class OpenAICompatibleProvider(Provider):
    """Adapter for any OpenAI-compatible chat-completions endpoint.

    Request shape: ``system`` + ``prompt`` become ``system`` / ``user``
    messages; ``response_format`` (a JSON Schema dict) becomes a
    ``response_format={"type": "json_schema", ...}`` parameter so the
    endpoint returns schema-shaped JSON. Response handling (refusal /
    truncation / empty / schema validation) is shared with the base class
    via :meth:`Provider._build_result`.
    """

    name = "openai-compatible"

    def __init__(self, *, model: str, api_key_env_var: str, base_url: str):
        super().__init__(model=model, api_key_env_var=api_key_env_var)
        self.base_url = base_url

    def _client(self) -> Any:
        return _new_openai_client(self._require_api_key(), self.base_url)

    def _call(self, *, system, prompt, response_format, max_tokens):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": response_format,
                },
            }
        return self._client().chat.completions.create(**kwargs)

    def _extract(self, raw: Any) -> tuple[str, Optional[str], dict]:
        choices = getattr(raw, "choices", None) or []
        message = getattr(choices[0], "message", None) if choices else None
        text = getattr(message, "content", None) or ""
        if getattr(message, "refusal", None):
            return "", "refusal", _openai_usage(raw)
        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
        if finish_reason == "content_filter":
            return text, "refusal", _openai_usage(raw)
        if finish_reason == "length":
            # OpenAI's truncation signal; the shared base class reports it
            # as a "max_tokens"/truncated bad response.
            return text, "max_tokens", _openai_usage(raw)
        return text, finish_reason, _openai_usage(raw)


def _openai_usage(raw: Any) -> dict:
    """Pull ``{input_tokens, output_tokens}`` out of a chat-completion."""
    usage_obj = getattr(raw, "usage", None)
    usage: dict = {}
    if usage_obj is not None:
        prompt_tokens = getattr(usage_obj, "prompt_tokens", None)
        completion_tokens = getattr(usage_obj, "completion_tokens", None)
        if prompt_tokens is not None:
            usage["input_tokens"] = prompt_tokens
        if completion_tokens is not None:
            usage["output_tokens"] = completion_tokens
    return usage


# --- Gemini implementation (issue #83) -------------------------------


#: Fixed base URL for Google's Generative Language API. Not a setting: this
#: adapter talks to exactly one API, unlike ``OpenAICompatibleProvider``
#: which is pointed at different gateways via ``LLM_OPENAI_BASE_URL``.
GEMINI_API_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"

#: ``finishReason`` values that mean the model refused / declined to answer
#: rather than completing or being truncated (mirrors how the
#: OpenAI-compatible adapter maps ``content_filter`` to ``"refusal"``).
_GEMINI_REFUSAL_FINISH_REASONS = frozenset(
    {"SAFETY", "RECITATION", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
)


def _gemini_request(*, url: str, headers: dict, json_body: dict) -> httpx.Response:
    """Make the outbound Gemini HTTP call.

    Isolated in its own function so tests can monkeypatch it and never
    construct a real ``httpx.Client`` or touch the network - the same seam
    role ``_new_anthropic_client``/``_new_openai_client`` play for the SDK
    providers. Does not raise for a non-2xx response; the caller decides.
    """
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        return client.post(url, headers=headers, json=json_body)


class GeminiProvider(Provider):
    """Adapter for Google's Generative Language API, called directly via
    ``httpx`` (no ``google-genai``/``google-generativeai`` dependency - see
    issue #83 and ``_docs/llm_portability.md`` §4).

    Request shape: ``system`` becomes ``systemInstruction``, ``prompt``
    becomes a single ``user`` entry in ``contents``, ``max_tokens`` becomes
    ``generationConfig.maxOutputTokens``, and ``response_format`` (a JSON
    Schema dict) becomes ``generationConfig.responseMimeType`` +
    ``responseSchema``. Response handling (refusal / truncation / empty /
    schema validation) is shared with the base class via
    :meth:`Provider._build_result`.
    """

    name = "gemini"

    def _call(self, *, system, prompt, response_format, max_tokens):
        api_key = self._require_api_key()
        url = f"{GEMINI_API_BASE_URL}/models/{self.model}:generateContent"
        headers = {
            "x-goog-api-key": api_key,  # never the ?key= query param (issue #83)
            "Content-Type": "application/json",
        }
        generation_config: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if response_format is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = response_format
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        response = _gemini_request(url=url, headers=headers, json_body=body)
        response.raise_for_status()
        return response.json()

    def _extract(self, raw: Any) -> tuple[str, Optional[str], dict]:
        raw = raw or {}
        candidates = raw.get("candidates") or []
        if not candidates:
            return "", None, _gemini_usage(raw)

        candidate = candidates[0] or {}
        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        )
        finish_reason = candidate.get("finishReason")
        if finish_reason in _GEMINI_REFUSAL_FINISH_REASONS:
            stop_reason = "refusal"
        elif finish_reason == "MAX_TOKENS":
            stop_reason = "max_tokens"
        else:
            # "STOP" (and anything else unrecognized) passes through as-is.
            stop_reason = finish_reason
        return text, stop_reason, _gemini_usage(raw)

    def _map_exception(self, exc: BaseException) -> LLMError:
        if isinstance(exc, httpx.HTTPStatusError):
            return _classify_gemini_http_error(exc)
        if isinstance(exc, httpx.TimeoutException):
            return _timeout_transient_error()
        if isinstance(exc, httpx.TransportError):
            return _connection_transient_error()
        return _unknown_transient_error(type(exc).__name__)


def _gemini_usage(raw: dict) -> dict:
    """Pull ``{input_tokens, output_tokens}`` out of ``usageMetadata``."""
    usage_obj = raw.get("usageMetadata")
    usage: dict = {}
    if isinstance(usage_obj, dict):
        if "promptTokenCount" in usage_obj:
            usage["input_tokens"] = usage_obj["promptTokenCount"]
        if "candidatesTokenCount" in usage_obj:
            usage["output_tokens"] = usage_obj["candidatesTokenCount"]
    return usage


def _classify_gemini_http_error(exc: httpx.HTTPStatusError) -> LLMError:
    """Map a Gemini HTTP error response to a typed :class:`LLMError`.

    Gemini reports a bad API key as HTTP 400 with
    ``error.status == "INVALID_ARGUMENT"``, not 401/403, so that needs an
    explicit special case ahead of the generic "400 = bad_request" branch
    (issue #83).
    """
    status = exc.response.status_code
    try:
        body = exc.response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    error_status = error.get("status")
    error_message = error.get("message") or ""

    if status == 403 or error_status == "PERMISSION_DENIED":
        return LLMAuthError(
            "provider rejected the credentials; check the configured API key"
        )
    if status == 400:
        if error_status == "INVALID_ARGUMENT" and _looks_like_bad_api_key(
            error_message
        ):
            return LLMAuthError(
                "provider rejected the credentials; check the configured API key"
            )
        return LLMBadResponseError(
            "provider rejected the request (HTTP 400)", reason="bad_request"
        )
    if status == 429:
        return LLMRateLimitError(retry_after=_extract_retry_after(exc))
    if status in (500, 503):
        return LLMTransientError(
            f"provider server error (HTTP {status})", reason="server_error"
        )
    # Unknown shape: treat as transient so a blip is retried, but bounded.
    return LLMTransientError(
        f"unexpected provider error (HTTP {status})", reason="unknown"
    )


def _looks_like_bad_api_key(message: str) -> bool:
    lowered = (message or "").lower()
    return "api key" in lowered or "api_key" in lowered


def _classify_provider_exception(exc: BaseException) -> LLMError:
    """Map a provider SDK exception (or a test fake shaped like one) to a
    typed :class:`LLMError`, using duck typing so tests need not import the
    SDK. Real ``anthropic`` errors carry ``status_code`` (from
    ``APIStatusError``) and a class name like ``APITimeoutError``.
    """
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__

    if status == 401 or status == 403 or "Authentication" in name or "PermissionDenied" in name:
        return LLMAuthError(
            "provider rejected the credentials; check the configured API key"
        )
    if status == 429 or "RateLimit" in name:
        return LLMRateLimitError(retry_after=_extract_retry_after(exc))
    if "Timeout" in name:
        return _timeout_transient_error()
    if "APIConnection" in name or "Connection" in name:
        return _connection_transient_error()
    if isinstance(status, int) and status >= 500:
        return LLMTransientError(f"provider server error (HTTP {status})", reason="server_error")
    if isinstance(status, int) and 400 <= status < 500:
        return LLMBadResponseError(
            f"provider rejected the request (HTTP {status})", reason="bad_request"
        )
    # Unknown shape: treat as transient so a blip is retried, but bounded.
    return _unknown_transient_error(name)


def _extract_retry_after(exc: BaseException) -> Optional[float]:
    # Prefer an explicit attribute; fall back to the HTTP response header.
    val = getattr(exc, "retry_after", None)
    if val is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                val = headers.get("retry-after")
            except AttributeError:
                val = None
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# --- Structured-output parsing ------------------------------------


#: Matches a whole response wrapped in a markdown code fence, with an
#: optional ``json`` language tag on the opening fence - e.g. ```` ```json\n
#: {...}\n``` ```` or ```` ```\n{...}\n``` ````. Applied to already-trimmed
#: text; only the fenced body is captured.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(?P<body>.*)\n```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Strip a wrapping markdown code fence, if the trimmed text is one.

    Text with no fence is returned unchanged (verbatim, untrimmed) so
    ``json.loads`` sees exactly what it always has.
    """
    trimmed = text.strip()
    match = _CODE_FENCE_RE.match(trimmed)
    if match:
        return match.group("body")
    return text


def _parse_structured(text: str, schema: dict) -> Any:
    candidate = _strip_code_fence(text)
    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError) as exc:
        raise LLMBadResponseError(
            "structured response was not valid JSON", reason="malformed_json"
        ) from exc
    _validate_against_schema(obj, schema)
    return obj


def _validate_against_schema(obj: Any, schema: dict) -> None:
    """Minimal, dependency-free JSON-Schema check: enough to turn a
    non-conforming response into an :class:`LLMBadResponseError` instead of
    letting a bad shape leak to the caller."""
    expected = schema.get("type")
    _TYPES = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
    }
    if expected in _TYPES:
        ok = isinstance(obj, _TYPES[expected])
        if expected in ("integer", "number") and isinstance(obj, bool):
            ok = False  # bool is an int subclass; a JSON number is not a bool
        if not ok:
            raise LLMBadResponseError(
                f"structured response is not a JSON {expected}", reason="schema_violation"
            )

    if expected == "object" and isinstance(obj, dict):
        for key in schema.get("required", []):
            if key not in obj:
                raise LLMBadResponseError(
                    f"structured response is missing required field '{key}'",
                    reason="schema_violation",
                )
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in obj and isinstance(subschema, dict) and "type" in subschema:
                _validate_against_schema(obj[key], subschema)

    if expected == "array" and isinstance(obj, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict) and "type" in item_schema:
            for item in obj:
                _validate_against_schema(item, item_schema)


# --- Retry helpers ------------------------------------------------


def _is_retryable(err: LLMError) -> bool:
    return isinstance(err, (LLMTransientError, LLMRateLimitError))


def _backoff_seconds(attempt: int, err: LLMError) -> float:
    if isinstance(err, LLMRateLimitError) and err.retry_after is not None:
        return min(err.retry_after, RETRY_BACKOFF_MAX_SECONDS)
    return min(
        RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
        RETRY_BACKOFF_MAX_SECONDS,
    )


def _sleep(seconds: float) -> None:
    """Indirection so tests can neutralise backoff waits via monkeypatch."""
    if seconds > 0:
        time.sleep(seconds)


# --- Cost estimation + attribution context (issue #28) ----------------


#: Fallback per-million-token prices in USD when the model id matches no
#: entry in ``LLM_PRICE_PER_MTOK`` below.
DEFAULT_INPUT_USD_PER_MTOK: float = 3.0
DEFAULT_OUTPUT_USD_PER_MTOK: float = 15.0

#: Known per-model ``(input, output)`` prices in USD per million tokens.
#: Matched by substring against the model id, lowercased.
LLM_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.8, 4.0),
    # OpenAI family (issue #27): ordered most-specific first because the
    # lookup is first-substring-match; the trailing "gpt" is the generic
    # fallback for other gpt-* ids.
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4": (10.0, 30.0),
    "o1": (15.0, 60.0),
    "gpt": (2.5, 10.0),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Estimate the USD cost of one call from token counts.

    Uses the first ``LLM_PRICE_PER_MTOK`` entry whose key appears in the
    model id, else the ``DEFAULT_*`` fallback rates.
    """
    name = (model or "").lower()
    prices = None
    for key, value in LLM_PRICE_PER_MTOK.items():
        if key in name:
            prices = value
            break
    if prices is None:
        prices = (DEFAULT_INPUT_USD_PER_MTOK, DEFAULT_OUTPUT_USD_PER_MTOK)
    total = (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
    return Decimal(str(total)).quantize(Decimal("0.000001"))


#: Attribution for the next ``generate`` call (issue #28): the batch / URL
#: that triggered it. Set via :func:`call_context` by the caller (e.g.
#: :mod:`submissions.generation`); a contextvar so concurrent callers never
#: leak context into each other, and so stubs that replace ``generate``
#: keep working untouched.
_current_call_context: ContextVar[dict] = ContextVar(
    "llm_call_context", default={"batch": None, "submitted_url": None}
)


@contextmanager
def call_context(
    *, batch: Any = None, submitted_url: Any = None
) -> Iterator[None]:
    """Attribute LLM calls in the wrapped block to a batch / URL.

    Values may be model instances, primary keys, or ``None`` (unattributed).
    """
    token = _current_call_context.set(
        {"batch": batch, "submitted_url": submitted_url}
    )
    try:
        yield
    finally:
        _current_call_context.reset(token)


def _resolve_attribution(
    batch: Any, submitted_url: Any
) -> tuple[Any, Any]:
    """Explicit kwargs win; otherwise fall back to :func:`call_context`."""
    ctx = _current_call_context.get() or {}
    if batch is None:
        batch = ctx.get("batch")
    if submitted_url is None:
        submitted_url = ctx.get("submitted_url")
    return batch, submitted_url


def _record_llm_call(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    status: str,
    error_class: str = "",
    provider: str = "",
    json_retried: bool = False,
    batch: Any = None,
    submitted_url: Any = None,
) -> None:
    """Persist one :class:`submissions.models.LLMCall` row (issue #28).

    ``json_retried`` (issue #100) records whether the one-shot JSON-retry
    path (issue #85) fired for this call - independent of ``status``, since
    a retry that itself fails still fired.

    Best-effort by design: observability must never break generation, so any
    failure here is logged and swallowed. The import is local so importing
    this module never requires the Django app registry to be ready.
    """
    try:
        from submissions.models import LLMCall

        kwargs: dict[str, Any] = {
            "model": model or "",
            "prompt_tokens": max(0, int(input_tokens or 0)),
            "completion_tokens": max(0, int(output_tokens or 0)),
            "latency_ms": max(0, int(latency_ms)),
            "estimated_cost_usd": estimate_cost_usd(
                model or "", int(input_tokens or 0), int(output_tokens or 0)
            ),
            "status": status,
            "error_class": error_class or "",
            "provider": provider or "",
            "json_retried": bool(json_retried),
        }
        for field_name, value in (("batch", batch), ("submitted_url", submitted_url)):
            if value is None:
                kwargs[field_name] = None
            elif isinstance(value, int):
                kwargs[f"{field_name}_id"] = value
            else:
                kwargs[field_name] = value
        LLMCall.objects.create(**kwargs)
    except Exception:  # noqa: BLE001 - observability must not break calls
        logger.warning("failed to record LLMCall row", exc_info=True)


# --- Extension popup curated models (issue #105, unified catalog #129) ---
# PROVIDER_CATALOG (the single source of truth) is defined below, after
# _NAMED_OPENAI_COMPATIBLE_DEFAULTS which its named-provider entries
# reference - followed by the derived EXTENSION_LLM_* constants. Kept in
# this section header so the #105 domain data stays beside _PROVIDERS.


# --- Provider registry (the seam #27 extends) --------------------


#: The one obvious place a new provider is wired in. Key == ``LLM_PROVIDER``.
#: ``"openai"`` is a short alias for ``"openai-compatible"``.
_PROVIDERS: dict[str, Callable[..., Provider]] = {
    "anthropic": AnthropicProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "openai": OpenAICompatibleProvider,
    # Named OpenAI-compatible providers (issue #84) - see
    # _NAMED_OPENAI_COMPATIBLE_DEFAULTS below for their hardcoded base_url /
    # api_key_env_var.
    "grok": OpenAICompatibleProvider,
    "openrouter": OpenAICompatibleProvider,
    "opencode-zen": OpenAICompatibleProvider,
    "gemini": GeminiProvider,
}

#: Provider names that need the OpenAI-compatible constructor kwargs.
_OPENAI_PROVIDER_NAMES = frozenset({"openai-compatible", "openai"})

#: Hardcoded defaults for "named" OpenAI-compatible providers (issue #84,
#: extended by #104 for ``opencode-zen``):
#: each is just an ``OpenAICompatibleProvider``. These are the fallback
#: ``base_url``/``api_key_env_var`` used when the corresponding
#: ``LLM_GROK_*``/``LLM_OPENROUTER_*``/``LLM_OPENCODE_ZEN_*`` override setting
#: (issue #98 / #104) is unset
#: or empty; ``model`` falls back to the generic ``LLM_MODEL`` setting via
#: :func:`_resolve_model` the same way, unless ``LLM_GROK_MODEL``/
#: ``LLM_OPENROUTER_MODEL``/``LLM_OPENCODE_ZEN_MODEL`` is set.
#:
#: Request-shape note: for ``grok``, live-verified (issue #102) against the
#: real xAI chat-completions endpoint using model ``grok-4.3`` - Bearer auth
#: header, a ``max_tokens`` field, and ``choices[0].message.content`` in the
#: response all matched what ``OpenAICompatibleProvider`` already sends/
#: expects, including the structured-output path: a non-``None``
#: ``response_format`` round-tripped into ``LLMResult.parsed`` with no
#: schema/parse error. No adapter change was needed. For ``openrouter`` this
#: remains doc-based only - no live credentials available in this
#: environment; live-credential verification is tracked in #119. For
#: ``opencode-zen`` (issue #114) no 200 has been observed yet, so this
#: remains doc-based too: on 2026-09-16 ``check_llm --provider opencode-zen``
#: with paid chat-completions ids (``deepseek-v4-pro``,
#: ``deepseek-v4-flash``) answered 401 ``CreditsError`` (no payment method
#: on the workspace) and the free chat-completions id ``big-pickle``
#: answered 400 ``MissingSessionID`` (free tier only usable inside
#: OpenCode). Neither error points at the request shape, so no adapter
#: change was made; re-run the live check once the workspace is funded.
_NAMED_OPENAI_COMPATIBLE_DEFAULTS: dict[str, dict[str, str]] = {
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "api_key_env_var": "XAI_API_KEY",
    },
    # OpenRouter model ids are origin-prefixed (e.g.
    # "anthropic/claude-3.5-sonnet", "openai/gpt-4o") - a bare/native model
    # name, or something like "openrouter/auto", should not be assumed;
    # ``LLM_MODEL`` must be set to one of OpenRouter's own prefixed ids.
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env_var": "OPENROUTER_API_KEY",
    },
    # Zen's OpenAI-compatible route is served from base ``/v1``: the OpenAI
    # SDK's ``client.chat.completions.create()`` appends ``/chat/completions``
    # to ``base_url`` itself, so the default must end at ``/v1`` (a default
    # already ending in ``/chat/completions`` would double the segment).
    # /v1/messages is Anthropic-shaped, /v1/responses is the OpenAI
    # Responses API, /v1/models/* is Google-shaped - none of which this
    # adapter speaks. Only ``/v1/chat/completions`` is supported here (issue
    # #115); the other route families are deferred to #160. No Zen model id
    # is hardcoded here; set LLM_MODEL /
    # LLM_OPENCODE_ZEN_MODEL explicitly (curated chat-completions ids are
    # #105's job).
    "opencode-zen": {
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_env_var": "OPENCODE_ZEN_API_KEY",
    },
}

# --- Unified provider/model catalog (issue #129) -----------------------
#
# PROVIDER_CATALOG is the single source of truth for providers/models:
# each entry carries the display id, backend registry key in _PROVIDERS,
# curated model ids (verbatim from ``_docs/llm_portability_2.md`` §6),
# key-env resolver, extension-visibility flag, and - for named
# OpenAI-compatible providers - the hardcoded base_url / default key env
# var (referencing _NAMED_OPENAI_COMPATIBLE_DEFAULTS, never re-typed).
# EXTENSION_LLM_PROVIDER_ORDER / EXTENSION_LLM_CURATED_MODELS /
# EXTENSION_LLM_REGISTRY_KEYS below are derived from it - there are no
# independently-edited parallel literals.
PROVIDER_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "name": "anthropic",
        "registry_key": "anthropic",
        "curated_models": ["claude-sonnet-4-6", "claude-haiku-4-5"],
        "key_env_resolver": _resolve_api_key_env_var,
        "extension_visible": True,
    },
    {
        "name": "openai",
        "registry_key": "openai-compatible",
        "curated_models": ["gpt-4o", "gpt-4o-mini"],
        "key_env_resolver": _resolve_openai_api_key_env_var,
        "extension_visible": True,
    },
    {
        "name": "grok",
        "registry_key": "grok",
        "curated_models": ["grok-4", "grok-3-mini"],
        "key_env_resolver": _resolve_grok_api_key_env_var,
        "extension_visible": True,
        "base_url": _NAMED_OPENAI_COMPATIBLE_DEFAULTS["grok"]["base_url"],
        "default_key_env_var": _NAMED_OPENAI_COMPATIBLE_DEFAULTS["grok"][
            "api_key_env_var"
        ],
    },
    {
        "name": "opencode-zen",
        "registry_key": "opencode-zen",
        "curated_models": ["kimi-k2.6", "glm-5.3", "deepseek-v4-pro"],
        "key_env_resolver": _resolve_opencode_zen_api_key_env_var,
        "extension_visible": True,
        "base_url": _NAMED_OPENAI_COMPATIBLE_DEFAULTS["opencode-zen"]["base_url"],
        "default_key_env_var": _NAMED_OPENAI_COMPATIBLE_DEFAULTS["opencode-zen"][
            "api_key_env_var"
        ],
    },
    {
        # Hidden (#133): generic power-user endpoint (Ollama, vLLM, gateways) with arbitrary user-supplied base URL, so no meaningful curated model list exists for a popup dropdown.
        "name": "openai-compatible",
        "registry_key": "openai-compatible",
        "curated_models": [],
        "key_env_resolver": _resolve_openai_api_key_env_var,
        "extension_visible": False,
    },
    {
        # Hidden (#133): registered backend provider (#83) never extension-verified with no curated model list; exposing it would promise an untested popup path.
        "name": "gemini",
        "registry_key": "gemini",
        "curated_models": [],
        "key_env_resolver": _resolve_gemini_api_key_env_var,
        "extension_visible": False,
    },
    {
        # Hidden (#133): live request-shape verification blocked with no key (#119, doc-based only) and origin-prefixed model ids (e.g. anthropic/claude-3.5-sonnet) with no safe curated list for a popup dropdown.
        "name": "openrouter",
        "registry_key": "openrouter",
        "curated_models": [],
        "key_env_resolver": _resolve_openrouter_api_key_env_var,
        "extension_visible": False,
        "base_url": _NAMED_OPENAI_COMPATIBLE_DEFAULTS["openrouter"]["base_url"],
        "default_key_env_var": _NAMED_OPENAI_COMPATIBLE_DEFAULTS["openrouter"][
            "api_key_env_var"
        ],
    },
)

#: Providers exposed at ``GET /api/extension/llm-config/``, in fixed
#: display order. ``"openai"`` is the display name for the backend
#: registry key ``"openai-compatible"``; ``"opencode-zen"`` (issue #104)
#: is emitted only when that key exists in :data:`_PROVIDERS`, so this
#: endpoint needs no change when #104 lands. ``"openai-compatible"``,
#: ``"gemini"`` and ``"openrouter"`` never appear in the response.
#: Derived from :data:`PROVIDER_CATALOG` (issue #129).
EXTENSION_LLM_PROVIDER_ORDER: tuple[str, ...] = tuple(
    entry["name"] for entry in PROVIDER_CATALOG if entry["extension_visible"]
)

#: Curated model ids per exposed provider, verbatim from
#: ``_docs/llm_portability_2.md`` §6. Static domain data, hence here
#: beside :data:`_PROVIDERS` rather than in ``config/settings.py``.
#: Derived from :data:`PROVIDER_CATALOG` (issue #129).
EXTENSION_LLM_CURATED_MODELS: dict[str, list[str]] = {
    entry["name"]: list(entry["curated_models"])
    for entry in PROVIDER_CATALOG
    if entry["extension_visible"]
}


#: Display name → backend registry key in :data:`_PROVIDERS`. Only
#: ``"openai"`` differs (registry key ``"openai-compatible"``).
#: Derived from :data:`PROVIDER_CATALOG` (issue #129).
EXTENSION_LLM_REGISTRY_KEYS: dict[str, str] = {
    entry["name"]: entry["registry_key"]
    for entry in PROVIDER_CATALOG
    if entry["extension_visible"]
}

SUPPORTED_PROVIDERS = tuple(sorted(_PROVIDERS))


class _ProviderSpec(NamedTuple):
    """Shared construction/key spec for one registry key (issue #127).

    ``model_resolver`` / ``key_resolver`` / ``base_url_resolver`` are
    zero-arg callables returning the settings-derived values; the
    ``lambda: _resolve_*()`` wrappers (rather than bare function refs)
    resolve the module global at call time so ``monkeypatch.setattr``
    on the ``_resolve_*`` helpers keeps working. ``base_url_resolver``
    is ``None`` for adapters whose constructor takes no ``base_url``
    (``anthropic``, ``gemini``).
    """

    factory: Callable[..., Provider]
    model_resolver: Callable[[], str]
    key_resolver: Callable[[], str]
    base_url_resolver: Optional[Callable[[], str]]


#: One shared spec table driving :func:`get_provider` construction and
#: ``GET /api/extension/llm-config/`` key resolution. ``openai`` is the
#: short alias for ``openai-compatible`` (same resolvers); the named
#: OpenAI-compatible entries reuse the ``_resolve_*`` helpers that fall
#: back to :data:`_NAMED_OPENAI_COMPATIBLE_DEFAULTS`.
_PROVIDER_SPECS: dict[str, _ProviderSpec] = {
    "anthropic": _ProviderSpec(
        AnthropicProvider,
        lambda: _resolve_model(),
        lambda: _resolve_api_key_env_var(),
        None,
    ),
    "openai-compatible": _ProviderSpec(
        OpenAICompatibleProvider,
        lambda: _resolve_openai_model(),
        lambda: _resolve_openai_api_key_env_var(),
        lambda: _resolve_openai_base_url(),
    ),
    "openai": _ProviderSpec(
        OpenAICompatibleProvider,
        lambda: _resolve_openai_model(),
        lambda: _resolve_openai_api_key_env_var(),
        lambda: _resolve_openai_base_url(),
    ),
    "grok": _ProviderSpec(
        OpenAICompatibleProvider,
        lambda: _resolve_grok_model(),
        lambda: _resolve_grok_api_key_env_var(),
        lambda: _resolve_grok_base_url(),
    ),
    "openrouter": _ProviderSpec(
        OpenAICompatibleProvider,
        lambda: _resolve_openrouter_model(),
        lambda: _resolve_openrouter_api_key_env_var(),
        lambda: _resolve_openrouter_base_url(),
    ),
    "opencode-zen": _ProviderSpec(
        OpenAICompatibleProvider,
        lambda: _resolve_opencode_zen_model(),
        lambda: _resolve_opencode_zen_api_key_env_var(),
        lambda: _resolve_opencode_zen_base_url(),
    ),
    "gemini": _ProviderSpec(
        GeminiProvider,
        lambda: _resolve_gemini_model(),
        lambda: _resolve_gemini_api_key_env_var(),
        None,
    ),
}


def _model_override_or(current: str, override: Optional[str]) -> str:
    """Return the per-call model override when non-empty, else *current*.

    Resolved per-call with no settings/env mutation (issue #106).
    """
    if override is not None and str(override).strip():
        return str(override).strip()
    return current


def get_provider(
    name: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Provider:
    """Build the configured provider adapter.

    Raises :class:`LLMConfigError` for an unknown ``LLM_PROVIDER``.
    Optional per-call ``provider``/``model`` kwargs (issue #106) select
    the adapter / model id for one call only, with no settings mutation.
    ``model`` wins over the per-provider ``LLM_*_MODEL`` settings and the
    generic ``LLM_MODEL``.
    """
    raw_name = provider if provider is not None else name
    if raw_name is None:
        provider_name = (_resolve_provider_name() or "").strip().lower()
    elif not isinstance(raw_name, str):
        provider_name = str(raw_name).strip().lower()
    else:
        provider_name = raw_name.strip().lower() or (
            _resolve_provider_name() or ""
        ).strip().lower()
    spec = _PROVIDER_SPECS.get(provider_name)
    if spec is None:
        exc = LLMConfigError(
            f"Unknown LLM_PROVIDER {provider_name!r}. "
            f"Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
        )
        exc.attempted_provider_name = provider_name
        raise exc
    kwargs: dict[str, Any] = {
        "model": _model_override_or(spec.model_resolver(), model),
        "api_key_env_var": spec.key_resolver(),
    }
    if spec.base_url_resolver is not None:
        kwargs["base_url"] = spec.base_url_resolver()
    return spec.factory(**kwargs)


def check() -> None:
    """Fail loudly if the LLM client cannot make a call (bad provider or
    missing key). Cheap enough to call at startup or before a batch run."""
    get_provider().check()


def generate(
    *,
    system: str,
    prompt: str,
    response_format: Optional[dict] = None,
    max_tokens: Optional[int] = None,
    batch: Any = None,
    submitted_url: Any = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> LLMResult:
    """Send ``system`` + ``prompt`` to the configured LLM and return an
    :class:`LLMResult`.

    Pass ``response_format`` (a JSON Schema dict) to get ``result.parsed``
    back as a validated object; a non-conforming reply raises
    :class:`LLMBadResponseError`.

    Optional per-call ``provider``/``model`` kwargs (issue #106) use that
    provider/model for this call only, with no settings mutation.

    Every invocation records an ``LLMCall`` row (issue #28): the provider
    adapter records ok/failed outcomes, and this wrapper additionally
    records the failed row when no provider could even be built (unknown
    ``LLM_PROVIDER``). ``batch`` / ``submitted_url`` attribute the row;
    when omitted, the ambient :func:`call_context` applies.
    """
    try:
        provider_obj = get_provider(provider=provider, model=model)
    except LLMConfigError as exc:
        try:
            label = str(model).strip() if model is not None and str(model).strip() else _resolve_model()
        except Exception:  # noqa: BLE001 - best-effort label for the row
            label = ""
        _record_llm_call(
            model=label,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            status="failed",
            error_class=type(exc).__name__,
            provider=getattr(exc, "attempted_provider_name", "") or "",
            batch=batch,
            submitted_url=submitted_url,
        )
        raise
    return provider_obj.generate(
        system=system,
        prompt=prompt,
        response_format=response_format,
        max_tokens=_resolve_max_tokens(max_tokens),
        batch=batch,
        submitted_url=submitted_url,
    )
