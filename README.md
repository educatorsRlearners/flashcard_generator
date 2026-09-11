# flashcard_generator

Walking skeleton: a Django app on SQLite with one page that accepts URLs,
persists them, and lists them back.

## Setup

```
uv sync
```

Content extraction uses `trafilatura` for the static (HTML) fast path,
Playwright (headless Chromium) for the JavaScript fallback, and
`pdfminer.six` for PDF text extraction. `.docx` files are read with the
Python standard library (no extra dependency). Chromium is a one-time
several-hundred-MB download that is **not** installed by `uv sync`:

```
uv run playwright install chromium
```

CI images will not have it unless this step runs.

Semantic dedup (see *Deduplicate cards* below) uses `sentence-transformers`
for local embeddings. It is installed by `uv sync`, but it pulls in `torch`
and the model weights are a one-time download (tens of MB) that `uv sync`
does **not** fetch:

```
uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

CI / test runs do not need the weights: the tests stub the encoder.

Per-card images (see *Card images* below) use `pillow` (image inspection /
storage) and `httpx` (image downloads + the Draw Things HTTP call). Both
are installed by `uv sync`. Generating fallback images additionally
assumes a running **Draw Things** with its HTTP API server enabled
(Draw Things → Settings → API Server); installing Draw Things and picking
a model/checkpoint is out of scope. If Draw Things is not running the card
is simply produced with no image — nothing aborts. Tests stub every
network call and need neither Draw Things nor internet.

## Run

```
uv run python manage.py migrate
uv run python manage.py dev
```

Then open http://127.0.0.1:8000/ , paste one or more URLs (one per line)
into the textarea, and submit. Submitting returns immediately and sends you
to the batch page, which holds one SSE (`EventSource`) connection to
`batch/<id>/events/` and updates its progress indicator live — per-URL
status plus the "cards generated so far" count — with no polling and no
page reload. When the batch finishes the server sends a `complete` event
and the client closes the connection; an already-completed batch never
opens one (final state is server-rendered). EventSource reconnects
automatically on a dropped connection and every (re)connect starts with a
`snapshot` event carrying the full current state, so mid-run loads,
reconnects, and multiple tabs all show live progress. Only if
EventSource is unavailable, or the stream errors repeatedly, does the
page fall back to polling the JSON status endpoint (`batch/<id>/status/`)
every 2 s.

`dev` is a development-only supervisor (stdlib only): it starts
`runserver` and the Huey consumer below together in one terminal, prefixes
their output (`[web]` / `[worker]`), restarts a crashed consumer
automatically (a crash-looping consumer stops everything with exit 1), and
stops both on Ctrl-C with no orphans. No extra process is needed for the
SSE stream above: it is served by `runserver` itself, which `dev` starts
in its default threaded mode. Do NOT run `runserver --nothreading` (or
`dev` against one): the single-threaded server would serialize the
long-lived `/events/` connection against normal requests and hang the
page. Production/WSGI and `uv run pytest`
never spawn a consumer (tests run Huey tasks eagerly in-process).

## Background processing (Huey)

Submitting a batch enqueues one background task per URL that runs the
extraction path (below). The tasks are processed by a Huey consumer backed
by a local SQLite file (`huey.sqlite3`) — no Redis or extra service. The
`dev` command above starts the consumer automatically, so submitting a
batch moves its URLs out of `pending` with no further step. Manual
fallback in a second terminal (if you run `runserver` on its own):

```
uv run python manage.py run_huey
```

`runserver` on its own does **not** start it (use `dev` instead).
If the consumer is
not running, the batch page shows the URLs stuck in `pending` with a notice
and this command. Pending tasks are persisted, so restarting the consumer
after a crash resumes them. To run tasks inline without a consumer (e.g. a
one-off script), set `HUEY_IMMEDIATE=1`.

## Extract content

Fetch and extract the main body text for submitted URLs:

```
uv run python manage.py extract_content --url https://example.com/article
uv run python manage.py extract_content --batch 1
uv run python manage.py extract_content            # all rows not yet extracted
uv run python manage.py extract_content --batch 1 --force   # re-extract
uv run python manage.py extract_content --url ... --ignore-robots  # skip robots.txt (local only)
```

The static path (`trafilatura`) is tried first; the browser fallback runs
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
   `brew install tesseract` (macOS) or
   `sudo apt install tesseract-ocr` (Debian/Ubuntu).
2. **Python binding (inside `uv`):** `uv add pytesseract`
   (pillow, needed to decode images, is already a dependency).

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
`unsupported_type`, `no_content`, `unknown`) and a one-line `failure_reason`
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

## Generate cards

Turn the extracted text of a URL into Anki-style flashcards with the #5 LLM
client:

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
never leaves half-written cards. Every card is tagged with the source URL,
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

After generation, each new `Card` is embedded locally (no API calls) and
compared by cosine similarity against (a) cards already stored as `unique`
from previous runs and (b) the other new cards in the same run. A card at
or above `DEDUP_SIMILARITY_THRESHOLD` (in `submissions/dedup.py`, the single
place to tune it) to another card is marked `duplicate`, with `duplicate_of`
pointing at the card it matched, and is hidden from the default review grid
(`Card.objects.for_review()`). Duplicates are never deleted. Within one run
the lowest-pk card is kept `unique`. With nothing to compare against, every
card is `unique` and its embedding is recorded.

This runs automatically as the final step of `generate_cards`. It is also a
standalone command:

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

As the final step of `generate_cards`, each new `Card` gets **at most one**
image (`submissions/images.py`):

1. **Source page first.** The card's source page is re-fetched (reusing the
   extraction fetch stack — politeness, size cap, retries) and its `<img>`
   tags are scanned for a *usable* image.
2. **Draw Things fallback.** If no source image is usable, one image is
   requested from a local Draw Things over its Automatic1111-compatible
   `/sdapi/v1/txt2img` endpoint, with a prompt built from the card's own
   term / topic.
3. **No image.** If neither yields anything the card is still produced,
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
side), `basic` → `"answer"` (shown on the answer side). The #9 review grid
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
row is written (`submissions/models.py`). It is a **snapshot**: the card
front/back (or cloze text), note type, source URL, the decision, the
optional rejection reason, and a timestamp are copied in as plain values.
`Feedback` has no foreign key to `Card`, `SubmittedURL` or `Batch`, so
deleting a batch and its cards never removes the feedback history. The rows
are read-only in the Django admin (`Feedback`, filterable by decision and
note type); a raw query works too, e.g.
`sqlite3 db.sqlite3 "select decision, reason, front from submissions_feedback"`.

When a new batch generates cards, `submissions/generation.py` prepends a
few-shot section to the generation system prompt, built from stored
`Feedback`:

- The most recent `FEWSHOT_EXAMPLES_PER_CATEGORY` accepted rows and,
  separately, the most recent `FEWSHOT_EXAMPLES_PER_CATEGORY` rejected rows
  (named constant in `submissions/generation.py`, currently 3) - so the
  section is capped at `2 x FEWSHOT_EXAMPLES_PER_CATEGORY` examples however
  much feedback accumulates.
- Selection is "most recent N per category" by timestamp; within the prompt
  the examples are ordered oldest-first, so the assembled prompt string is
  deterministic for the same stored data.
- Each example shows the card; rejected examples also show the reason, or
  `(no reason given)` when the rejection had none (reason-less rejections
  are still used).
- Zero feedback -> no section at all. Only-accepted or only-rejected
  feedback -> only that category's list is included; the other is omitted.

## Push to Anki

Accepted cards from the review grid are pushed into a single Anki deck over
the [AnkiConnect](https://foosoft.net/projects/anki-connect/) HTTP API
(standard library only, no extra dependency).

**Anki must be running with the AnkiConnect add-on installed** and listening
at `ANKI_CONNECT_URL` (default `http://127.0.0.1:8765`).

```
uv run python manage.py push_to_anki
```

- Sends only cards with `review_status == accepted` that have not been synced
  yet. Other states are ignored.
- Creates the deck (AnkiConnect `createDeck`) if it does not exist.
- `basic` cards → the "Basic" note type, `cloze` cards → "Cloze".
- Every note is tagged with its source URL, ISO date added, and topic.
- On success a card records `anki_note_id` + `synced_at`, so re-running adds
  zero new notes for already-synced cards.
- If Anki is unreachable the command aborts with a message naming the problem
  and the configured URL; nothing is marked synced. A per-note AnkiConnect
  error (bad note type, etc.) fails just that card; an Anki duplicate is
  reported as skipped-duplicate. The command prints counts of
  added / skipped / failed with reasons.

Settings (`config/settings.py`, each also an env var of the same name):

| Setting | Default | Meaning |
| --- | --- | --- |
| `ANKI_DECK_NAME` | `Flashcard Generator` | the single deck cards are pushed into |
| `ANKI_CONNECT_URL` | `http://127.0.0.1:8765` | AnkiConnect base URL |
| `ANKI_CONNECT_TIMEOUT` | `10` | seconds before Anki is treated as unreachable |

The AnkiConnect transport lives in `submissions/anki.py`
(`AnkiConnectClient`), behind which `push_accepted_cards()` does the
orchestration; both are fakeable in tests without a live Anki.

## Configuration (.env)

Secrets and local overrides are read from a git-ignored `.env` file in the
project root (via `python-dotenv`, loaded in `config/settings.py`). Copy the
template and fill in your key:

```bash
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

Real environment variables take precedence over `.env`.

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
settings (each falls back to an environment variable of the same name):

| Setting | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `anthropic` | Provider registry key: `anthropic`, `openai-compatible` (alias `openai`) |
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

## Browser extension setup (native messaging host)

The extension (`extension/`) reaches this backend through a native
messaging host (`native_host/host.py`, #37), which Chrome/Brave discover
via a manifest file registered on your machine. One-time setup, in this
order:

1. Generate a signing keypair with `openssl` (manual, one-time - not
   scripted by any command here) and put its base64 public key into
   `extension/manifest.json`'s `"key"` field. Pinning a key keeps the
   extension's ID stable across reloads.
2. Load the extension unpacked: `chrome://extensions` (or
   `brave://extensions`) → enable Developer mode → "Load unpacked" →
   select the `extension/` directory. Note the extension ID Chrome/Brave
   assigns it - because the key is pinned in step 1, this ID stays stable
   across future reloads.
3. Run the installer with that ID:
   ```
   uv run python manage.py install_native_host --extension-id <id>
   ```
   This writes `native_host/run_host.sh` (a wrapper script with an
   absolute interpreter path baked in) and registers the native-messaging
   manifest with whichever of Chrome/Brave are installed, and mints the
   extension auth token (#33) if one doesn't exist yet. Safe to re-run any
   time the extension's ID changes (e.g. after an unpinned reload) -
   re-running overwrites the wrapper and manifest(s) in place.
4. If Chrome/Brave was already open when the manifest was written, reload
   the extension once more. Native messaging host manifests are read fresh
   per `connectNative` call, but a stale `chrome://extensions` page may not
   reflect a just-loaded ID - if the popup reports it can't connect,
   reloading the extension is the fix.

`install_native_host --extension-id <id>` can be run before step 1's key
exists - the ID is always supplied explicitly on the command line, never
auto-discovered from the extension's files, so the two have no ordering
dependency beyond needing *an* ID (pinned or not) in hand first.

macOS only (this repo's development and documented setup are macOS-only);
Linux/Windows native-messaging support is tracked separately in #43.

For the full manual verification checklist (cold start, error cases,
review-tab regression), see `_docs/extension_manual_checklist.md`.

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
   Chrome, so it reads Chrome's environment, not your terminal's — launch
   Chrome from a terminal with the var set (e.g.
   `BACKEND_URL=http://127.0.0.1:9000 open -a "Google Chrome"`), or set it
   persistently for GUI apps.
3. Hand-edit `extension/manifest.json`'s `host_permissions` to match the
   new origin exactly (MV3 permissions are static at load time, so this
   cannot be picked up at runtime):
   ```
   "host_permissions": ["http://127.0.0.1:9000/*"],
   ```
   then reload the extension at `chrome://extensions` (Developer mode →
   Reload). No code or permission-prompt flow is involved.
4. Re-run the manual checklist above; the popup follows the host's
   `base_url` with no other change.

Mismatch policy: the host's configured URL always wins. If it answers, the
extension is pointed at it (`already_running`), even if you also started a
backend by hand on a different port — that other backend is ignored, not
adopted. If the configured URL is down, the host spawns its own backend on
the matching port, even if another port answers. Keep both sides on the
same `BACKEND_URL` (or pass `dev --addrport` explicitly) to avoid running
two backends unknowingly.

## Tests

```
uv run pytest
```
