# flashcard_generator

Walking skeleton: a Django app on SQLite with one page that accepts URLs,
persists them, and lists them back.

## Setup

```
uv sync
```

## Run

```
uv run python manage.py migrate
uv run python manage.py runserver
```

Then open http://127.0.0.1:8000/ , paste one or more URLs (one per line)
into the textarea, and submit. Saved URLs are listed back on the page and
stored in `db.sqlite3`.

## Tests

```
uv run pytest
```
