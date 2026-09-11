import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load a local, git-ignored .env file (e.g. ANTHROPIC_API_KEY) before any
# os.environ lookups below. Real environment variables always win.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = "dev-insecure-key-not-for-production"

DEBUG = True

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "huey.contrib.djhuey",
    "submissions",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

# --- Media files (issue #12: per-card images) --------------------------
# Card images (source-page or Draw Things) are stored on disk here.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Draw Things fallback image generation (issue #12) -----------------
# When a generated card has no usable source-page image, an image is
# requested from a locally running Draw Things via its HTTP API
# (Automatic1111-compatible ``/sdapi/v1/txt2img``). This task assumes
# Draw Things is already installed and its API server is enabled
# (Draw Things -> Settings -> API Server); standing it up is out of scope.
# If it is unreachable / disabled the card is simply produced with no
# image - batch generation never aborts.
DRAW_THINGS_URL = os.environ.get("DRAW_THINGS_URL", "http://127.0.0.1:7860")
DRAW_THINGS_ENABLED = os.environ.get("DRAW_THINGS_ENABLED", "1") == "1"

# --- Background batch processing (Huey, issue #8) -----------------------
# One task per submitted URL runs the extraction path in a background
# consumer. The broker is a local SQLite file (huey.sqlite3) so no Redis
# or extra service is needed. Dev entrypoint (issue #20):
#     uv run python manage.py dev          # runserver + consumer together
# Manual fallback:
#     uv run python manage.py run_huey
# Set HUEY_IMMEDIATE=1 to run tasks inline in the submitting process (no
# consumer needed); the test suite forces immediate mode via a fixture.
HUEY = {
    "huey_class": "huey.SqliteHuey",
    "name": "flashcard_generator",
    "filename": str(BASE_DIR / "huey.sqlite3"),
    "immediate": os.environ.get("HUEY_IMMEDIATE", "") == "1",
    "immediate_use_memory": True,
    "results": False,
    "utc": True,
    "consumer": {
        "workers": 4,
        "worker_type": "thread",
    },
}

#: A batch with URLs still pending this many seconds after it was created
#: and with nothing processed yet is reported as "worker not running".
HUEY_WORKER_STALE_SECONDS = int(os.environ.get("HUEY_WORKER_STALE_SECONDS", "15"))

# --- LLM client (submissions/llm.py) -------------------------------------
# Which provider/model the in-process LLM client talks to. All of these are
# configuration, not code: change the model with an env var, no code edit.
# See README.md ("LLM client") for details.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5")
# Name of the environment variable that holds the provider API key. The key
# itself is read from os.environ at call time and never stored in settings.
LLM_API_KEY_ENV_VAR = os.environ.get("LLM_API_KEY_ENV_VAR", "ANTHROPIC_API_KEY")
# Default output-token ceiling when a caller does not pass max_tokens.
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4096"))
# OpenAI-compatible provider (issue #27; adapter in submissions/llm.py).
# Base URL of the OpenAI-compatible chat-completions endpoint. Point at any
# OpenAI-compatible gateway (OpenAI, Ollama, vLLM, ...) with no code change.
LLM_OPENAI_BASE_URL = os.environ.get(
    "LLM_OPENAI_BASE_URL", "https://api.openai.com/v1"
)
# Optional per-provider overrides: when non-empty these win over the generic
# LLM_MODEL / LLM_API_KEY_ENV_VAR above for the openai-compatible provider.
# Empty (default) falls back to the generic settings, so a provider swap can
# be just LLM_PROVIDER + LLM_MODEL (+ base URL / key env var as needed).
LLM_OPENAI_MODEL = os.environ.get("LLM_OPENAI_MODEL", "")
LLM_OPENAI_API_KEY_ENV_VAR = os.environ.get("LLM_OPENAI_API_KEY_ENV_VAR", "")

# --- Card generation (submissions/generation.py, issue #32) ---------------
# Language/domain used for programming analogies in generated cards.
# Surfaced into the generation prompt as
# "When you use a programming analogy, use <language>." Changing it is a
# settings/env change, no code edit.
CARD_ANALOGY_LANGUAGE = os.environ.get("CARD_ANALOGY_LANGUAGE", "python")

# --- Smart few-shot feedback selection (submissions/generation.py, issue #25)
# Relevance-ranked, token-budgeted pick of stored feedback examples for the
# current page. All have documented defaults; change via env, no code edit.
# * FEWSHOT_TOKEN_BUDGET: max tokens for the whole few-shot section (both
#   categories combined). Examples are added in rank order until the next one
#   would exceed it, so the prompt never grows unbounded.
# * FEWSHOT_ACCEPTED_SHARE: fraction of the budget reserved for accepted
#   examples (1 - share goes to rejected) so one side cannot crowd out the
#   other.
# * FEWSHOT_SELECTION_MODE: "relevance" (default, rank by similarity to the
#   page) or "recency" (fall back to #10's most-recent-N-per-category pick).
# * FEWSHOT_MIN_FEEDBACK_CHARS: feedback whose example text is shorter than
#   this (non-whitespace chars) is skipped gracefully, never fatal.
# * FEWSHOT_CHARS_PER_TOKEN: token-count approximation for the Claude family
#   (~4 chars/token, Anthropic's rule of thumb). The exact tokenizer is
#   server-side; mirroring it locally would need a new dependency, so the
#   budget check uses this documented approximation (stubbed in tests).
# * FEWSHOT_EMBED_FN: optional dotted path to an embedding backend
#   ``fn(list[str]) -> list[list[float] | None]`` for a future #5 embedding
#   method. Unset (default) uses the offline token-overlap default; a failing
#   backend degrades to the recency cap instead of crashing generation.
FEWSHOT_TOKEN_BUDGET = int(os.environ.get("FEWSHOT_TOKEN_BUDGET", "2000"))
FEWSHOT_ACCEPTED_SHARE = float(os.environ.get("FEWSHOT_ACCEPTED_SHARE", "0.5"))
FEWSHOT_SELECTION_MODE = os.environ.get("FEWSHOT_SELECTION_MODE", "relevance")
FEWSHOT_MIN_FEEDBACK_CHARS = int(os.environ.get("FEWSHOT_MIN_FEEDBACK_CHARS", "20"))
FEWSHOT_CHARS_PER_TOKEN = int(os.environ.get("FEWSHOT_CHARS_PER_TOKEN", "4"))
FEWSHOT_EMBED_FN = os.environ.get("FEWSHOT_EMBED_FN", "")

# --- Image OCR (submissions/extraction.py, issue #19) ----------------------
# URLs that *are* an image (PNG/JPEG/WebP/TIFF) or an image-only (scanned)
# PDF are OCR'd with Tesseract via the ``pytesseract`` binding. The native
# ``tesseract`` binary lives outside ``uv`` (brew/apt install); the binding
# itself is a regular ``uv`` dependency. OCR is enabled by default and works
# whenever the toolchain is present — purely local, localhost-only, no
# server or API key. Set OCR_ENABLED=0 to disable it (image URLs then fail
# cleanly with a setup hint, exactly as if the toolchain were absent).
# OCR_TIMEOUT_SECONDS bounds how long one file can occupy the worker.
OCR_ENABLED = os.environ.get("OCR_ENABLED", "1") == "1"
OCR_TIMEOUT_SECONDS = float(os.environ.get("OCR_TIMEOUT_SECONDS", "60"))

# --- Anki sync (submissions/anki.py, issue #11) -------------------------
# The single deck accepted cards are pushed into, and the base URL of the
# AnkiConnect add-on's local HTTP server. Both are configuration: point at a
# different deck or a remote AnkiConnect with an env var, no code edit.
ANKI_DECK_NAME = os.environ.get("ANKI_DECK_NAME", "Flashcard Generator")
ANKI_CONNECT_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")
# Seconds to wait on any single AnkiConnect HTTP call before treating Anki
# as unreachable.
ANKI_CONNECT_TIMEOUT = float(os.environ.get("ANKI_CONNECT_TIMEOUT", "10"))

# --- Browser extension auth (submissions/extension_auth.py, issue #33) ---
# Local shared-secret token file the extension presents to authenticate its
# requests to this backend (checked by #35; minted/read by #37/#38's native
# messaging host on first run). Minted/read/shown via
# ``manage.py extension_token`` and ``submissions/extension_auth.py``.
EXTENSION_TOKEN_FILE = BASE_DIR / ".extension_token"
