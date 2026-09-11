# flashcard_generator — macOS (Apple Silicon) only.
#
#   make setup   first-time setup: install + check
#   make run     start the backend (Django dev server + Huey worker)
#
# Local service URLs (overridable, same defaults as config/settings.py):
#   BACKEND_URL      default http://127.0.0.1:8000
#   ANKI_CONNECT_URL default http://127.0.0.1:8765
#   DRAW_THINGS_URL  default http://127.0.0.1:7860

.DEFAULT_GOAL := help

VENV := .venv
BACKEND_URL ?= http://127.0.0.1:8000
ANKI_CONNECT_URL ?= http://127.0.0.1:8765
DRAW_THINGS_URL ?= http://127.0.0.1:7860

.PHONY: help install check run setup clean

help: ## List all available targets (default).
	@echo "Available targets:"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "  %-10s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create venv if missing, install deps, run Django migrations.
	@echo "==> install: checking for 'uv'..."
	@command -v uv >/dev/null 2>&1 || (echo "ERROR: 'uv' is not installed. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1)
	@echo "==> install: resolving dependency file..."
	@if [ -f requirements.txt ]; then \
		echo "==> install: found requirements.txt; installing from it..."; \
		if [ ! -d "$(VENV)" ]; then echo "==> install: creating virtualenv at $(VENV)..."; uv venv "$(VENV)" || (echo "ERROR: 'uv venv' failed."; exit 1); else echo "==> install: virtualenv already exists at $(VENV); reusing it."; fi; \
		uv pip install --python "$(VENV)/bin/python" -r requirements.txt || (echo "ERROR: dependency install from requirements.txt failed."; exit 1); \
	elif [ -f pyproject.toml ]; then \
		echo "==> install: found pyproject.toml; syncing with 'uv sync' (creates $(VENV) if missing)..."; \
		uv sync || (echo "ERROR: 'uv sync' failed."; exit 1); \
	else \
		echo "ERROR: no dependency file found. Expected requirements.txt or pyproject.toml in the project root."; exit 1; \
	fi
	@echo "==> install: running Django migrations..."
	@uv run python manage.py migrate || (echo "ERROR: 'manage.py migrate' failed. Is Django installed and config/settings.py valid?"; exit 1)
	@echo "==> install: done. Next: 'make check', then 'make run'."

# CHECK_STRICT=1 promotes optional-service failures to fatal
# (e.g. CI). Default: optional services warn, required checks fail.
CHECK_STRICT ?= 0

check: ## Verify non-pip prerequisites. Required checks fail; Anki/SD are optional and warn. Prints PASS/FAIL per check.
	@echo "==> check: probing local prerequisites (Anki / Stable Diffusion are optional and never block 'make run')..."
	@hard_fail=0; soft_fail=0; \
	check_pass() { echo "[PASS] $$1"; }; \
	check_hard_fail() { echo "[FAIL] $$1 -- $$2"; hard_fail=1; }; \
	check_soft_fail() { echo "[FAIL] $$1 -- $$2"; soft_fail=1; }; \
	if [ -d "$(VENV)" ]; then check_pass "virtualenv exists at $(VENV)"; else check_hard_fail "virtualenv at $(VENV)" "run 'make install' first"; fi; \
	if [ -f extension/manifest.json ]; then check_pass "Chrome/Brave extension present (extension/manifest.json)"; else check_hard_fail "extension/manifest.json" "the unpacked extension is missing from extension/"; fi; \
	if pgrep -x Anki >/dev/null 2>&1; then check_pass "Anki app is running"; else check_soft_fail "Anki app process (optional)" "open Anki (AnkiConnect needs the app running with its add-on installed)"; fi; \
	if curl -s -m 5 -o /dev/null -X POST "$(ANKI_CONNECT_URL)" -H 'Content-Type: application/json' -d '{"action":"version","version":6}'; then check_pass "AnkiConnect reachable at $(ANKI_CONNECT_URL)"; else check_soft_fail "AnkiConnect at $(ANKI_CONNECT_URL) (optional)" "is Anki open with the AnkiConnect add-on installed and listening? (default http://127.0.0.1:8765)"; fi; \
	if curl -s -m 5 -o /dev/null "$(DRAW_THINGS_URL)"; then check_pass "Stable Diffusion server reachable at $(DRAW_THINGS_URL)"; else check_soft_fail "Stable Diffusion server at $(DRAW_THINGS_URL) (optional)" "start Draw Things with its HTTP API server enabled (Draw Things -> Settings -> API Server); cards are still generated without images if it is down"; fi; \
	if [ "$(CHECK_STRICT)" = "1" ] && [ $$soft_fail -ne 0 ]; then echo "==> check: CHECK_STRICT=1, treating optional failures as fatal."; exit 1; fi; \
	if [ $$hard_fail -ne 0 ]; then echo "==> check: REQUIRED checks failed (see [FAIL] lines above)."; exit 1; fi; \
	if [ $$soft_fail -ne 0 ]; then echo "==> check: required checks passed; optional services are down (see [FAIL] lines above). Core flow ('make run') is unaffected."; exit 0; fi; \
	echo "==> check: all checks passed."

run: ## Start the Django dev server plus the Huey background worker (Ctrl-C stops both).
	@echo "==> run: starting backend at $(BACKEND_URL) (web + worker via 'manage.py dev')..."
	@if [ ! -d "$(VENV)" ]; then echo "ERROR: no virtualenv at $(VENV). Run 'make install' first."; exit 1; fi
	@uv run python manage.py dev || (echo "ERROR: backend exited. See the [web]/[worker] log lines above."; exit 1)

setup: install check ## First-time setup: install, then check prerequisites.

clean: ## Remove the virtualenv and cached/build artifacts (keeps db.sqlite3, media/, .env).
	@echo "==> clean: removing virtualenv and caches..."
	@rm -rf "$(VENV)" __pycache__ .pytest_cache .coverage htmlcov .ruff_cache
	@find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null; true
	@find . -type f -name '*.py[cod]' -delete 2>/dev/null; true
	@echo "==> clean: done. Kept: db.sqlite3, huey.sqlite3, media/, .env (delete those by hand if you really want a fresh slate)."
