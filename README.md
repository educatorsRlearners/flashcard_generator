# flashcard_generator

Walking skeleton: a Django app on SQLite with one page that accepts URLs,
persists them, and lists them back.

## Setup

```
uv sync
```

Content extraction uses `trafilatura` for the static fast path and Playwright
(headless Chromium) for the JavaScript fallback. Chromium is a one-time
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
```

The static path (`trafilatura`) is tried first; the browser fallback runs
automatically when the static text has fewer than 200 non-whitespace
characters. Results land in the `extracted_text` / `extracted_title` /
`extraction_method` / `extracted_at` fields and are visible in the admin.

### Failures

A URL that cannot be extracted ends `status = failed` with two fields: a
machine-readable `failure_kind` (`dns`, `connection`, `http_client`,
`blocked`, `timeout`, `too_large`, `unsupported_type`, `no_content`,
`unknown`) and a one-line `failure_reason` with the specific detail (e.g.
`HTTP 429 (rate limited)`). A bad URL never stops the run: the command skips
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

## Tests

```
uv run pytest
```
