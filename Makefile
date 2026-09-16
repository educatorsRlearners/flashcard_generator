# flashcard_generator — macOS (Apple Silicon) only.
#
#   make setup   one-command extension-ready setup (idempotent, no hand-run uv commands)
#   make run     ensure artifacts fast, then start the backend (Django dev server + Huey worker)
#   make doctor  diagnose setup artifacts read-only (per-check PASS/FAIL with fix hints)
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

.PHONY: help install check run setup clean ensure check-llm remind-browser doctor

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

# doctor: read-only diagnosis of the setup artifacts `make setup` creates
# (signing key, token, wrapper, browser manifests, EXTENSION_ID, LLM key,
# .env). Prints one [PASS]/[FAIL] line per check ([SKIP] for a browser that
# is not installed); every FAIL names its fix command or env var. Never
# writes, mints, or regenerates anything (no --force anywhere), makes no
# network calls, and never launches a browser, so it is safe headless/in CI.
# It never duplicates `make check`: venv / extension-dir / Anki /
# AnkiConnect / Draw Things stay in `check`. Detection logic and hint
# wording are reused from generate_signing_key / install_native_host /
# check-llm (placeholder string, derived-ID algorithm, browser dirs, key-var
# resolution, shadowing/restart wording) so hints cannot drift.
doctor: ## Diagnose setup artifacts read-only (PASS/FAIL per check with fix hints; no writes, no network).
	@echo "==> doctor: checking setup artifacts (read-only: no writes, no network calls, no browser launch)..."
	@fail=0; \
	if command -v openssl >/dev/null 2>&1; then echo "[PASS] openssl on PATH ($$(command -v openssl))"; else echo '[FAIL] openssl on PATH -- openssl was not found on PATH. Install it (e.g. via Xcode Command Line Tools: `xcode-select --install`) and re-run `uv run python manage.py generate_signing_key`'; fail=1; fi; \
	out=$$(uv run python -c "exec('from submissions.management.commands.install_native_host import EXTENSION_MANIFEST_PATH as mp, PLACEHOLDER_KEY as ph, read_manifest_key as rmk\ntry:\n    k = rmk(mp)\nexcept Exception as e:\n    print(\"[FAIL] signing key pinned in extension/manifest.json -- %s Fix: run \\'uv run python manage.py generate_signing_key\\' (never --force here; --force regenerates the key and gives the extension a new ID).\" % e)\n    raise SystemExit(1)\nif k == ph:\n    print(\"[FAIL] signing key pinned in extension/manifest.json -- manifest key is still the placeholder. Fix: run \\'uv run python manage.py generate_signing_key\\' (never --force here; --force regenerates the key and gives the extension a new ID).\")\n    raise SystemExit(1)\nprint(\"[PASS] signing key pinned in extension/manifest.json\")')" 2>&1); status=$$?; echo "$$out"; if [ $$status -ne 0 ]; then fail=1; fi; \
	out=$$(uv run python -c "exec('from submissions.management.commands.generate_signing_key import PRIVATE_KEY_PATH as pk\nif pk.exists():\n    print(\"[PASS] private key present (.extension_signing_key.pem)\")\nelse:\n    print(\"[FAIL] private key present (.extension_signing_key.pem) -- missing at %s. Fix: re-run \\'uv run python manage.py generate_signing_key\\'.\" % pk)\n    raise SystemExit(1)')" 2>&1); status=$$?; echo "$$out"; if [ $$status -ne 0 ]; then fail=1; fi; \
	out=$$(uv run python -c "exec('import os as _os, django as _dj\n_os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\", \"config.settings\")\n_dj.setup()\nfrom django.conf import settings as _s\np = _s.EXTENSION_TOKEN_FILE\nif p.exists():\n    print(\"[PASS] extension token present (%s)\" % p.name)\nelse:\n    print(\"[FAIL] extension token present (%s) -- missing at %s. Fix: re-run \\'uv run python manage.py install_native_host\\' (mints the token on first run) or run \\'uv run python manage.py extension_token --mint\\'.\" % (p.name, p))\n    raise SystemExit(1)')" 2>&1); status=$$?; echo "$$out"; if [ $$status -ne 0 ]; then fail=1; fi; \
	out=$$(uv run python -c "exec('import os as _os\nfrom submissions.management.commands.install_native_host import WRAPPER_SCRIPT_PATH as wsp\nif not wsp.exists():\n    print(\"[FAIL] wrapper present and executable (native_host/run_host.sh) -- missing at %s. Fix: re-run \\'uv run python manage.py install_native_host\\'.\" % wsp)\n    raise SystemExit(1)\nif not _os.access(wsp, _os.X_OK):\n    print(\"[FAIL] wrapper present and executable (native_host/run_host.sh) -- %s exists but is not executable. Fix: re-run \\'uv run python manage.py install_native_host\\'.\" % wsp)\n    raise SystemExit(1)\nprint(\"[PASS] wrapper present and executable (native_host/run_host.sh)\")')" 2>&1); status=$$?; echo "$$out"; if [ $$status -ne 0 ]; then fail=1; fi; \
	out=$$(uv run python -c "exec('from submissions.management.commands.install_native_host import CANDIDATE_BROWSERS as cb, MANIFEST_FILENAME as mf\nfailed = False\nseen = False\nfor label, bdir, ndir in cb:\n    p = ndir / mf\n    if not bdir.exists():\n        print(\"[SKIP] %s not installed (%s)\" % (label, bdir))\n        continue\n    seen = True\n    if p.exists():\n        print(\"[PASS] %s native-messaging manifest present (%s)\" % (label, p))\n    else:\n        print(\"[FAIL] %s native-messaging manifest missing (%s) -- Fix: re-run \\'uv run python manage.py install_native_host\\'.\" % (label, p))\n        failed = True\nif not seen:\n    print(\"[FAIL] no supported browser installed -- none of these directories exist: %s. Fix: install Chrome or Brave, then re-run \\'uv run python manage.py install_native_host\\'.\" % (\", \".join(\"%s (%s)\" % (label, bdir) for label, bdir, _nd in cb)))\n    failed = True\nraise SystemExit(1 if failed else 0)')" 2>&1); status=$$?; echo "$$out"; if [ $$status -ne 0 ]; then fail=1; fi; \
	out=$$(uv run python -c "exec('import os as _os\nfrom submissions.management.commands.install_native_host import ENV_PATH as ep, EXTENSION_MANIFEST_PATH as mp, PLACEHOLDER_KEY as ph, RESTART_NOTE as rn, derive_extension_id as dei, read_manifest_key as rmk\nderived = None\nread_error = None\ntry:\n    _k = rmk(mp)\n    derived = None if _k == ph else dei(_k)\nexcept Exception as _e:\n    read_error = _e\ndotenv_val = None\nif ep.exists():\n    for _line in ep.read_text().splitlines():\n        _s = _line.strip()\n        if _s.startswith(\"export \"):\n            _s = _s[len(\"export \"):].strip()\n        if not _s or _s.startswith(chr(35)) or \"=\" not in _s:\n            continue\n        _name, _, _val = _s.partition(\"=\")\n        if _name.strip() == \"EXTENSION_ID\":\n            dotenv_val = _val.strip().strip(chr(34) + chr(39))\nshell_val = _os.environ.get(\"EXTENSION_ID\") or None\nproblems = []\nif derived is None:\n    if read_error is not None:\n        problems.append(\"cannot derive an ID from %s (%s)\" % (mp, read_error))\n    else:\n        problems.append(\"manifest key is still the placeholder, so there is no stable ID to match\")\n    problems.append(\"fix: run \\'uv run python manage.py generate_signing_key\\' first (never --force here), then re-run \\'uv run python manage.py install_native_host\\'\")\nelse:\n    if dotenv_val is None:\n        problems.append(\".env has no EXTENSION_ID line (fix: re-run \\'uv run python manage.py install_native_host\\'; %s)\" % rn)\n    elif dotenv_val != derived:\n        problems.append(\".env EXTENSION_ID is stale (%s != derived %s) (fix: re-run \\'uv run python manage.py install_native_host\\'; %s)\" % (dotenv_val, derived, rn))\n    if shell_val is not None and shell_val != derived:\n        problems.append(\"shell \$$EXTENSION_ID=%s disagrees with derived %s -- Warning: EXTENSION_ID is also set as a real environment variable in this shell - that takes precedence over the value just written to .env (config/settings.py calls load_dotenv with its default override=False), so unset it or update it too for the new ID to actually take effect.\" % (shell_val, derived))\nif problems:\n    print(\"[FAIL] EXTENSION_ID three-way match (derived=%s, .env=%s, shell=%s) -- %s\" % (derived, dotenv_val, shell_val, \"; \".join(problems)))\n    raise SystemExit(1)\nprint(\"[PASS] EXTENSION_ID three-way match (derived=%s, .env=%s, shell=%s)\" % (derived, dotenv_val, shell_val if shell_val is not None else \"unset\"))')" 2>&1); status=$$?; echo "$$out"; if [ $$status -ne 0 ]; then fail=1; fi; \
	out=$$(uv run python -c "exec('import os as _os, django as _dj\n_os.environ.setdefault(\"DJANGO_SETTINGS_MODULE\", \"config.settings\")\n_dj.setup()\nfrom submissions import llm as _llm\ntry:\n    _p = _llm.get_provider()\nexcept Exception as _e:\n    print(\"[FAIL] LLM key for configured provider -- %s: %s\" % (type(_e).__name__, _e))\n    raise SystemExit(1)\n_v = _p.api_key_env_var\nif _os.environ.get(_v):\n    print(\"[PASS] LLM key present (%s key in %s)\" % (_p.name, _v))\nelse:\n    print(\"[FAIL] LLM key for configured provider -- No API key found. Set the %s environment variable (LLM_PROVIDER=%s).\" % (_v, _p.name))\n    raise SystemExit(1)')" 2>&1); status=$$?; echo "$$out"; if [ $$status -ne 0 ]; then fail=1; fi; \
	out=$$(uv run python -c "exec('from submissions.management.commands.install_native_host import ENV_PATH as ep, ENV_EXAMPLE_PATH as ex\nif ep.exists():\n    print(\"[PASS] .env present (%s)\" % ep)\nelse:\n    print(\"[FAIL] .env present -- %s is missing (bootstrapped from %s by install_native_host). Fix: re-run \\'uv run python manage.py install_native_host\\'.\" % (ep, ex))\n    raise SystemExit(1)')" 2>&1); status=$$?; echo "$$out"; if [ $$status -ne 0 ]; then fail=1; fi; \
	if [ $$fail -ne 0 ]; then echo "==> doctor: some checks FAILED (see [FAIL] lines above; each names its fix)."; exit 1; fi; \
	echo "==> doctor: all checks passed."

# ensure: shared idempotent prerequisite chain for `setup` and `run`
# (venv/deps -> migrations -> signing key -> native host). Every step
# fast-skips with a message when its artifacts already exist; nothing is
# ever regenerated. The LLM-key presence guard is the separate `check-llm`
# target so it can also run on its own.
ensure: ## Ensure venv, migrations, signing key, and native host (skips present artifacts; safe to re-run).
	@echo "==> ensure: venv/deps..."
	@if [ -d "$(VENV)" ]; then \
		echo "==> ensure: virtualenv exists at $(VENV); skipping 'uv sync'."; \
	else \
		echo "==> ensure: no virtualenv at $(VENV); running 'uv sync'..."; \
		uv sync || (echo "ERROR: 'uv sync' failed."; exit 1); \
	fi
	@echo "==> ensure: Django migrations..."
	@if uv run python manage.py migrate --check >/dev/null 2>&1; then \
		echo "==> ensure: migrations already applied; skipping 'migrate'."; \
	else \
		echo "==> ensure: unapplied migrations found; running 'migrate'..."; \
		uv run python manage.py migrate || (echo "ERROR: 'manage.py migrate' failed. Is Django installed and config/settings.py valid?"; exit 1); \
	fi
	@echo "==> ensure: extension signing key (setup/run never regenerates an existing key)..."
	@if uv run python -c "import sys; from submissions.management.commands.generate_signing_key import MANIFEST_PATH, PLACEHOLDER_KEY, PRIVATE_KEY_PATH, read_manifest; m = read_manifest(MANIFEST_PATH); sys.exit(0 if (m.get('key') != PLACEHOLDER_KEY and PRIVATE_KEY_PATH.exists()) else 1)" >/dev/null 2>&1; then \
		echo "==> ensure: real signing key already pinned (private key present); skipping 'generate_signing_key'."; \
	else \
		echo "==> ensure: pinning a real signing key via 'generate_signing_key'..."; \
		out=$$(uv run python manage.py generate_signing_key 2>&1); status=$$?; echo "$$out"; \
		if [ $$status -eq 0 ]; then \
			echo "==> ensure: signing key generated and pinned."; \
		elif echo "$$out" | grep -q "already has a real signing key"; then \
			echo "==> ensure: real signing key already pinned; keeping it (skip, not failure)."; \
		else \
			exit $$status; \
		fi; \
	fi
	@echo "==> ensure: native-messaging host..."
	@if uv run python -c "import os, sys, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup(); from django.conf import settings as s; from submissions.management.commands.install_native_host import EXTENSION_MANIFEST_PATH as mp, PLACEHOLDER_KEY as ph, WRAPPER_SCRIPT_PATH as wsp, derive_extension_id as dei, read_manifest_key as rmk; k = rmk(mp); d = dei(k) if k != ph else None; sys.exit(0 if (d is not None and s.EXTENSION_TOKEN_FILE.exists() and wsp.exists() and s.EXTENSION_ID == d) else 1)" >/dev/null 2>&1; then \
		echo "==> ensure: native host registered (wrapper, token, and matching EXTENSION_ID present); skipping 'install_native_host'."; \
	else \
		echo "==> ensure: running 'install_native_host' (derives ID, mints token on first install, sets EXTENSION_ID in .env)..."; \
		uv run python manage.py install_native_host || (echo "ERROR: 'install_native_host' failed (missing browser? placeholder key?). See the error above."; exit 1); \
	fi

check-llm: ## Presence-check the configured LLM provider key (no network call); fails naming the exact env var.
	@echo "==> check-llm: resolving configured provider key (presence only, no network call)..."
	@uv run python -c "import os, sys, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup(); from submissions import llm; p = llm.get_provider(); v = p.api_key_env_var; sys.exit('ERROR: No API key found. Set the %s environment variable (LLM_PROVIDER=%s).' % (v, p.name)) if not os.environ.get(v) else print('==> check-llm: %s key present in %s.' % (p.name, v))"

remind-browser: ## Print the manual unpacked-extension load reminder (the browser cannot be automated).
	@echo "==> Manual browser step (the browser cannot be automated; do this once by hand):"
	@echo "    1. Load the unpacked extension: open brave://extensions (or chrome://extensions) -> enable Developer mode -> 'Load unpacked' -> select $$(pwd)/extension"
	@uv run python -c "from submissions.management.commands.install_native_host import EXTENSION_MANIFEST_PATH as mp, RESTART_NOTE as rn, derive_extension_id as dei, read_manifest_key as rmk; print('    2. Extension ID derived from %s: %s (cross-check it against the ID shown in the browser UI).' % (mp, dei(rmk(mp)))); print('    3. ' + rn)" 2>/dev/null || { echo "    2. (Could not derive the extension ID yet - finish 'make setup' first, then re-run.)"; echo "    3. (Restart any already-running backend after 'make setup' - .env is read once at process start.)"; }
	@echo "    4. If Brave/Chrome was already open when the manifest was written, reload the extension once more before using it."
	@if [ -n "$${EXTENSION_ID:-}" ]; then echo "    WARNING: Warning: EXTENSION_ID is also set as a real environment variable in this shell - that takes precedence over the value just written to .env (config/settings.py calls load_dotenv with its default override=False), so unset it or update it too for the new ID to actually take effect."; fi

run: ensure check-llm remind-browser ## Ensure artifacts fast, then start the backend (dev server + Huey worker; Ctrl-C stops both).
	@echo "==> run: starting backend at $(BACKEND_URL) (web + worker via 'manage.py dev')..."
	@uv run python manage.py dev || (echo "ERROR: backend exited. See the [web]/[worker] log lines above."; exit 1)

setup: ensure check-llm check remind-browser ## One-command extension-ready setup (idempotent; no hand-run uv commands).
	@echo "==> setup: done. Start the backend with 'make run'."

clean: ## Remove the virtualenv and cached/build artifacts (keeps db.sqlite3, media/, .env).
	@echo "==> clean: removing virtualenv and caches..."
	@rm -rf "$(VENV)" __pycache__ .pytest_cache .coverage htmlcov .ruff_cache
	@find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null; true
	@find . -type f -name '*.py[cod]' -delete 2>/dev/null; true
	@echo "==> clean: done. Kept: db.sqlite3, huey.sqlite3, media/, .env (delete those by hand if you really want a fresh slate)."
