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

## Run

```
uv run python manage.py migrate
uv run python manage.py runserver
```

Then open http://127.0.0.1:8000/ , paste one or more URLs (one per line)
into the textarea, and submit. Saved URLs are listed back on the page and
stored in `db.sqlite3`.

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
