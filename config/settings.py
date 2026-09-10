import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Background batch processing (Huey, issue #8) -----------------------
# One task per submitted URL runs the extraction path in a background
# consumer. The broker is a local SQLite file (huey.sqlite3) so no Redis
# or extra service is needed. Start the consumer with:
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
