"""Dev supervisor: runserver + Huey consumer together (issue #20).

Development-only convenience: spawns ``runserver`` and ``run_huey`` as
child processes, prefixes their output (``[web]`` / ``[worker]``), and
supervises them:

* Ctrl-C (SIGINT/SIGTERM) terminates both children; no orphans.
* A crashed web server stops everything with a visible non-zero exit.
* A crashed consumer is restarted automatically (visible log line); if it
  crash-loops (too many restarts in a short window) the whole command
  exits non-zero instead of silently continuing with a dead worker.

Batch progress streaming (issue #23, SSE via plain Django
``StreamingHttpResponse``) needs nothing extra: the stream is served by
the ``runserver`` child itself. It does need ``runserver``'s default
threaded mode — do NOT pass ``--nothreading``, which would serialize the
long-lived ``/events/`` connection against normal requests. ``dev``
intentionally starts plain ``runserver`` (threaded by default) plus the
consumer, which is everything push needs.

Stdlib only (``subprocess`` + threads). Never used by production/WSGI or
by the test suite (Huey immediate mode covers tests; see
``tests/conftest.py``).
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor

WEB_PREFIX = "[web]"
WORKER_PREFIX = "[worker]"
ENV_PREFIX = "[env]"
MIGRATIONS_PREFIX = "[migrations]"

#: Crash-loop guard: more than this many worker restarts inside this many
#: seconds means the worker is broken -> exit non-zero instead of looping.
MAX_WORKER_RESTARTS = 5
WORKER_RESTART_WINDOW = 60.0

#: How often (seconds), while children are already running, the supervision
#: loop rechecks the DB for newly-pending migrations (issue #81). Gated by
#: elapsed time against the loop's existing ~0.2s tick, same pattern as the
#: .env content-hash check (issue #54) - not a separate thread or a
#: filesystem watch on migration files.
MIGRATION_CHECK_INTERVAL = 5.0

#: Default backend origin (issue #47). Same env var name and same default
#: as native_host/host.py's DEFAULT_BACKEND_URL and config/settings.py's
#: BACKEND_URL - that shared name+default is what keeps the three in sync.
#: host.py stays stdlib-only so the parsing helper below is deliberately
#: duplicated there instead of imported; if the default ever changes,
#: update all three.
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"

#: Sentinel for "no .env baseline established yet" (issue #54) - distinct
#: from the real ``None`` hash_env_file() returns for "no .env file".
_UNSET = object()


def backend_url_to_addrport(url: str) -> str:
    """Convert a backend origin URL to runserver ``addrport`` form.

    ``"http://127.0.0.1:9000/"`` -> ``"127.0.0.1:9000"``. Mirrors
    native_host/host.py's helper (duplicated: host.py is stdlib-only).
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return backend_url_to_addrport(DEFAULT_BACKEND_URL)
    if "://" not in u:
        return u
    parts = urllib.parse.urlparse(u)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    return f"{host}:{port}"


def default_addrport(env: dict | None = None) -> str:
    """Addrport derived from the BACKEND_URL env var (issue #47)."""
    source = env if env is not None else os.environ
    raw = (source.get("BACKEND_URL", "") or "").strip()
    return backend_url_to_addrport(raw or DEFAULT_BACKEND_URL)


def env_path() -> Path:
    """Path to the project's ``.env`` file (issue #54)."""
    return Path(__file__).resolve().parents[3] / ".env"


def hash_env_file(path: Path | None = None) -> str | None:
    """Content hash of ``.env``, or ``None`` if it doesn't exist (issue #54).

    Hash-based (not mtime), so a ``touch`` with unchanged content never
    triggers a restart. ``None`` is a sentinel distinct from any hash, so
    ``.env`` being created after ``dev`` started, or deleted while it's
    running, both count as "changed" relative to whatever the baseline was.
    """
    p = path if path is not None else env_path()
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


def env_file_vars(path: Path | None = None) -> set[str]:
    """Variable names ``.env`` currently defines (issue #54).

    A minimal stdlib-only parse (no quoting/escaping edge cases) - just
    enough to warn about a name also being set as a real shell env var;
    not a general-purpose ``.env`` parser.
    """
    p = path if path is not None else env_path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        return set()
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, _value = line.partition("=")
        key = key.strip()
        if sep and key:
            names.add(key)
    return names


def format_shell_override_warning(names: list[str]) -> str:
    """Warning that a restart won't actually pick up new .env value(s) (issue #54).

    Mirrors the warning ``install_native_host`` prints (#52): ``.env``'s
    var(s) are also set as real shell environment variables, and
    ``config/settings.py`` calls ``load_dotenv(..., override=False)``, so
    the shell value keeps winning in the restarted children too.
    """
    joined = ", ".join(sorted(names))
    return (
        f"Warning: {joined} also set as a real environment variable in "
        "this shell - that takes precedence over the new value in .env "
        "(config/settings.py calls load_dotenv with its default "
        "override=False), so the restarted children still won't see the "
        "new .env value until you unset or update the shell variable too."
    )


def pending_migrations(alias: str = DEFAULT_DB_ALIAS) -> list:
    """Migration plan Django would apply for *alias* right now (issue #79).

    Same executor + ``migration_plan(leaf_nodes())`` call ``manage.py
    migrate --check`` itself uses, so this reflects what an actual
    ``migrate`` would do - not ``makemigrations --check``, which answers a
    different question (model changes with no migration file yet; out of
    scope here). Covers every installed app, not just ``submissions``.
    """
    connection = connections[alias]
    connection.prepare_database()
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    return executor.migration_plan(targets)


def format_pending_migrations(plan: list) -> str:
    """Human-readable, impossible-to-miss message naming the pending migrations."""
    names = ", ".join(f"{migration.app_label}.{migration.name}" for migration, _backwards in plan)
    return (
        "UNAPPLIED MIGRATIONS: "
        f"{names}. Run `uv run python manage.py migrate` before `dev`."
    )


def migration_plan_key(plan: list) -> list:
    """Comparable snapshot of a migration plan (issue #81).

    ``Migration`` objects don't define ``__eq__``, and a fresh
    ``MigrationExecutor`` is built on every ``pending_migrations()`` call, so
    two calls returning "the same" plan never compare equal by identity.
    This reduces a plan to plain, comparable ``(app_label, name)`` pairs so
    the periodic recheck can tell "unchanged" from "changed" plans.
    """
    return [(migration.app_label, migration.name) for migration, _backwards in plan]


def project_manage_py() -> str:
    """Absolute path to manage.py (children are spawned via this file)."""
    return str(Path(__file__).resolve().parents[3] / "manage.py")


def build_web_cmd(addrport: str) -> list[str]:
    return [sys.executable, project_manage_py(), "runserver", addrport]


def build_worker_cmd(extra_args: list[str] | None = None) -> list[str]:
    return [sys.executable, project_manage_py(), "run_huey", *(extra_args or [])]


def child_env() -> dict[str, str]:
    """Env for children: force real queue mode (never HUEY immediate)."""
    env = dict(os.environ)
    # HUEY_IMMEDIATE=1 would make the web process run tasks inline and the
    # consumer pointless; dev mode always wants a live consumer.
    env.pop("HUEY_IMMEDIATE", None)
    # Unbuffered child output so prefixed lines appear promptly.
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def prune_restarts(timestamps: list[float], now: float, window: float) -> list[float]:
    """Drop restart timestamps older than *window*; helper kept separate for tests."""
    return [t for t in timestamps if now - t < window]


class Command(BaseCommand):
    help = (
        "Development-only: run the Django dev server and the Huey consumer "
        "together with prefixed logs. Ctrl-C stops both. "
        "Stdlib only; never used in production or tests. "
        "The batch progress SSE stream (issue #23) is served by runserver "
        "itself (threaded by default; do not add --nothreading)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--addrport",
            default=None,
            help=(
                "Address:port for runserver (default: addrport derived from "
                "the BACKEND_URL env var, i.e. 127.0.0.1:8000 unless "
                "overridden). An explicit --addrport always wins over the "
                "env var."
            ),
        )
        parser.add_argument(
            "--huey-args",
            default="",
            help=(
                "Extra space-separated args appended to run_huey "
                '(e.g. --huey-args="--workers 2").'
            ),
        )
        parser.add_argument(
            "--no-restart-worker",
            action="store_true",
            help="Exit non-zero on the first worker crash instead of restarting it.",
        )

    def handle(self, *args, **options):
        if os.environ.get("PYTEST_CURRENT_TEST"):
            self.stderr.write(
                "Refusing to supervise runserver+run_huey under pytest "
                "(tests use Huey immediate mode)."
            )
            return
        addrport = options["addrport"] or default_addrport()
        huey_args = (options["huey_args"] or "").split()
        code = self.run_supervised(
            addrport,
            huey_args,
            restart_worker=not options["no_restart_worker"],
        )
        if code:
            raise SystemExit(code)

    # -- supervision ----------------------------------------------------
    def run_supervised(
        self, addrport: str, huey_args: list[str], restart_worker: bool = True
    ) -> int:
        """Spawn both children, stream prefixed logs, supervise. Returns exit code."""
        plan = pending_migrations()
        if plan:
            self.stderr.write(format_pending_migrations(plan))
            return 1
        if os.environ.get("HUEY_IMMEDIATE", "") == "1":
            self.stdout.write(
                "WARNING: HUEY_IMMEDIATE=1 is set; unsetting it for dev children "
                "so the consumer does real work."
            )
        web_cmd = build_web_cmd(addrport)
        worker_cmd = build_worker_cmd(huey_args)
        env = child_env()

        self.stdout.write(f"Starting web server: {' '.join(web_cmd)}")
        self.stdout.write(f"Starting Huey consumer: {' '.join(worker_cmd)}")

        web = self._spawn(web_cmd, env)
        worker = self._spawn(worker_cmd, env)
        self._pump(web, WEB_PREFIX)
        self._pump(worker, WORKER_PREFIX)

        stop = threading.Event()
        old_sigint = signal.getsignal(signal.SIGINT)
        old_sigterm = signal.getsignal(signal.SIGTERM)

        def _on_signal(signum, _frame):
            stop.set()

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        restarts: list[float] = []
        env_hash: str | None | object = _UNSET
        # Baseline set only once children are running, so the periodic
        # recheck never fires on tick 0 (issue #81); the startup check above
        # already covers "pending before anything starts".
        last_migration_check = time.monotonic()
        last_warned_plan: list | None = None
        try:
            while not stop.is_set():
                now_m = time.monotonic()
                if not stop.is_set() and now_m - last_migration_check >= MIGRATION_CHECK_INTERVAL:
                    last_migration_check = now_m
                    recheck_plan = pending_migrations()
                    if stop.is_set():
                        # Shutdown wins: never print/act on a recheck that
                        # lands during Ctrl-C/SIGTERM teardown.
                        break
                    plan_key = migration_plan_key(recheck_plan)
                    if plan_key and plan_key != last_warned_plan:
                        last_warned_plan = plan_key
                        self.stdout.write(
                            f"{MIGRATIONS_PREFIX} "
                            f"{format_pending_migrations(recheck_plan)}"
                        )
                    elif not plan_key and last_warned_plan is not None:
                        last_warned_plan = None
                        self.stdout.write(
                            f"{MIGRATIONS_PREFIX} Pending migrations resolved; "
                            "previously-warned migrations are now applied."
                        )
                current_env_hash = hash_env_file()
                if env_hash is _UNSET:
                    # First tick establishes the baseline; never restart on it.
                    env_hash = current_env_hash
                elif current_env_hash != env_hash:
                    env_hash = current_env_hash
                    if stop.is_set():
                        # Ctrl-C/SIGTERM wins over a pending env restart.
                        break
                    self.stdout.write(
                        f"{ENV_PREFIX} .env changed; restarting web server "
                        "and worker."
                    )
                    shadowed = sorted(
                        name for name in env_file_vars() if os.environ.get(name)
                    )
                    if shadowed:
                        self.stdout.write(
                            f"{ENV_PREFIX} " + format_shell_override_warning(shadowed)
                        )
                    self._terminate(web)
                    self._terminate(worker)
                    web = self._spawn(web_cmd, env)
                    worker = self._spawn(worker_cmd, env)
                    self._pump(web, WEB_PREFIX)
                    self._pump(worker, WORKER_PREFIX)
                    # Env-triggered restarts are intentional, not crashes -
                    # they must never consume the crash-loop budget below.
                    time.sleep(0.2)
                    continue
                web_rc = web.poll()
                worker_rc = worker.poll()
                if web_rc is not None:
                    self.stderr.write(
                        f"{WEB_PREFIX} exited with code {web_rc}; stopping worker."
                    )
                    self._terminate(worker)
                    return web_rc or 1
                if worker_rc is not None:
                    now = time.monotonic()
                    restarts = prune_restarts(restarts, now, WORKER_RESTART_WINDOW)
                    restarts.append(now)
                    if not restart_worker:
                        self.stderr.write(
                            f"{WORKER_PREFIX} exited with code {worker_rc}; "
                            "not restarting (--no-restart-worker). Stopping web server."
                        )
                        self._terminate(web)
                        return worker_rc or 1
                    if len(restarts) > MAX_WORKER_RESTARTS:
                        self.stderr.write(
                            f"{WORKER_PREFIX} crashed {len(restarts)} times in "
                            f"{WORKER_RESTART_WINDOW:.0f}s (crash loop); "
                            "stopping web server and exiting 1."
                        )
                        self._terminate(web)
                        return 1
                    self.stderr.write(
                        f"{WORKER_PREFIX} exited with code {worker_rc}; "
                        f"restarting ({len(restarts)}/{MAX_WORKER_RESTARTS})."
                    )
                    worker = self._spawn(worker_cmd, env)
                    self._pump(worker, WORKER_PREFIX)
                time.sleep(0.2)
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGTERM, old_sigterm)
            self._terminate(web)
            self._terminate(worker)

    # -- process helpers ------------------------------------------------
    def _spawn(self, cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,  # own process group -> clean kill, no orphans
        )

    def _pump(self, proc: subprocess.Popen, prefix: str) -> threading.Thread:
        """Stream a child's merged stdout/stderr to our stdout with a prefix."""

        def _reader():
            assert proc.stdout is not None
            for line in proc.stdout:
                self.stdout.write(f"{prefix} {line.rstrip()}")
            # readline loop ends on EOF (child exited); thread dies silently.

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        return thread

    def _terminate(self, proc: subprocess.Popen) -> None:
        """Kill a whole child process group; fall back to terminate()."""
        if proc.poll() is not None:
            return
        try:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                proc.wait(timeout=10)
        except Exception:  # never raise from cleanup
            pass
