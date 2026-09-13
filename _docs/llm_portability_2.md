# Popup: LLM Provider/Model Selector — Spec

> Builds on `llm-provider-portability-spec.md`. That spec made the backend
> config-driven via `.env`. This spec adds a UI in the extension popup so the
> provider/model can be chosen per-use, overriding the `.env` default.

## 1. Goal
Add a provider + model selector to the extension popup, next to the
**"Generate cards from this page"** button, so you can switch between
Anthropic, OpenAI, Grok, and OpenCode Zen without editing `.env` or
restarting the backend.

**Browser note**: target is Brave, not Chrome. Brave is Chromium-based and
supports Manifest V3 unpacked extensions identically to Chrome — no
architecture changes needed anywhere in this spec or the extension pivot
spec. Loading the extension is the same `brave://extensions` → "Load
unpacked" flow as `chrome://extensions`.

## 2. UI Layout (Popup)
```
┌─────────────────────────────────┐
│  Provider:  [ Anthropic     ▾]  │
│  Model:     [ claude-sonnet ▾]  │
│                                  │
│  ⚠ (shown only if key missing)  │
│                                  │
│  [ Generate cards from this page ]
└─────────────────────────────────┘
```
- **Provider dropdown**: Anthropic, OpenAI, Grok, OpenCode Zen.
- **Model dropdown**: dependent on provider — updates its options whenever
  the provider changes. Populated from a small curated list of
  recommended models per provider (not free text, not a live-fetched list).
- **Warning banner**: appears only if the selected provider's API key isn't
  configured on the backend (see §5). Generate button is disabled while
  it's showing.

## 3. Behavior
- **Persistence**: last-selected provider and model are remembered across
  popup opens, stored in `chrome.storage.local` (extension-side, not synced
  to the backend/SQLite — this is a UI preference, not app data).
- **Default on first use**: whatever `.env`'s `LLM_PROVIDER`/`LLM_MODEL`
  currently is, fetched once from the backend (see §4) to seed the initial
  dropdown state.
- **Override behavior**: whatever is selected in the popup is sent with
  *every* generation request and **always overrides** the backend's `.env`
  default — `.env` only matters as the very first seed value and as a
  fallback if the backend is ever called without a provider/model
  specified (e.g. a future non-extension client).

## 4. Backend Changes Needed
1. **`GET /api/llm-config`** — returns:
   - the list of supported providers,
   - the curated model list per provider (see §6),
   - which providers currently have a valid API key configured,
   - the current `.env` default (provider + model), for seeding the popup
     on first install.
2. **Generation endpoint update** — the existing "generate cards from this
   content" endpoint now accepts optional `provider` and `model` fields in
   the request body. If present, they override `LLM_PROVIDER`/`LLM_MODEL`
   from `.env` for that call only (no state change on the backend side —
   this is a per-request override, not a config mutation).

## 5. API Key Validation
- On popup open, call `GET /api/llm-config` to get key-presence status per
  provider (boolean per provider — never returns the actual key).
- If the currently selected provider has no key configured:
  - Show a warning banner: *"No API key configured for \[Provider] — add
    it to your backend `.env` and restart."*
  - Disable the **Generate** button until a provider with a valid key is
    selected.
- This check is a simple presence check (is the env var set and
  non-empty), not a live test call to the provider — keeps popup load
  fast. The existing `check-llm` Makefile target (from the earlier spec)
  remains the tool for verifying a key actually *works*.

## 6. Curated Model Lists (initial set)
Small, hand-maintained list per provider — not fetched live from each
provider's API (avoids extra latency/failure modes in the popup, and model
catalogs don't change often enough to justify it).

| Provider | Models offered in dropdown |
|---|---|
| Anthropic | `claude-sonnet-4-6`, `claude-haiku-4-5` |
| OpenAI | `gpt-4o`, `gpt-4o-mini` |
| Grok | `grok-4`, `grok-3-mini` |
| OpenCode Zen | `claude-sonnet-4-5`, `gpt-5.1`, `grok-code` |

This list lives in one place on the **backend** (not hardcoded in the
extension) so updating it doesn't require reloading the unpacked extension
— it's returned by `GET /api/llm-config` from §4.

## 7. Data Flow Summary
1. Popup opens → `GET /api/llm-config` → populate dropdowns, seed from
   `.env` default on first-ever use, otherwise restore from
   `chrome.storage.local`.
2. User changes provider → model dropdown repopulates from the curated
   list for that provider → selection saved to `chrome.storage.local`
   immediately (no separate "Save" button/action).
3. User clicks **Generate cards from this page** → request to backend
   includes `provider` and `model` alongside the extracted page content.
4. Backend's `llm/factory.py` (from the portability spec) uses the
   request's `provider`/`model` if present, falling back to `.env` only if
   they're absent.

## 8. Out of Scope (for now)
- Editing/adding API keys from the popup (still `.env`-only, edited
  manually + backend restart).
- Live-fetching each provider's full model catalog.
- Per-card provider choice (one provider/model per generation run, not
  per card).
- Syncing the remembered choice across multiple machines/browsers (local
  `chrome.storage.local` only).
- Live key validation (real API test call) from the popup — presence
  check only; use `make check-llm` for an actual test.

## 9. Open Questions for Build Phase
- Exact `chrome.storage.local` schema (e.g. `{ provider: "anthropic",
  model: "claude-sonnet-4-6" }`).
- Whether `GET /api/llm-config` needs CORS/auth handling given it's
  called from the extension to `localhost` — likely fine for local-only
  use but worth a explicit check.
- Who maintains the curated model list over time (manual edit in backend
  config) and how often it needs revisiting as providers release new
  models.