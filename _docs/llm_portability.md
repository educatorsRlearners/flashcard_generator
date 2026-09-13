# LLM Provider Portability — Spec

## 1. Goal
The backend currently calls the Anthropic API directly for card generation
(term extraction, definitions, Q&A/cloze writing, few-shot feedback
injection). This spec covers making the LLM layer **config-driven**, so
switching providers is a config change, not a code change.

**Providers to support:**
- Anthropic (current default)
- OpenAI
- xAI (Grok)
- Google Gemini
- OpenCode Zen (multi-model gateway at opencode.ai)

## 2. Key Insight: Three Shapes, Not Five
Of the five providers, three speak the **OpenAI-compatible Chat Completions
format** (same request/response shape, just a different base URL and model
name):

| Provider | API shape | Base URL |
|---|---|---|
| OpenAI | OpenAI Chat Completions (native) | `https://api.openai.com/v1` |
| xAI (Grok) | OpenAI-compatible | `https://api.x.ai/v1` |
| OpenCode Zen | OpenAI-compatible | `https://opencode.ai/zen/v1` |
| Anthropic | Messages API (own format) | `https://api.anthropic.com/v1` |
| Gemini | Generative Language API (own format) | Google's endpoint |

So this only needs **three adapter implementations**, not five:
1. `AnthropicProvider`
2. `OpenAICompatibleProvider` (parameterized by base URL + model — reused for
   OpenAI, Grok, and OpenCode Zen)
3. `GeminiProvider`

## 3. Architecture: Adapter Pattern

```
llm/
  base.py          # LLMProvider abstract interface
  anthropic.py      # AnthropicProvider
  openai_compat.py  # OpenAICompatibleProvider (OpenAI, Grok, OpenCode Zen)
  gemini.py         # GeminiProvider
  factory.py        # get_provider() -> reads config, returns the right instance
```

**Common interface** (`base.py`):
```python
class LLMProvider(ABC):
    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        """Send a single-turn prompt, return raw text response."""
```

Every call site in the app (term extraction, card writing, definition
generation) goes through this interface — nothing calls Anthropic's SDK
directly anymore.

## 4. Config Design

**Environment variables** (`.env`, not committed):
```
LLM_PROVIDER=anthropic        # anthropic | openai | grok | gemini | opencode_zen
LLM_MODEL=claude-sonnet-4-6   # provider-specific model string

ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
XAI_API_KEY=...
GOOGLE_API_KEY=...
OPENCODE_API_KEY=...
```

- `LLM_PROVIDER` selects which adapter `factory.py` instantiates.
- `LLM_MODEL` is passed straight through to that provider — no validation
  against a hardcoded list, since model names change often. If the string
  is wrong, the provider's own error comes back at call time (see §6).
- Only the API key for the **selected** provider needs to be set; others can
  be blank.
- **OpenCode Zen naming note**: OpenCode Zen exposes a curated, coding-agent-
  optimized catalog under plain model IDs (e.g. `claude-sonnet-4-5`,
  `gpt-5.1`, `grok-code`, `gemini-3-pro`) — not provider-prefixed like some
  gateways. Worth a comment in `.env.example` linking to
  `https://opencode.ai/docs/zen/` since the catalog changes over time and
  some models are free/rate-limited differently than others.
- OpenCode Zen also offers an Anthropic-compatible endpoint
  (`https://opencode.ai/zen`, Messages API format) as an alternative to the
  OpenAI-compatible one — not used here, since routing it through
  `OpenAICompatibleProvider` keeps the adapter count at three.

## 5. Structured Output (JSON) Across Providers
Card generation needs reliable structured output (term, definition, card
type, cloze text, etc.) as JSON. Providers differ in how they guarantee
this (OpenAI/Grok/OpenCode Zen support a JSON-mode flag; Gemini has its own;
Anthropic relies on prompting + tags).

**Decision: don't depend on provider-specific JSON-mode features.** Instead:
- Prompt every provider identically: "Respond with only valid JSON matching
  this schema: ..." (schema described in the prompt itself).
- Parse the response defensively: strip markdown code fences if present,
  attempt `json.loads`, and on failure, retry once with an explicit
  "your last response wasn't valid JSON, return only JSON" follow-up.
- This keeps the prompt layer identical across all five providers — the
  adapter difference is purely in how the HTTP call is made, not in prompt
  content.

## 6. Error Handling
Normalize provider errors into a small set of app-level exceptions so the
rest of the code doesn't need to know which provider is active:
- `LLMAuthError` — bad/missing API key
- `LLMRateLimitError` — rate limited, safe to retry with backoff
- `LLMBadResponseError` — unparseable output after retry
- `LLMProviderError` — catch-all for anything else (bad model string,
  network failure, provider outage)

Each adapter catches its own SDK/HTTP exceptions and re-raises as one of
these, so error-handling code elsewhere is provider-agnostic.

## 7. Validating a Provider Config
Add a lightweight check (e.g. `make check-llm` or a Django management
command) that sends a trivial prompt ("reply with the word OK") to the
currently configured provider and reports success/failure with a clear
message — so switching `LLM_PROVIDER` in `.env` can be verified in one
command before running a real generation batch.

## 8. Migration Steps
1. Extract current Anthropic-calling code into `llm/anthropic.py` behind the
   `LLMProvider` interface — no behavior change yet, just the abstraction.
2. Add `llm/factory.py` reading `LLM_PROVIDER` from config; default to
   `anthropic` so nothing breaks for existing setups.
3. Add `llm/openai_compat.py`, parameterized by base URL — wire up OpenAI
   first (most testable), then confirm Grok and OpenCode Zen work by only
   changing the base URL and key.
4. Add `llm/gemini.py` separately, since its request/response shape differs
   most.
5. Update all call sites (term extraction, card generation, few-shot
   feedback injection) to use `factory.get_provider()` instead of importing
   Anthropic's SDK directly.
6. Add the `check-llm` validation command.
7. Update `.env.example` with all five provider variables and the
   OpenCode Zen naming note.

## 9. Out of Scope (for now)
- Streaming responses (not currently used in the generation pipeline).
- Vision/multimodal input to the LLM (image handling stays in the separate
  local Stable Diffusion path, not the text LLM).
- Local/open-weight models via Ollama or LM Studio (previously decided
  against — config-driven cloud providers only).
- Per-provider prompt tuning (e.g. writing a Gemini-specific prompt for
  better results) — one shared prompt template across all providers, at
  least for the MVP of this change.
- Automatic fallback between providers on failure (e.g. retry with a
  different provider if one is down) — single configured provider only.

## 10. Open Questions for Build Phase
- Do Grok and OpenCode Zen need any request-shape tweaks beyond base URL/model
  (e.g. different max_tokens field name, different auth header), or are
  they fully drop-in with the OpenAI SDK?
- Should `LLM_MODEL` have a sane per-provider default if left blank in
  `.env`, or should a missing value just be a hard config error?
- Does the JSON-schema-in-prompt approach hold up across all five providers
  without per-provider retry-rate differences becoming a UX issue (i.e. does
  one provider need the retry-on-bad-JSON path far more than others)?