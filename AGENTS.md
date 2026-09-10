Commands

- `uv sync` - install dependencies
- `uv run pytest` - the whole suite
- `uv run pytest tests/test_home.py` - one test file
- `uv run python manage.py run_huey` - background task consumer for batch
  processing (run alongside `runserver`); tests use Huey immediate mode
- `uv run python manage.py push_to_anki` - push accepted cards to Anki via
  AnkiConnect (idempotent). Requires Anki running with the AnkiConnect
  add-on. Settings: `ANKI_DECK_NAME` (default "Flashcard Generator"),
  `ANKI_CONNECT_URL` (default http://127.0.0.1:8765)

Rules

- Dependencies are added in `pyproject.toml`. Do not add one without
  asking


Documents

- `_docs/process.md` - how work is organized
<!-- - Before writing tests, read `_docs/testing-guidelines.md` -->
<!-- - For anything touching the UI, read `_docs/design-system.md` -->