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

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from django.core.management.base import BaseCommand

WEB_PREFIX = "[web]"
WORKER_PREFIX = "[worker]"

#: Crash-loop guard: more than this many worker restarts inside this many
#: seconds means the worker is broken -> exit non-zero instead of looping.
MAX_WORKER_RESTARTS = 5
WORKER_RESTART_WINDOW = 60.0

#: Default backend origin (issue #47). Same env var name and same default
#: as native_host/host.py's DEFAULT_BACKEND_URL and config/settings.py's
#: BACKEND_URL - that shared name+default is what keeps the three in sync.
#: host.py stays stdlib-only so the parsing helper below is deliberately
#: duplicated there instead of imported; if the default ever changes,
#: update all three.
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"


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
        try:
            while not stop.is_set():
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
