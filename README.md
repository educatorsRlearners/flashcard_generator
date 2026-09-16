<h1 align="center">Flashcard Generator</h1>

<p align="center">
  <img src="assets/hero-banner.svg" alt="Flashcard Generator: turn any web page into reviewed, spaced-repetition Anki flashcards">
</p>

<p align="center">
  <em>Turn any web page into reviewed, spaced-repetition Anki flashcards.</em>
</p>

> A Brave/Chrome (MV3) browser extension that generates Anki flashcards from
> the current page, reviewed before syncing via AnkiConnect. The extension
> popup on the page you are reading is how pages are submitted.

> **Maintenance note:** the provider table ([LLM client](#llm-client)) and
> [Current Limitations](#current-limitations) below are the most-likely-stale
> sections of this file. Source of truth for both is `PROVIDER_CATALOG` in
> `submissions/llm.py` (served to the popup via
> `GET /api/extension/llm-config/`).

<p align="center">

![Python](https://img.shields.io/badge/Python-4338CA?style=flat-square)
![Django](https://img.shields.io/badge/Django-6366F1?style=flat-square)
![SQLite](https://img.shields.io/badge/SQLite-4338CA?style=flat-square)
![LLM Anthropic • OpenAI • Grok • Gemini • OpenRouter • Zen](https://img.shields.io/badge/LLM_Anthropic_%E2%80%A2_OpenAI_%E2%80%A2_Grok_%E2%80%A2_Gemini_%E2%80%A2_OpenRouter_%E2%80%A2_Zen-7C3AED?style=flat-square)
![Huey](https://img.shields.io/badge/Huey-4338CA?style=flat-square)
![Playwright](https://img.shields.io/badge/Playwright-6366F1?style=flat-square)
![Sentence Transformers](https://img.shields.io/badge/Sentence%20Transformers-7C3AED?style=flat-square)
![AnkiConnect](https://img.shields.io/badge/AnkiConnect-4338CA?style=flat-square)
![Chrome Extension (MV3)](https://img.shields.io/badge/Chrome%20Extension%20(MV3)-6366F1?style=flat-square)

</p>

## Table of Contents

- [What it is](#what-it-is)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Getting started](#getting-started)
- [Extension internals](#extension-internals)
- [Configuration & providers](#configuration--providers)
- [Setup](#setup)
- [Run](#run)
- [Background processing (Huey)](#background-processing-huey)
- [Extract content](#extract-content)
- [Generate cards](#generate-cards)
- [Deduplicate cards](#deduplicate-cards)
- [Card images](#card-images)
- [Review feedback (durable) + few-shot injection](#review-feedback-durable--few-shot-injection)
- [Review grid](#review-grid)
- [Push to Anki](#push-to-anki)
- [LLM client](#llm-client)
- [LLM usage (cost / latency observability)](#llm-usage-cost--latency-observability)
- [Current Limitations](#current-limitations)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

## What it is

Turning a web page into flashcards you'll actually review is normally
manual and slow: read it, decide what's worth remembering, write a
front/back pair, find an image, load it into your spaced-repetition tool —
one at a time, for every source. This app automates that end to end: a
Brave/Chrome browser extension is the front end — click its popup on a
page you're reading, and a Django backend (reached through a native
messaging host, never a browser tab) extracts the text, generates
flashcards, dedupes and images them, and lets you accept, reject, or edit
every card in a review grid before pushing the accepted ones into Anki.
Nothing reaches Anki without going through review first. See
[Getting started](#getting-started) below to install and try it.

## How it works

1. Click the extension icon on the page you are reading.
2. Confirm the popup (provider/model selection).
3. The backend generates Q&A + cloze cards with images from the page text.
4. Review each card in the review tab — Accept or Reject per card.
5. Pick the Anki deck on the review page and Finish — the accepted cards
   are pushed to that batch's stored deck via AnkiConnect (per-batch deck,
   never a single global deck).

## Requirements

- macOS Apple Silicon
- Brave/Chrome with MV3 extension support
- Anki running with the AnkiConnect add-on installed
- Local Stable Diffusion / Draw Things — optional fallback for card images
  (cards are still produced with no image when it is not running)
- API key for at least one LLM provider (see
  [LLM client](#llm-client))

## Getting started

One-time install, then every-day use — top to bottom, no other section
needed to finish this once.

**Install** (macOS only — this repo's development and documented setup are
macOS-only; Linux/Windows native-messaging support is tracked separately
in #43):

1. ```
   make setup
   ```
   One command takes a fresh checkout to extension-ready: installs deps
   (`uv sync`), applies migrations, pins a real signing key into
   `extension/manifest.json`'s `"key"` field (private key to gitignored
   `.extension_signing_key.pem`), registers the native messaging host,
   mints the extension auth token (`.extension_token`), writes
   `EXTENSION_ID=<derived-id>` into `.env` (creating `.env` from
   `.env.example` first when missing), presence-checks the configured LLM
   key (fails fast naming the exact env var — no network call), and
   finishes with `make check`. Re-running is safe: present artifacts are
   skipped, the key/token are never regenerated, and the `.env` line is
   updated in place. Start the backend day to day with `make run` (same
   fast ensure, then `runserver` + Huey consumer together).
   Heavier optional pieces (Playwright/Chromium, sentence-transformer
   weights, Tesseract) aren't required for this basic flow — see
   [Setup](#setup) if a feature later asks for one of them.
   Manual fallback (the same steps by hand, no `make`):
   ```
   uv sync
   uv run python manage.py migrate
   uv run python manage.py generate_signing_key
   uv run python manage.py install_native_host
   ```
   Re-running `generate_signing_key` once a real key is already pinned
   fails unless you pass `--force` (which regenerates both keys and gives
   the extension a new ID — repeat step 2 and re-run
   `install_native_host` if you do this); `make setup` never passes
   `--force`.
2. Load the extension unpacked: `brave://extensions` (or
   `chrome://extensions`) → enable Developer mode → "Load unpacked" →
   select the `extension/` directory. Because the key is pinned in step 1,
   the ID Brave/Chrome assigns stays stable across future reloads — you
   don't need to copy it down, step 1 already derived it itself (printed
   as `Extension ID derived from extension/manifest.json: <id>` so you
   can cross-check it against the ID shown on
   `brave://extensions`/`chrome://extensions`). Full detail on what step 1
   wires up lives in [Extension internals](#extension-internals).

   Pass `--extension-id <id>` to `install_native_host` explicitly only to
   override the derived ID (e.g. testing/multi-profile setups) — if it
   disagrees with the ID derived from the manifest, a warning naming both
   is printed but the explicit value still wins.

   **Restart any already-running backend** (`manage.py dev`, or
   `runserver`/`run_huey` started manually) after this — `.env` is only
   read once at process start, so a live process keeps using its old
   `EXTENSION_ID` until restarted. `make setup` / `make run` print this
   reminder (plus the derived ID) every time.

   If Brave/Chrome was already open when the manifest was written, reload
   the extension once more before using it.

**Use:**

3. Click the extension's popup on any regular webpage you're reading
   (`brave://` and `chrome://` and extension pages themselves are unreadable). This
   extracts the page and hands it to the backend — see
   [Extract content](#extract-content).
4. The backend turns the extracted text into flashcards (see
   [Generate cards](#generate-cards)), filters out ones that duplicate
   cards you already have (see [Deduplicate cards](#deduplicate-cards)),
   and attaches an image to each (see [Card images](#card-images)).
5. The popup opens the batch's review grid. Accept, reject, or edit each
   card — see [Review grid](#review-grid).
6. Pick the Anki deck on the review page (dropdown of live Anki decks plus
   free-text new name; typed name wins) and click **Finish**. This stores
   the deck on the batch and enqueues a background push of the accepted
   cards to that deck — see [Push to Anki](#push-to-anki). The outcome
   (in progress / pushed / unreachable / failed) is shown as a banner on
   the review page.

If anything above doesn't behave as described, the full manual
verification checklist (cold start, error cases, review-tab regression)
is in `_docs/extension_manual_checklist.md`.

### Commands at a glance

| Command | What it does |
| --- | --- |
| `make setup` | One-command extension-ready setup: deps, migrations, signing key, native host + token + `EXTENSION_ID`, LLM-key presence check, then `make check` (idempotent; primary path) |
| `make run` | Fast-ensure the same artifacts, then start `runserver` + the Huey consumer together, prefixed logs (`[web]` / `[worker]`), Ctrl-C stops both (primary path) |
| `make check` | Verify non-pip prerequisites (Anki / local image gen warn only) |
| `make install` | Deps + migrations only (`make setup` covers this and more) |
| `uv sync` | Install dependencies (manual fallback for the `make setup` step) |
| `uv run pytest` | Run the whole test suite |
| `uv run python manage.py dev` | Dev entrypoint: starts `runserver` + the Huey consumer together, prefixed logs (`[web]` / `[worker]`), Ctrl-C stops both (manual fallback for `make run`) |
| `uv run python manage.py run_huey` | Run the background task consumer alone (manual fallback; normally started automatically by `dev`) |
| `uv run python manage.py push_to_anki` | Push accepted cards to Anki via AnkiConnect (idempotent) |

## Extension internals

The extension (`extension/`) reaches the backend through a native
messaging host (`native_host/host.py`, #37), which Brave/Chrome discover
via a manifest file registered on your machine by
`install_native_host` — see [Getting started](#getting-started) for the
one-time setup steps. Reference detail on how the pieces talk to each
other:

`install_native_host` derives the extension ID itself from
`extension/manifest.json`'s pinned `"key"` field, using the same
deterministic algorithm Chrome/Brave use (SHA-256 of the key's DER bytes,
first 16 bytes, hex nibbles mapped through `0123456789abcdef` →
`abcdefghijklmnop`) — so a real key must be pinned first (`generate_signing_key`,
step 2 above) or the command exits with a `CommandError` telling you to
run it. Pass `--extension-id <id>` explicitly to override the derived
value instead (e.g. testing/multi-profile setups); if a real pinned key
disagrees with the explicit value, a warning naming both is printed but
the explicit `--extension-id` still wins. Re-running is always safe, with
or without `--extension-id`: it overwrites `native_host/run_host.sh` (a
wrapper script with an absolute interpreter path baked in), the
native-messaging manifest(s), and the `.env` line in place. Native
messaging host manifests are read fresh per `connectNative` call, but a
stale `brave://extensions` / `chrome://extensions` page may not reflect a just-loaded ID — if the
popup reports it can't connect, reloading the extension is the fix.

### Auth token + `EXTENSION_ID`

Extension requests authenticate with a local shared-secret bearer token
(`Authorization: Bearer <token>`, #33), minted on first installer run and
stored git-ignored in `.extension_token` (`EXTENSION_TOKEN_FILE`):

```
uv run python manage.py extension_token --mint    # first time (fails if one exists)
uv run python manage.py extension_token --show    # print current token
uv run python manage.py extension_token --rotate  # replace it
```

CORS is hand-rolled (no dependency): every API response carries
`Access-Control-Allow-Origin: chrome-extension://<EXTENSION_ID>` only when
`EXTENSION_ID` (env var, `config/settings.py`) is set to the loaded
extension's ID. `install_native_host` sets this for you automatically in
`.env` — there's no need to set it by hand. Unset or stale → header
omitted (fail closed, browser blocks the response). If the popup reports
it cannot reach the backend on first run, check this value first: a
backend process already running when `.env` was updated keeps using its
old `EXTENSION_ID` until restarted, and a real `EXTENSION_ID` shell
environment variable takes precedence over `.env` and must be
updated/unset too. The native host reads the token file and passes the
token to the popup, so you never copy it by hand.

### Using it + API

Click **Generate** in the popup on a regular webpage (`brave://` and `chrome://` and
extension pages are unreadable): the popup gets token + `base_url` from
the native host (spawning `manage.py dev` if needed), injects
Readability + `content_extract.js`, POSTs `{url, title, text, images}` to
`POST /api/extension/submit/` (auth + `EXTENSION_ID` CORS as above, 202
with `{batch_id, submitted_url_id}`), polls
`GET /api/extension/submit/<id>/status/` every 1 s until `terminal: true`,
then opens `review_url`. Card generation is chained automatically
(`process_extension_submission`). Closing the popup mid-flow aborts it;
re-clicking starts a fresh submission safely.

### Backend port configurability (BACKEND_URL)

`BACKEND_URL` (default `http://127.0.0.1:8000`) is the single env var read
by both `native_host/host.py` (readiness probe, the `--addrport` it spawns
`manage.py dev` with, and the `base_url` it returns to the extension) and
`submissions/management/commands/dev.py` (its `--addrport` default, also
exposed as `config/settings.py`'s `BACKEND_URL`). The shared name+default
is what keeps them from drifting. An explicit `dev --addrport` flag always
wins over the env var.

To run everything on another port (e.g. 9000):

1. Start the backend with the env var set:
   ```
   BACKEND_URL=http://127.0.0.1:9000 uv run python manage.py dev
   ```
2. Make the same value visible to the native host. The host is launched by
   Brave/Chrome, so it reads the Brave/Chrome environment, not your terminal's — launch
   Brave/Chrome from a terminal with the var set (e.g.
   `BACKEND_URL=http://127.0.0.1:9000 open -a "Brave Browser"` or
   `BACKEND_URL=http://127.0.0.1:9000 open -a "Google Chrome"`), or set it
   persistently for GUI apps.
3. Hand-edit `extension/manifest.json`'s `host_permissions` to match the
   new origin exactly (MV3 permissions are static at load time, so this
   cannot be picked up at runtime):
   ```
   "host_permissions": ["http://127.0.0.1:9000/*"],
   ```
   then reload the extension at `brave://extensions` (or `chrome://extensions`) (Developer mode →
   Reload). No code or permission-prompt flow is involved.
4. Re-run the manual checklist (`_docs/extension_manual_checklist.md`);
   the popup follows the host's `base_url` with no other change.

Mismatch policy: the host's configured URL always wins. If it answers, the
extension is pointed at it (`already_running`), even if you also started a
backend by hand on a different port — that other backend is ignored, not
adopted. If the configured URL is down, the host spawns its own backend on
the matching port, even if another port answers. Keep both sides on the
same `BACKEND_URL` (or pass `dev --addrport` explicitly) to avoid running
two backends unknowingly.

## Configuration & providers

Configuration is split three ways:

- **`config/settings.py`** — checked-in Django settings with sensible
  defaults. Each one is also backed by an environment variable of the same
  name, so it can be overridden with no code change.
- **`.env`** (git-ignored) — secrets and local overrides, read via
  `python-dotenv` and loaded by `config/settings.py`.
- **Real shell environment variables** — take precedence over `.env`
  (`load_dotenv`'s default `override=False`). Useful for one-off overrides
  or when a value (like `EXTENSION_ID`) needs to be visible to another
  process (e.g. Chrome launching the native messaging host).

Copy the template and fill in your key to get started:

```bash
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

Which LLM provider and model are used, and every other per-feature setting
(dedup threshold, image fallback, Anki deck name, few-shot budget, and so
on), is documented alongside the feature it configures below — see
[LLM client](#llm-client) for the provider/model settings table, or jump to
any section's own settings table.

`.env` keys for provider/model selection:

| Setting | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `anthropic` | Backend provider registry key (see provider table in [LLM client](#llm-client)) |
| `LLM_MODEL` | `claude-sonnet-5` | Model id passed to the provider (per-provider `LLM_<NAME>_MODEL` overrides win when set) |

Per-provider API-key env vars are listed in the provider table in
[LLM client](#llm-client). The extension popup's provider/model selection
overrides `.env` per generation: the popup sends `provider` / `model`
payload fields (`chosenLlm()` / `submitContent()` in `extension/popup.js`)
and the backend stores them as a per-submission override
(`llm_provider_override` / `llm_model_override` on the submission),
never mutating settings or `.env`.

## Setup

First-time setup uses the verified Makefile targets (all exist in
`Makefile`) — `make setup` is the primary path, `uv` commands below it
are the manual fallback:

```
make setup    # deps, migrations, signing key, native host + token + EXTENSION_ID, LLM-key check, then check prerequisites
make check    # verify non-pip prerequisites (Anki / local image gen warn only)
make run      # fast-ensure the same artifacts, then start runserver + Huey consumer together (same as manage.py dev)
```

Then load the extension unpacked: `brave://extensions` (or
`chrome://extensions`) → enable Developer mode → "Load unpacked" → select
the `extension/` directory. The full one-time flow (signing key, native
host, extension ID) is [Getting started](#getting-started) above.

Optional heavier pieces (sentence-transformer weights for semantic dedup,
Tesseract for OCR, Draw Things for fallback images) are not required for
the basic extension flow — see the feature sections below if one of them
asks for it. When needed:

```
uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

(one-time weights download for semantic dedup; tests stub the encoder).
Per-card images use `pillow` + `httpx` (installed by `uv sync`); fallback
generation additionally assumes a running **Draw Things** with its HTTP API
server enabled (Draw Things → Settings → API Server). If Draw Things is
not running the card is simply produced with no image — nothing aborts.
Playwright/Chromium exists only as a server-side fallback for
the extraction path (see [Extract content](#extract-content)).

## Run

The extension is how you use this app day to day (see
[Getting started](#getting-started)); the native host spawns the backend
for you. This section is for a contributor who wants to run the backend
directly — e.g. to work on it without going through the extension.
`make run` (primary path) fast-ensures the setup artifacts first
(deps/migrations/key/native host/LLM key, skipping present ones), then
starts `dev`; the `uv` commands below are the manual fallback:

```
uv run python manage.py migrate
uv run python manage.py dev
```

`dev` is a development-only supervisor (stdlib only): it starts
`runserver` and the Huey consumer below together in one terminal, prefixes
their output (`[web]` / `[worker]`), restarts a crashed consumer
automatically (a crash-looping consumer stops everything with exit 1), and
stops both on Ctrl-C with no orphans. Do NOT run `runserver --nothreading`
(or `dev` against one): the single-threaded server would serialize
long-lived connections (such as the extension's status polling) against
normal requests and hang. Production/WSGI and `uv run pytest` never spawn a
consumer (tests run Huey tasks eagerly in-process).

## Background processing (Huey)

Each extension submission enqueues background work that runs the
extraction path (below). The tasks are processed by a Huey consumer backed
by a local SQLite file (`huey.sqlite3`) — no Redis or extra service. The
`dev` command above starts the consumer automatically, so a submission
moves out of `pending` with no further step. Manual
fallback in a second terminal (if you run `runserver` on its own):

```
uv run python manage.py run_huey
```

`runserver` on its own does **not** start it (use `dev` instead).
If the consumer is
not running, a submission stays in `pending` (the contributor-facing batch
page shows this state with a notice
and this command). Pending tasks are persisted, so restarting the consumer
after a crash resumes them. To run tasks inline without a consumer (e.g. a
one-off script), set `HUEY_IMMEDIATE=1`.

## Extract content

The extension popup on the page you are reading is the primary workflow
(pages arrive with `extraction_method = extension`, see below). The
commands in this section are contributor/diagnostic tools for the
server-side extraction path — not the way day-to-day use submits pages.

Fetch and extract the main body text for submitted URLs:

```
uv run python manage.py extract_content --url https://example.com/article
uv run python manage.py extract_content --batch 1
uv run python manage.py extract_content            # all rows not yet extracted
uv run python manage.py extract_content --batch 1 --force   # re-extract
uv run python manage.py extract_content --url ... --ignore-robots  # skip robots.txt (local only)
```

The static path (`trafilatura`) is tried first; the server-side browser
fallback (headless Chromium) runs
automatically when the static text has fewer than 200 non-whitespace
characters. Results land in the `extracted_text` / `extracted_title` /
`extraction_method` / `extracted_at` fields and are visible in the admin.

A URL that resolves to a PDF (`application/pdf`, or a `%PDF-` body served
as `application/octet-stream`) or a Word `.docx` is routed to the
`document` extraction path instead: its text is extracted directly and the
browser fallback is never used. A PDF with no text layer (scanned /
image-only) is handed to the OCR path below instead of failing as
`no_content`. Other non-HTML types (`application/zip`, `text/csv`,
legacy `.doc`, …) still fail with `unsupported_type`.

### Image OCR

A URL that *is* an image (`image/png`, `image/jpeg`, `image/webp`,
`image/tiff` — or the matching URL extension served as
`application/octet-stream` with recognised image magic bytes) is routed to
the `ocr` extraction path, as is an image-only / scanned PDF whose text
layer extracted fewer than 200 non-whitespace characters. Recognised text
lands in `extracted_text` with `extraction_method = ocr` (`extracted_title`
stays empty — images expose no title); multi-page scans are concatenated
in page order. The browser fallback is never used for image or OCR-routed
content, and the 10 MB body cap still applies to image downloads.

The OCR toolchain has two parts:

1. **Native binary (outside `uv`):** install Tesseract —
   `brew install tesseract` (macOS).
2. **Python binding (inside `uv`):** `pytesseract` is already a dependency
   (installed by `uv sync`; pillow, needed to decode images, likewise).

OCR is enabled by default (`OCR_ENABLED=1`) and is purely local — no
server, no API key. When the toolchain is absent (binding not installed or
`tesseract` not on `PATH`) or disabled (`OCR_ENABLED=0`), image URLs fail
cleanly with `failure_reason` naming OCR as an optional component and
pointing here; the command continues to the next URL and exits 0. The same
applies when a scan cannot be rasterized (no `pymupdf`/`pdf2image`
installed) or recognition yields nothing readable (`no_content` /
"no readable text"). One file can occupy the worker for at most
`OCR_TIMEOUT_SECONDS` (default 60 s); hitting it is a clean `timeout`
failure, not a hang.

### Fetch politeness

Every fetch on the extraction path (static, document, and browser fallback)
is polite:

- **robots.txt** — before fetching a URL the host's
  `https://<host>/robots.txt` is retrieved (stdlib `urllib.robotparser`),
  parsed, and cached per host for an hour. A URL disallowed for our
  User-Agent is not fetched and fails with
  `failure_kind = blocked_by_robots` / `failure_reason = "disallowed by
  robots.txt"`. A missing, empty, or unreachable `robots.txt` is treated as
  "allow all" (fail open) and never fails the URL itself. Pass
  `--ignore-robots` to skip this check for local testing.
- **Per-domain rate limiting** — consecutive fetches to the same registrable
  domain (eTLD+1; a naive last-two-labels match for now) are spaced by at
  least 1 second, or by the domain's `robots.txt` `Crawl-delay` if larger.
  Fetches to different domains are not delayed relative to each other. This
  makes `extract_content` over a batch **noticeably slower** — that is the
  intended trade-off.
- **Retry with backoff** — a fetch that times out, hits a connection error,
  or returns HTTP 429 / 5xx is retried up to 3 times with exponential
  backoff (`min(30s, 1s * 2**attempt)` plus jitter), honouring a
  `Retry-After` header (seconds or HTTP-date form, capped at 30s) when the
  server sends one. If every attempt fails the URL ends
  `failure_kind = retries_exhausted` with the underlying cause in the
  reason. DNS failures, HTTP 400/401/403/404/410, unsupported content types
  and oversized bodies are **not** retried.

All of this is in-process for the synchronous command; sharing the cache and
the rate-limit clock across workers is part of background batch processing
(issue #8).

### Failures

A URL that cannot be extracted ends `status = failed` with two fields: a
machine-readable `failure_kind` (`dns`, `connection`, `http_client`,
`blocked`, `blocked_by_robots`, `retries_exhausted`, `timeout`, `too_large`,
`unsupported_type`, `no_content`, `paywall`, `bot_wall`, `consent_wall`,
`unknown`) and a one-line `failure_reason`
with the specific detail (e.g. `HTTP 429 (rate limited)`). A bad URL never
stops the run: the command skips
it, moves to the next URL, and still exits 0. The batch detail page and the
Django admin show the kind and reason per URL, and the batch page shows a
by-kind breakdown (e.g. `3 failed: 2 blocked, 1 timeout`). On a re-run that
now succeeds, both fields are cleared.

Note on retries: a URL whose fetch fails *before* an extraction method is
chosen keeps `extraction_method = none`. The no-selector run
(`extract_content` with no `--url` / `--id` / `--batch`) selects exactly the
`none` rows, so it **re-attempts every previously-failed URL** on each run.
This is intentional - it is the way to retry transient failures - but it
means a plain re-run is not idempotent for rows that keep failing.

### Extension submissions (`extraction_method = extension`)

Pages submitted from the browser extension skip the fetch stack above: the
content script (`extension/content_extract.js`) extracts title/text in the
live, already-rendered DOM with vendored Readability.js
(`extension/lib/Readability.js`), falling back to
`document.title` / `document.body.innerText` when Readability yields
nothing. Before collecting, it runs a bounded lazy-image reveal pass (at
most 10 viewport hops × 150 ms ≈ 1.5 s, scroll position restored,
fail-soft) so IntersectionObserver-driven lazy images populate before
candidate collection. Alongside the text it sends up to 25 candidate image
URLs scoped to the Readability article (`images`, DOM order); the server
stores them on `SubmittedURL.extension_image_urls` and they become the
first-choice candidates in *Card images* below. The submit endpoint rejects
text under the 200-non-whitespace-char threshold with `text too short`.

## Generate cards

In the normal extension flow card generation runs automatically after
submit (`process_extension_submission`); the commands below are
contributor/diagnostic tools for re-running it directly.

Turn the extracted text of a URL into Anki-style flashcards with the LLM
client (see [LLM client](#llm-client)):

```
uv run python manage.py generate_cards --url https://example.com/article
uv run python manage.py generate_cards --id 42
uv run python manage.py generate_cards --batch 1     # every extracted URL in a batch
uv run python manage.py generate_cards              # all extracted, status=ok, no cards yet
uv run python manage.py generate_cards --url ... --force   # delete this URL's cards and regenerate
```

One line is printed per URL: the number of cards created (by note type,
plus any rejected), or the skip / failure reason.

Each card is auto-classified **per card** (a single URL can yield a mix):

- **basic** - a term with a standalone definition (`front` = term/question,
  `back` = definition).
- **cloze** - a term used in a reusable sentence; `front` holds that
  sentence with Anki `{{c1::…}}` markers, `back` may be blank.

The card count follows the density of the content - there is no fixed "N
per URL". A per-URL upper bound (`MAX_CARDS_PER_URL` in
`submissions/generation.py`, alongside the content threshold and
prompt-size limits) is a safety cap only.

Cards are saved in one transaction (`bulk_create`) - a mid-run LLM failure
never leaves half-written cards. `generation.generate_for()` persists the
cards first, then delegates all post-generation work (local dedup,
live-deck Anki dedup, image attachment) to
`submissions/post_generation.py:run_post_generation` (each stage
best-effort with its own try/except, so one failing stage never aborts the
others). Every card is tagged with the source URL,
an ISO date, and a topic when one can be inferred (blank otherwise). A
card's `batch` is a copy of its `SubmittedURL`'s originating batch (may be
null); `submitted_url` is the authoritative link.

Cards rephrase the source: the system prompt instructs the model to restate
definitions in its own words and never reuse the page's sentences verbatim
(page grounding still holds — the model may use page context plus its own
knowledge, it just must not parrot sentences). After generation each kept
card is scanned for contiguous verbatim runs of more than
`MAX_VERBATIM_WORDS` (12) words copied from the page's extracted text
(case-/punctuation-insensitive word-window scan in
`submissions/generation.py`). A card that trips the check is **kept, not
regenerated or dropped** — the run only logs it: the per-URL output line
grows a `(N cards close to source wording)` suffix (e.g. `... created: 2
basic (1 cards close to source wording)`), so the reviewer can spot and
reject it, feeding the few-shot loop.

Programming analogies use a configurable language (default Python): the
prompt carries `When you use a programming analogy, use <language>.`, so a
default run produces Python analogies ("a class and an instance of it"),
not Java.

Settings (`config/settings.py`, each also an env var of the same name):

| Setting | Default | Meaning |
| --- | --- | --- |
| `CARD_ANALOGY_LANGUAGE` | `python` | Language used for programming analogies in generated cards |

Change it with no code edit, e.g. `export CARD_ANALOGY_LANGUAGE=javascript`.

Behaviour on trouble:
- `extracted_text` under the threshold (200 non-whitespace chars) - URL
  skipped, reason `insufficient content for generation`, exit 0.
- rate-limit / transient LLM error - that URL is marked
  `generation_status = failed` with the reason, the run continues, exit 0.
- auth error / provider config missing - the command stops with a non-zero
  exit (it would fail for every URL).
- refusal / truncation / malformed response, or every card rejected by
  validation (`no valid cards produced`) - URL skipped/failed with the
  reason, zero cards, run continues.

`Card` rows are visible in the Django admin (filterable by note type and
batch, and inline on the `SubmittedURL` page).

## Deduplicate cards

Like the two sections above, the commands here are contributor/diagnostic
tools — in the normal extension flow dedup runs automatically as a
post-generation stage (`submissions/post_generation.py`, after the cards
are persisted). After generation, each new `Card` is embedded locally (no API calls) and
compared by cosine similarity against (a) cards already stored as `unique`
from previous runs and (b) the other new cards in the same run. A card at
or above `DEDUP_SIMILARITY_THRESHOLD` (in `submissions/dedup.py`, the single
place to tune it) to another card is marked `duplicate`, with `duplicate_of`
pointing at the card it matched, and is hidden from the default review grid
(`Card.objects.for_review()`). Duplicates are never deleted. Within one run
the lowest-pk card is kept `unique`. With nothing to compare against, every
card is `unique` and its embedding is recorded.

The post-generation pipeline (`DEFAULT_STAGES` in
`submissions/post_generation.py`: local semantic dedup, then live-deck Anki
dedup against the batch's stored deck, then image attachment) runs
automatically as the final step of `generate_cards`. Local dedup can be
skipped with `DEDUP_ENABLED=0` (still marks `dedup_ready` so the status
endpoint's `terminal` gating behaves as before); the live-deck Anki stage
compares against the notes currently in the batch's stored deck (never
`ANKI_DECK_NAME`) and degrades to local-only with a warning when no deck is
chosen or Anki is unreachable. A custom `stages` list replaces the default
pipeline entirely (injection seam, no edit to `generation.py`).

Local dedup is also a standalone command:

```
uv run python manage.py dedup_cards --batch 1     # every card in a batch
uv run python manage.py dedup_cards --id 42       # one card
uv run python manage.py dedup_cards --all         # every card
uv run python manage.py dedup_cards --all --force # ignore cached embeddings
uv run python manage.py dedup_cards --all --include-duplicates  # also re-check duplicates
```

It prints one line per card (`unique` / `duplicate of card N`) and reuses
each card's cached embedding unless `--force` is given. The embedding model
(`all-MiniLM-L6-v2`) is loaded once per run; if its weights are missing the
command exits non-zero and tells you to run the one-time download above
(post-generation dedup instead logs a warning and is skipped). `dedup_status`,
`duplicate_of` and `similarity_score` are shown and filterable in the Django
admin.

## Card images

As a post-generation stage (`image_attachment_stage` in
`submissions/post_generation.py`), each new `Card` gets **at most one**
image (`submissions/images.py`):

1. **Extension candidates first.** For extension submissions, the
   `extension_image_urls` stored at submit time (live-DOM article images —
   the only candidates on authenticated / JS-rendered pages) come first,
   in received order, followed by the server-refetched candidates below.
   Deduplicated across both lists, capped at `MAX_IMAGE_CANDIDATES` (25)
   total. Both sources pass the same chrome-marker / usability filter.
2. **Source page refetch.** The card's source page is re-fetched (reusing
   the extraction fetch stack — politeness, size cap, retries) and its
   `<img>` / `<source>` tags (`src`, `data-src`, `data-original`,
   `data-lazy-src`, first `srcset` URL) are scanned for candidates.
3. **Relevance ranking.** Candidates are reordered best-first, purely and
   deterministically (stable sort, ties keep DOM/merge order):
   hero `og:image` / `twitter:image` > term-match (card token in image
   `alt` or URL slug) > larger declared `width × height` area
   (missing = 0) > DOM order. Ranking reorders only — never admits or
   rejects.
4. **Draw Things fallback.** If no source image is usable, one image is
   requested from a local Draw Things over its Automatic1111-compatible
   `/sdapi/v1/txt2img` endpoint, with a prompt built from the card's own
   term / topic.
5. **No image.** If neither yields anything the card is still produced,
   with `image_source = none`. Image work never raises, never aborts the
   batch, and is bounded by a per-image timeout (`IMAGE_FETCH_TIMEOUT`)
   and a Draw Things timeout (`DRAW_THINGS_TIMEOUT`).

**"Usable image" rules** (named constants at the top of
`submissions/images.py`, applied consistently): minimum 200×200 px,
minimum 1 KB encoded, raster type only (`jpeg`/`png`/`webp`/`gif`, no
SVG), under an 8 MB cap; URLs containing chrome/tracking markers
(`favicon`, `sprite`, `spacer`, `pixel`, `1x1`, `logo`, `icon`, `avatar`,
`tracking`, `beacon`, ad paths, …) and `data:` URIs are excluded before
download. If a chosen candidate fails to fetch (404, timeout, non-image,
blocked) the next candidate is tried, then Draw Things.

**Placement.** The image side follows the note type and is exposed as
`Card.image_placement`: `cloze` → `"question"` (shown on the question
side), `basic` → `"answer"` (shown on the answer side). The review grid
reads this rule.

Images are stored under `MEDIA_ROOT` (`media/`, git-ignored) in `cards/`,
and the card records the file plus `image_source` (`source_page` /
`draw_things` / `none`), both visible in the Django admin.

**Settings** (each falls back to an environment variable of the same name):

| Setting | Default | Meaning |
| --- | --- | --- |
| `DRAW_THINGS_URL` | `http://127.0.0.1:7860` | Base URL of the local Draw Things HTTP API |
| `DRAW_THINGS_ENABLED` | `1` | Set to `0` to skip fallback generation entirely |
| `MEDIA_ROOT` | `media/` | Where card images are written |
| `MEDIA_URL` | `media/` | URL prefix for stored images |

## Review feedback (durable) + few-shot injection

Every time a card is accepted or rejected in the review grid, a `Feedback`
row is written (`submissions/models.py`, via `submissions/feedback.py`). It is a **snapshot**: the card
front/back (or cloze text), note type, source URL, the decision, the
optional rejection reason, and a timestamp are copied in as plain values.
`Feedback` has no foreign key to `Card`, `SubmittedURL` or `Batch`, so
deleting a batch and its cards never removes the feedback history. The rows
are read-only in the Django admin (`Feedback`, filterable by decision and
note type); a raw query works too, e.g.
`sqlite3 db.sqlite3 "select decision, reason, front from submissions_feedback"`.

When a new batch generates cards, `submissions/feedback.py`
(`build_fewshot_section`, called from `submissions/generation.py`) prepends a
few-shot section to the generation system prompt, built from stored
`Feedback`. Default strategy is relevance-ranked and token-budgeted
(`FEWSHOT_SELECTION_MODE=relevance`); `recency` reproduces the original
most-recent-N-per-category pick:

- **relevance (default):** feedback examples are ranked by token-overlap
  similarity to the current page text (offline default; a real embedding
  backend can be plugged via `FEWSHOT_EMBED_FN`, a dotted path to
  `fn(list[str]) -> list[list[float] | None]`) and added in rank order
  until the next one would exceed `FEWSHOT_TOKEN_BUDGET` (default 2000
  tokens, `~4` chars/token via `FEWSHOT_CHARS_PER_TOKEN`). The budget is
  split so accepted examples get `FEWSHOT_ACCEPTED_SHARE` (default 0.5)
  and rejected ones get the rest — one side cannot crowd out the other.
  Examples shorter than `FEWSHOT_MIN_FEEDBACK_CHARS` (default 20
  non-whitespace chars) are skipped. Any backend/counter failure degrades
  to the recency pick, never a crash.
- **recency fallback:** the most recent `FEWSHOT_EXAMPLES_PER_CATEGORY`
  accepted rows and, separately, the most recent
  `FEWSHOT_EXAMPLES_PER_CATEGORY` rejected rows (named constant in
  `submissions/feedback.py`, currently 3) — capped at
  `2 x FEWSHOT_EXAMPLES_PER_CATEGORY` examples however much feedback
  accumulates; ordered oldest-first so the prompt string is deterministic.
- Each example shows the card; rejected examples also show the reason, or
  `(no reason given)` when the rejection had none (reason-less rejections
  are still used).
- Zero feedback -> no section at all. Only-accepted or only-rejected
  feedback -> only that category's list is included; the other is omitted.

Settings (`config/settings.py`, each also an env var of the same name):

| Setting | Default | Meaning |
| --- | --- | --- |
| `FEWSHOT_TOKEN_BUDGET` | `2000` | max tokens for the whole few-shot section (both categories) |
| `FEWSHOT_ACCEPTED_SHARE` | `0.5` | fraction of the budget reserved for accepted examples |
| `FEWSHOT_SELECTION_MODE` | `relevance` | `relevance` or `recency` |
| `FEWSHOT_MIN_FEEDBACK_CHARS` | `20` | shorter feedback examples are skipped |
| `FEWSHOT_CHARS_PER_TOKEN` | `4` | chars-per-token approximation for the budget check |
| `FEWSHOT_EMBED_FN` | `` | optional dotted path to an embedding backend |

## Review grid

`batch/<id>/review/` shows one batch's cards (`Card.objects.for_review()` —
semantic duplicates hidden, never deleted). Per card, inline and without a
page reload:

- **Accept / Reject** (+ optional reason on reject; accepting clears it).
  Every decision writes a durable `Feedback` snapshot above (`was_edited`
  records whether the card had been edited). Rejections — with or without
  a reason — feed the few-shot loop (see
  [Review feedback (durable) + few-shot injection](#review-feedback-durable--few-shot-injection)).
- **Edit text / revert.** Edit front/back; the first save snapshots
  `original_front` / `original_back` so revert always restores the
  generator output (`is_edited`, `edited_at` track state).
- **Image:** pick from source candidates, regenerate via Draw Things,
  remove, or revert to the automatic pick (`image_manually_set` /
  `original_image` / `original_image_source` track state). Placement
  follows `Card.image_placement` (cloze → question, basic → answer).
- **Finish** → pick the Anki deck (dropdown of live decks + free-text new
  name; typed name wins, stored as `batch.deck_name`), confirm when cards
  are still undecided, then push accepted cards to Anki below. The deck
  choice is required — Finish with no deck re-renders the page with an
  error and enqueues nothing.

The review page shows the latest push outcome as a banner whenever Finish
has been clicked (`batch.push_status` / `batch.push_outcome_message`):
`pending` (push in flight), `done` (counts pushed / already-synced /
failed into the snapshot deck name), `unreachable` (Anki/AnkiConnect was
down, nothing pushed), or `failed` (unexpected task error). Only the
latest attempt is kept — each write overwrites absolute counts, so
overlapping Finish clicks end with whichever task completes last.

## Push to Anki

Accepted cards from the review grid are pushed to the batch's stored deck
(`batch.deck_name`, chosen on the review page) over the
[AnkiConnect](https://foosoft.net/projects/anki-connect/) HTTP API
(standard library only, no extra dependency). There is no single global
deck — each batch pushes to its own stored deck.

**Anki must be running with the AnkiConnect add-on installed** and listening
at `ANKI_CONNECT_URL` (default `http://127.0.0.1:8765`).

```
uv run python manage.py push_to_anki
```

- Finish on the review page (`card_review_finish`) stores the deck choice,
  marks the batch push `pending` synchronously, and enqueues the Huey task
  `push_accepted_cards_task(batch.pk)` — the push runs in the background
  and the page redirects back to show the outcome banner above.
- `push_to_anki` (manual/CLI path) pushes every deck-assigned batch grouped
  per deck via the same `push_accepted_cards()` orchestration.
- Sends only cards with `review_status == accepted` that have not been synced
  yet. Other states are ignored.
- Batches with no stored deck (NULL/empty) are skipped and never fall back
  to `ANKI_DECK_NAME`.
- Creates the deck (AnkiConnect `createDeck`) if it does not exist.
- `basic` cards → the "Basic" note type, `cloze` cards → "Cloze".
- Every note is tagged with its source URL, ISO date added, and topic.
- On success a card records `anki_note_id` + `synced_at`, so re-running adds
  zero new notes for already-synced cards. The batch records the terminal
  outcome (`push_status`, `push_deck_name`, pushed/skipped/failed counts,
  `push_finished_at`).
- If Anki is unreachable the batch records `unreachable` (review page shows
  the retry hint; CLI aborts naming the problem and the configured URL)
  and nothing is marked synced. A per-note AnkiConnect
  error (bad note type, etc.) fails just that card; an Anki duplicate is
  reported as skipped-duplicate. The command prints counts of
  added / skipped / failed with reasons.

Settings (`config/settings.py`, each also an env var of the same name):

| Setting | Default | Meaning |
| --- | --- | --- |
| `ANKI_CONNECT_URL` | `http://127.0.0.1:8765` | AnkiConnect base URL |
| `ANKI_CONNECT_TIMEOUT` | `10` | seconds before Anki is treated as unreachable |
| `ANKI_DECK_NAME` | `Flashcard Generator` | legacy default; never used as a push target (pushes always use the batch's stored `deck_name`) |

The AnkiConnect transport lives in `submissions/anki.py`
(`AnkiConnectClient`), behind which `push_accepted_cards()` /
`push_batch_accepted_cards()` / `push_all_deck_batches()` do the
orchestration and `submissions/tasks.py:push_accepted_cards_task` is the
background entrypoint; all are fakeable in tests without a live Anki.

## LLM client

`submissions/llm.py` is a thin, provider-agnostic client for text
generation. Call `submissions.llm.generate(system=..., prompt=...,
response_format=None, max_tokens=None)` and get back an `LLMResult` with
`.text` (and `.parsed`, a validated object, when you pass a JSON Schema as
`response_format`). Errors surface as typed exceptions from that module
(`LLMConfigError`, `LLMAuthError`, `LLMRateLimitError`, `LLMTransientError`,
`LLMBadResponseError`) so callers never import a provider SDK. Transient
failures (429, 5xx, connection, timeout) are retried with backoff; auth and
bad-request errors are not.

Which provider and model are used is configuration, read from Django
settings (each falls back to an environment variable of the same name).
Backend-supported providers (`PROVIDER_CATALOG` in `submissions/llm.py` is
the single source of truth — update that table when adding a provider;
`SUPPORTED_PROVIDERS`, `EXTENSION_LLM_PROVIDER_ORDER`,
`EXTENSION_LLM_CURATED_MODELS`, and `EXTENSION_LLM_REGISTRY_KEYS` are all
derived from it, and `_PROVIDER_SPECS` drives both `get_provider()`
construction and llm-config key resolution):

| Provider (`LLM_PROVIDER`) | Curated popup models | Key env var | Notes |
| --- | --- | --- | --- |
| `anthropic` (default) | `claude-sonnet-4-6`, `claude-haiku-4-5` | `ANTHROPIC_API_KEY` | Default provider |
| `openai` | `gpt-4o`, `gpt-4o-mini` | `OPENAI_API_KEY` via `LLM_OPENAI_API_KEY_ENV_VAR` | Display name for registry key `openai-compatible`; base URL via `LLM_OPENAI_BASE_URL` |
| `grok` (xAI) | `grok-4`, `grok-3-mini` | `XAI_API_KEY` | xAI's OpenAI-compatible endpoint |
| `opencode-zen` | `claude-sonnet-4-5`, `gpt-5.1`, `grok-code` | `OPENCODE_ZEN_API_KEY` | Per-provider overrides follow the generic `LLM_OPENCODE_ZEN_*` pattern (see `config/settings.py` + `submissions/llm.py`) |
| `openai-compatible` (backend-only) | — | `OPENAI_API_KEY` via `LLM_OPENAI_API_KEY_ENV_VAR` | Generic OpenAI-compatible endpoint (Ollama, vLLM, gateways) |
| `gemini` (backend-only) | — | `GOOGLE_API_KEY` via `LLM_GEMINI_API_KEY_ENV_VAR` | Google's Generative Language API; no default key-var name is hardcoded — pair with `LLM_GEMINI_API_KEY_ENV_VAR=GOOGLE_API_KEY` |
| `openrouter` (backend-only) | — | `OPENROUTER_API_KEY` | Origin-prefixed model ids (e.g. `anthropic/claude-3.5-sonnet`, `openai/gpt-4o`) |

The extension popup (`GET /api/extension/llm-config/`) exposes only the
`extension_visible` catalog entries in catalog order (currently
`anthropic` / `openai` / `grok` / `opencode-zen`), each with its curated
models and a `key_configured` presence boolean (never the key itself);
`openai-compatible`, `openrouter` and `gemini` are backend-only. The popup's
per-generation `provider` / `model` selection (`chosenLlm()` /
`submitContent()` in `extension/popup.js`) is stored as a per-submission
override (`llm_provider_override` / `llm_model_override`) and never mutates
`.env`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `anthropic` | Provider registry key (see table above) |
| `LLM_MODEL` | `claude-sonnet-5` | Model id passed to the provider |
| `LLM_API_KEY_ENV_VAR` | `ANTHROPIC_API_KEY` | Name of the env var holding the API key |
| `LLM_MAX_TOKENS` | `4096` | Default output-token ceiling when a caller omits `max_tokens` |
| `LLM_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible chat-completions endpoint (only used by `openai-compatible`) |
| `LLM_OPENAI_MODEL` | `` (falls back to `LLM_MODEL`) | Per-provider model override for `openai-compatible` |
| `LLM_OPENAI_API_KEY_ENV_VAR` | `` (falls back to `LLM_API_KEY_ENV_VAR`) | Per-provider key-env-var override for `openai-compatible` |

The API key itself is read from the environment (`ANTHROPIC_API_KEY` by
default) at call time, never stored in settings and never logged or placed
in an exception message. With no key set, the first call (or
`submissions.llm.check()`) raises `LLMAuthError` naming the variable to set.

**Point the client at a different model** with no code change:

```
export LLM_MODEL=claude-opus-5        # or any current Anthropic model id
uv run python manage.py shell
```

Changing provider is the same (`export LLM_PROVIDER=...`); an unknown value
raises `LLMConfigError` listing the supported providers. Timeout and retry
counts are named constants at the top of `submissions/llm.py`.

**Use an OpenAI-compatible endpoint** (OpenAI, Ollama, vLLM, any gateway
speaking chat-completions) with no code change — callers are untouched, the
provider switch routes every call through the new adapter:

```
export LLM_PROVIDER=openai-compatible
export LLM_OPENAI_BASE_URL=https://api.openai.com/v1   # or http://127.0.0.1:11434/v1, ...
export LLM_API_KEY_ENV_VAR=OPENAI_API_KEY               # or LLM_OPENAI_API_KEY_ENV_VAR=...
export LLM_MODEL=gpt-4o-mini                            # or LLM_OPENAI_MODEL=...
```

`LLM_OPENAI_MODEL` / `LLM_OPENAI_API_KEY_ENV_VAR`, when non-empty, override
the generic `LLM_MODEL` / `LLM_API_KEY_ENV_VAR` for the OpenAI-compatible
provider only. The adapter sends `system` + `prompt` as `system` / `user`
messages and maps `response_format` (including the card-list schema) to
`response_format: {type: json_schema, ...}`; auth, rate-limit, 5xx, timeout
and malformed-output errors surface as the same `LLM*Error` types, with the
same retry behaviour, and every call records its `LLMCall` usage row.

## LLM usage (cost / latency observability)

Every call made through `submissions.llm.generate` records one `LLMCall`
row (`submissions/models.py`): timestamp, model, prompt/completion tokens
(from the provider response usage payload, not estimates), wall-clock
latency in ms, estimated USD cost (per-model per-token prices in
`submissions/llm.py`: `LLM_PRICE_PER_MTOK`, with `DEFAULT_*` fallback
rates), attributable batch / URL, and status (`ok`, or `failed` with the
`LLMError` subclass name in `error_class`). A run that hits an LLM error
still records its failed row. Generation (`submissions/generation.py`)
attributes its calls via `submissions.llm.call_context`; unattributed calls
simply record null batch / URL.

Inspect the rows in the Django admin (`LLMCall`, read-only) or with the
management command:

```
uv run python manage.py llm_usage                  # latest 50 calls + totals
uv run python manage.py llm_usage --batch 1        # one batch
uv run python manage.py llm_usage --url https://example.com/article
uv run python manage.py llm_usage --status failed  # failures only
uv run python manage.py llm_usage --limit 10
```

## Current Limitations

- Per-batch Anki deck (chosen on the review page, stored as
  `batch.deck_name`) — `ANKI_DECK_NAME` is never used as a push target.
- One page at a time — each extension submission handles the page being viewed.
- macOS/Apple Silicon-gated setup and local image generation.
- Curated popup model lists live in `submissions/llm.py` (`PROVIDER_CATALOG`;
  `EXTENSION_LLM_CURATED_MODELS` is derived from it, not hand-maintained
  separately).

## Architecture

| Module | Responsibility |
| --- | --- |
| `submissions/generation.py` | Turns extracted text into basic/cloze cards via the LLM client, applies safety caps and the verbatim-copy check, prepends few-shot examples |
| `submissions/post_generation.py` | Post-generation pipeline after cards are persisted (local semantic dedup, live-deck Anki dedup, image attachment; `DEDUP_ENABLED` / `dedup_ready` bookkeeping) |
| `submissions/feedback.py` | Durable `Feedback` snapshots plus relevance-ranked, token-budgeted few-shot selection/rendering |
| `submissions/dedup.py` | Embeds cards locally and marks near-duplicates against prior `unique` cards and same-run siblings |
| `submissions/images.py` | Picks or generates each card's single image (extension/source-page candidates, relevance ranking, Draw Things fallback) |
| `submissions/llm.py` | Provider-agnostic LLM client (`generate()`), `PROVIDER_CATALOG` source of truth, typed errors, retry/backoff, per-call usage recording |
| `submissions/anki.py` | AnkiConnect HTTP transport (`AnkiConnectClient`) and the per-batch-deck push orchestration (`push_batch_accepted_cards` / `push_all_deck_batches`) |
| `submissions/tasks.py` | Background Huey entrypoint (`push_accepted_cards_task`) that records the terminal `Batch.push_status` outcome |
| `submissions/extension_api.py` | Browser-extension submit/status/llm-config/decks API endpoints, CORS allowlisting by `EXTENSION_ID` |
| `native_host/host.py` | Brave/Chrome native messaging host process; readiness probe and backend auto-spawn for the extension |
| `config/settings.py` | Single source of Django settings; every setting also reads from an env var of the same name |

## Development

```
uv sync                              # install dependencies
uv run pytest                        # run the whole test suite
uv run pytest tests/test_home.py     # run one test file
uv run python manage.py dev          # dev entrypoint: runserver + Huey consumer together
```

Huey tests run in immediate mode (no consumer needed). See
[Background processing (Huey)](#background-processing-huey) for
`run_huey`, the manual fallback, and `HUEY_IMMEDIATE=1`.

## License

[MIT](./LICENSE) © 2026 Evan Simpson
