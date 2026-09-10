"""Thin, provider-agnostic LLM client.

The rest of the app calls :func:`generate` (or constructs a provider via
:func:`get_provider`) and never imports a provider SDK directly.  Which
provider and model are used is read from Django settings:

* ``LLM_PROVIDER``      - registry key, default ``"anthropic"``
* ``LLM_MODEL``         - model id, default ``"claude-sonnet-5"``
* ``LLM_API_KEY_ENV_VAR`` - name of the env var holding the API key
* ``LLM_MAX_TOKENS``    - default output-token ceiling

Adding a second provider (issue #27) is one entry in ``_PROVIDERS`` plus a
new ``Provider`` subclass in this file - nothing else in the codebase
changes, and ``import anthropic`` stays confined to this module.

Nothing here logs or embeds the API key in an exception message.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterator, Optional

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


# --- Provider interface ----------------------------------------------


class Provider:
    """A provider adapter. Subclasses implement :meth:`_call` and
    :meth:`_map_exception`; the retry loop and response handling live here."""

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
        try:
            self._require_api_key()

            attempt = 0
            while True:
                attempt += 1
                try:
                    raw = self._call(
                        system=system,
                        prompt=prompt,
                        response_format=response_format,
                        max_tokens=max_tokens,
                    )
                except LLMError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - provider SDK error
                    mapped = self._map_exception(exc)
                    if _is_retryable(mapped) and attempt < MAX_ATTEMPTS:
                        _sleep(_backoff_seconds(attempt, mapped))
                        continue
                    raise mapped from None
                result = self._build_result(raw, response_format)
                break
        except LLMError as exc:
            _record_llm_call(
                model=self.model,
                input_tokens=0,
                output_tokens=0,
                latency_ms=int((time.perf_counter() - start) * 1000),
                status="failed",
                error_class=type(exc).__name__,
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

    def _map_exception(self, exc: BaseException) -> LLMError:  # pragma: no cover
        raise NotImplementedError

    def _extract(self, raw: Any) -> tuple[str, Optional[str], dict]:
        """Return (text, stop_reason, usage) from a provider response."""
        raise NotImplementedError  # pragma: no cover

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

    def _map_exception(self, exc: BaseException) -> LLMError:
        return _classify_provider_exception(exc)


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
        return LLMTransientError("request timed out", reason="timeout")
    if "APIConnection" in name or "Connection" in name:
        return LLMTransientError("connection error contacting provider", reason="connection")
    if isinstance(status, int) and status >= 500:
        return LLMTransientError(f"provider server error (HTTP {status})", reason="server_error")
    if isinstance(status, int) and 400 <= status < 500:
        return LLMBadResponseError(
            f"provider rejected the request (HTTP {status})", reason="bad_request"
        )
    # Unknown shape: treat as transient so a blip is retried, but bounded.
    return LLMTransientError(f"unexpected provider error ({name})", reason="unknown")


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


def _parse_structured(text: str, schema: dict) -> Any:
    try:
        obj = json.loads(text)
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
    batch: Any = None,
    submitted_url: Any = None,
) -> None:
    """Persist one :class:`submissions.models.LLMCall` row (issue #28).

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


# --- Provider registry (the seam #27 extends) --------------------


#: The one obvious place a new provider is wired in. Key == ``LLM_PROVIDER``.
_PROVIDERS: dict[str, Callable[..., Provider]] = {
    "anthropic": AnthropicProvider,
}

SUPPORTED_PROVIDERS = tuple(sorted(_PROVIDERS))


def get_provider(name: Optional[str] = None) -> Provider:
    """Build the configured provider adapter.

    Raises :class:`LLMConfigError` for an unknown ``LLM_PROVIDER``.
    """
    provider_name = (name or _resolve_provider_name() or "").strip().lower()
    factory = _PROVIDERS.get(provider_name)
    if factory is None:
        raise LLMConfigError(
            f"Unknown LLM_PROVIDER {provider_name!r}. "
            f"Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    return factory(model=_resolve_model(), api_key_env_var=_resolve_api_key_env_var())


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
) -> LLMResult:
    """Send ``system`` + ``prompt`` to the configured LLM and return an
    :class:`LLMResult`.

    Pass ``response_format`` (a JSON Schema dict) to get ``result.parsed``
    back as a validated object; a non-conforming reply raises
    :class:`LLMBadResponseError`.

    Every invocation records an ``LLMCall`` row (issue #28): the provider
    adapter records ok/failed outcomes, and this wrapper additionally
    records the failed row when no provider could even be built (unknown
    ``LLM_PROVIDER``). ``batch`` / ``submitted_url`` attribute the row;
    when omitted, the ambient :func:`call_context` applies.
    """
    try:
        provider = get_provider()
    except LLMConfigError as exc:
        try:
            model = _resolve_model()
        except Exception:  # noqa: BLE001 - best-effort label for the row
            model = ""
        _record_llm_call(
            model=model,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            status="failed",
            error_class=type(exc).__name__,
            batch=batch,
            submitted_url=submitted_url,
        )
        raise
    return provider.generate(
        system=system,
        prompt=prompt,
        response_format=response_format,
        max_tokens=_resolve_max_tokens(max_tokens),
        batch=batch,
        submitted_url=submitted_url,
    )
