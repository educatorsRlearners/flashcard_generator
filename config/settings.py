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

# "submissions" is listed before "huey.contrib.djhuey" on purpose (issue
# #80): Django resolves a management command name to the
# earliest-listed app that provides it, so this order lets
# submissions/management/commands/run_huey.py override the vendored
# huey.contrib.djhuey run_huey command (adding the pending-migrations
# fail-fast check) without editing the vendored file.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "submissions",
    "huey.contrib.djhuey",
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
# Shared fallback helper for the per-provider LLM_* env parsing below
# (issue #126). Mirrors submissions/llm.py::_resolve_provider_setting without
# importing it (settings load before the app - circular-import risk): the
# provider/env-name mapping is duplicated here by name, kept in sync by hand.
def _llm_env(name, default):
    return os.environ.get(name, default)


LLM_PROVIDER = _llm_env("LLM_PROVIDER", "anthropic")
LLM_MODEL = _llm_env("LLM_MODEL", "claude-sonnet-5")
# Name of the environment variable that holds the provider API key. The key
# itself is read from os.environ at call time and never stored in settings.
LLM_API_KEY_ENV_VAR = _llm_env("LLM_API_KEY_ENV_VAR", "ANTHROPIC_API_KEY")
# Default output-token ceiling when a caller does not pass max_tokens.
LLM_MAX_TOKENS = int(_llm_env("LLM_MAX_TOKENS", "4096"))
# OpenAI-compatible provider (issue #27; adapter in submissions/llm.py).
# Base URL of the OpenAI-compatible chat-completions endpoint. Point at any
# OpenAI-compatible gateway (OpenAI, Ollama, vLLM, ...) with no code change.
LLM_OPENAI_BASE_URL = _llm_env(
    "LLM_OPENAI_BASE_URL", "https://api.openai.com/v1"
)
# Optional per-provider overrides: when non-empty these win over the generic
# LLM_MODEL / LLM_API_KEY_ENV_VAR above for the openai-compatible provider.
# Empty (default) falls back to the generic settings, so a provider swap can
# be just LLM_PROVIDER + LLM_MODEL (+ base URL / key env var as needed).
LLM_OPENAI_MODEL = _llm_env("LLM_OPENAI_MODEL", "")
LLM_OPENAI_API_KEY_ENV_VAR = _llm_env("LLM_OPENAI_API_KEY_ENV_VAR", "")

# Gemini provider (issue #83; adapter in submissions/llm.py). Talks to
# Google's Generative Language API directly via httpx, so there is no
# LLM_GEMINI_BASE_URL - the endpoint is a fixed module constant, not
# configurable. Optional per-provider overrides below follow the same
# fallback pattern as LLM_OPENAI_* above: empty falls back to the generic
# LLM_MODEL / LLM_API_KEY_ENV_VAR. No new env-var name is invented for the
# key itself - conventionally set LLM_GEMINI_API_KEY_ENV_VAR=GOOGLE_API_KEY
# (or set the generic LLM_API_KEY_ENV_VAR=GOOGLE_API_KEY and leave this
# blank), and export GOOGLE_API_KEY yourself.
LLM_GEMINI_MODEL = _llm_env("LLM_GEMINI_MODEL", "")
LLM_GEMINI_API_KEY_ENV_VAR = _llm_env("LLM_GEMINI_API_KEY_ENV_VAR", "")

# Grok provider (issue #98; adapter in submissions/llm.py). A named
# OpenAI-compatible provider pointed at xAI's chat-completions endpoint.
# Optional per-provider overrides below follow the same fallback pattern as
# LLM_OPENAI_* above: empty falls back to the hardcoded base_url/key env var
# defaults in submissions/llm.py (or the generic LLM_MODEL for the model).
LLM_GROK_BASE_URL = _llm_env("LLM_GROK_BASE_URL", "")
LLM_GROK_MODEL = _llm_env("LLM_GROK_MODEL", "")
LLM_GROK_API_KEY_ENV_VAR = _llm_env("LLM_GROK_API_KEY_ENV_VAR", "")

# OpenRouter provider (issue #84/#98; adapter in submissions/llm.py). A named
# OpenAI-compatible provider pointed at OpenRouter's chat-completions
# endpoint. Optional per-provider overrides below follow the same fallback
# pattern as LLM_OPENAI_* above: empty falls back to the hardcoded
# base_url/key env var defaults in submissions/llm.py (or the generic
# LLM_MODEL for the model).
LLM_OPENROUTER_BASE_URL = _llm_env("LLM_OPENROUTER_BASE_URL", "")
LLM_OPENROUTER_MODEL = _llm_env("LLM_OPENROUTER_MODEL", "")
LLM_OPENROUTER_API_KEY_ENV_VAR = _llm_env(
    "LLM_OPENROUTER_API_KEY_ENV_VAR", ""
)

# OpenCode Zen provider (issue #104; adapter in submissions/llm.py). A named
# OpenAI-compatible provider pointed at Zen's OpenAI-compatible route
# (https://opencode.ai/zen/v1/chat/completions). Optional per-provider
# overrides below follow the same fallback pattern as LLM_GEMINI_* above:
# empty falls back to the hardcoded base_url/key env var defaults in
# submissions/llm.py (or the generic LLM_MODEL for the model). No Zen model
# id is hardcoded as a default anywhere.
LLM_OPENCODE_ZEN_BASE_URL = _llm_env("LLM_OPENCODE_ZEN_BASE_URL", "")
LLM_OPENCODE_ZEN_MODEL = _llm_env("LLM_OPENCODE_ZEN_MODEL", "")
LLM_OPENCODE_ZEN_API_KEY_ENV_VAR = _llm_env(
    "LLM_OPENCODE_ZEN_API_KEY_ENV_VAR", ""
)

# --- LLM cost/failure-rate alerting (submissions/tasks.py, issue #90) ------
# A periodic Huey task watches recent LLMCall rows and logs a WARNING when
# total estimated cost or failure rate over a rolling window crosses one of
# these thresholds. Both are unset (blank) by default so existing installs
# get no alerting until explicitly configured. A stray/malformed value
# (non-numeric, negative) is treated the same as unset - see
# ``submissions.tasks._parse_positive_float``.
LLM_ALERT_COST_USD_THRESHOLD = os.environ.get("LLM_ALERT_COST_USD_THRESHOLD", "")
#: Percentage 0-100, e.g. "50" means 50%.
LLM_ALERT_FAILURE_RATE_THRESHOLD = os.environ.get(
    "LLM_ALERT_FAILURE_RATE_THRESHOLD", ""
)
#: Rolling window size in minutes for both checks above.
LLM_ALERT_WINDOW_MINUTES = os.environ.get("LLM_ALERT_WINDOW_MINUTES", "60")

# --- Card generation (submissions/generation.py, issue #32) ---------------
# Language/domain used for programming analogies in generated cards.
# Surfaced into the generation prompt as
# "When you use a programming analogy, use <language>." Changing it is a
# settings/env change, no code edit.
CARD_ANALOGY_LANGUAGE = os.environ.get("CARD_ANALOGY_LANGUAGE", "python")

# --- Smart few-shot feedback selection (submissions/feedback.py, issue #25)
# Relevance-ranked, token-budgeted pick of stored feedback examples for the
# current page. All have documented defaults; change via env, no code edit.
# * FEWSHOT_ENABLED: master switch (default on). Set FEWSHOT_ENABLED=0 to
#   disable the few-shot section without editing prompt code; when disabled
#   no Feedback DB query and no embedding/similarity call is made (a zero
#   FEWSHOT_TOKEN_BUDGET disables it the same way).
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
FEWSHOT_ENABLED = os.environ.get("FEWSHOT_ENABLED", "1") == "1"
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

# --- Backend origin (native host + dev supervisor, issue #47) -------------
# Single env var shared by native_host/host.py (readiness probe, spawn
# addrport, base_url reply) and submissions/management/commands/dev.py
# (--addrport default). Same name and same default in all three places is
# what keeps them from drifting; host.py duplicates the parsing helper
# because it is stdlib-only. An explicit `dev --addrport` flag always wins
# over this value.
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")

# --- Browser extension auth (submissions/extension_auth.py, issue #33) ---
# Local shared-secret token file the extension presents to authenticate its
# requests to this backend (checked by #35; minted/read by #37/#38's native
# messaging host on first run). Minted/read/shown via
# ``manage.py extension_token`` and ``submissions/extension_auth.py``.
EXTENSION_TOKEN_FILE = BASE_DIR / ".extension_token"
# The unpacked extension's chrome-extension://<id> origin, set once after
# loading the extension in developer mode (issue #35). A fixed, operator-set
# value rather than reflecting the request's Origin header against an
# allow-list: this is a single local developer's extension talking to a
# single local backend, so a static value is simpler and fails closed (no
# Access-Control-Allow-Origin header emitted, so the browser blocks the
# response) if it is never configured, instead of an allow-list regex that
# has to be gotten right to avoid accepting an unintended origin.
EXTENSION_ID = os.environ.get("EXTENSION_ID", "")
