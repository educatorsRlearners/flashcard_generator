Commands

- `uv sync` - install dependencies
- `uv run pytest` - the whole suite
- `uv run pytest tests/test_home.py` - one test file
- `uv run python manage.py dev` - dev entrypoint: starts `runserver` +
  Huey consumer together with prefixed logs (`[web]` / `[worker]`);
  Ctrl-C stops both, a crashed worker auto-restarts (crash-loop exits 1)
- `uv run python manage.py run_huey` - background task consumer alone
  (manual fallback; normally started automatically by `dev`); tests use
  Huey immediate mode
- `uv run python manage.py push_to_anki` - push accepted cards to Anki via
  AnkiConnect (idempotent). Requires Anki running with the AnkiConnect
  add-on. Settings: `ANKI_DECK_NAME` (default "Flashcard Generator"),
  `ANKI_CONNECT_URL` (default http://127.0.0.1:8765)
- `uv run python manage.py check_llm` - smoke-test the configured LLM
  provider with a trivial prompt; exits 0/1. No prerequisites beyond
  normal `.env` LLM config
- `/llm-usage/` - LLM usage dashboard (cost/latency/volume/failure
  aggregates over a recent time window: `?window=24h`, `7d` (default), or
  `30d`); same `LLMCall` data as the `llm_usage` management command above,
  shown aggregated over time instead of one row per call

Rules

- Dependencies are added in `pyproject.toml`. Do not add one without
  asking


Documents

- `_docs/process.md` - how work is organized
<!-- - Before writing tests, read `_docs/testing-guidelines.md` -->
<!-- - For anything touching the UI, read `_docs/design-system.md` -->