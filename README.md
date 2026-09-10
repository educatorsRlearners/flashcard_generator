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

## Run

```
uv run python manage.py migrate
uv run python manage.py runserver
```

Then open http://127.0.0.1:8000/ , paste one or more URLs (one per line)
into the textarea, and submit. Submitting returns immediately and sends you
to the batch page, which polls a JSON status endpoint and updates its
progress indicator until the batch is done.

## Background processing (Huey)

Submitting a batch enqueues one background task per URL that runs the
extraction path (below). The tasks are processed by a Huey consumer backed
by a local SQLite file (`huey.sqlite3`) — no Redis or extra service. Start
the consumer in a second terminal:

```
uv run python manage.py run_huey
```

`runserver` does **not** start it (that is issue #20). If the consumer is
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
image-only) fails with `failure_kind = no_content`; OCR of scanned PDFs and
images is a separate follow-up (issue #19) and is not done here. Other
non-HTML types (`image/png`, `application/zip`, `text/csv`, legacy `.doc`,
…) still fail with `unsupported_type`.

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
| `LLM_PROVIDER` | `anthropic` | Provider registry key (only `anthropic` today; #27 adds more) |
| `LLM_MODEL` | `claude-sonnet-5` | Model id passed to the provider |
| `LLM_API_KEY_ENV_VAR` | `ANTHROPIC_API_KEY` | Name of the env var holding the API key |
| `LLM_MAX_TOKENS` | `4096` | Default output-token ceiling when a caller omits `max_tokens` |

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

## Tests

```
uv run pytest
```
