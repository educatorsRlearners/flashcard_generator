"""Native messaging host: launch the backend on demand (issue #37).

Chrome launches this as a subprocess via ``chrome.runtime.connectNative``
and exchanges exactly one request/response with it over stdio, framed as a
4-byte little-endian length prefix followed by that many bytes of UTF-8
JSON. Given one request, this script:

* checks whether the Django backend answers at the configured
  ``BACKEND_URL`` (env var, issue #47; default ``http://127.0.0.1:8000/``);
* if not, spawns ``uv run python manage.py dev --addrport <addrport>``
  with the addrport derived from that same URL (detached, matching
  ``dev.py``'s own conventions) and polls until it does, or times out;
* reads (or mints, on first run) the extension auth token from #33; and
* replies with a single framed JSON message, then exits.

Stdlib only (``struct``, ``json``, ``subprocess``, ``urllib.request``,
``os``, ``sys``, ``time``, ``pathlib``) - this script is never imported by
``submissions``/``config``, only invoked by Chrome as a subprocess, so it
is not a dependency of the package ``pyproject.toml`` describes.

Nothing but the single framed reply is ever written to stdout - stdout is
the wire-protocol channel back to Chrome, so all logging goes to stderr.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# -- named constants (tunable without hunting for magic numbers) --------

#: Per-attempt timeout for a single readiness probe request.
PROBE_TIMEOUT_S = 1.0
#: How often to re-probe readiness while waiting for a spawned backend.
POLL_INTERVAL_S = 0.5
#: Total time to wait for the backend to become ready before giving up.
READY_TIMEOUT_S = 30.0
#: A lock file older than this is treated as abandoned (crashed holder).
#: READY_TIMEOUT_S + 5s margin.
STALE_LOCK_THRESHOLD_S = READY_TIMEOUT_S + 5.0

BACKEND_URL = "http://127.0.0.1:8000/"

#: Env var install_native_host bakes uv's install-time-resolved absolute
#: path into (issue #55) - written into run_host.sh alongside the
#: existing sys.executable-baking for the same reason: a GUI-launched
#: Chrome/Brave gives this process launchd's minimal PATH, which often
#: lacks user-local uv install locations (e.g. ~/.local/bin). Read once
#: per invocation via resolve_uv_binary() below; falls back to the bare
#: string "uv" (ordinary PATH lookup) when unset, e.g. when host.py is
#: run directly in dev/tests rather than via the wrapper script.
UV_ENV_VAR = "FLASHCARD_GENERATOR_UV"

#: Default backend origin when the BACKEND_URL env var is unset or blank
#: (issue #47). BACKEND_URL above is kept equal to this default for
#: backwards compatibility; runtime code must call resolve_backend_url()
#: (reads the env at call time) rather than the module constant, so an
#: override works without reimporting. config/settings.py and
#: submissions/management/commands/dev.py read the same env var name with
#: the same default - that shared name+default is what keeps them in sync.
DEFAULT_BACKEND_URL = BACKEND_URL


def resolve_backend_url(env: dict | None = None) -> str:
    """Return the configured backend origin (issue #47).

    Reads BACKEND_URL from *env* (default os.environ), falling back to
    DEFAULT_BACKEND_URL when unset or blank. Never raises; surrounding
    whitespace is stripped, trailing slash kept (callers strip it for the
    origin form in replies).
    """
    source = env if env is not None else os.environ
    raw = (source.get("BACKEND_URL", "") or "").strip()
    return raw or DEFAULT_BACKEND_URL


def resolve_uv_binary(env: dict | None = None) -> str:
    """Return the ``uv`` binary this process should invoke (issue #55).

    Reads UV_ENV_VAR from *env* (default os.environ) - the absolute path
    install_native_host baked into run_host.sh at install time, when a
    full user PATH was available. Falls back to the bare string "uv"
    (subprocess's own PATH lookup) when the env var is unset, e.g.
    host.py run directly rather than via the wrapper script.
    """
    source = env if env is not None else os.environ
    return (source.get(UV_ENV_VAR) or "").strip() or "uv"


def _uv_not_found_message(uv_binary: str) -> str:
    """Error text for a failed attempt to launch *uv_binary*.

    Distinguishes "never resolved" (bare "uv", ordinary PATH lookup
    failed) from "resolved once, now missing" (an absolute path baked in
    by install_native_host that no longer exists - moved/reinstalled/
    uninstalled since), so the popup names the actual problem instead of
    today's generic ``[Errno 2] No such file or directory: 'uv'``.
    """
    if uv_binary == "uv":
        return "uv not found on PATH"
    return (
        f"uv not found at {uv_binary} (it may have moved or been "
        "uninstalled - re-run install_native_host)"
    )


def backend_url_to_addrport(url: str) -> str:
    """Convert a backend origin URL to runserver ``addrport`` form.

    ``"http://127.0.0.1:9000/"`` -> ``"127.0.0.1:9000"``. A value with no
    ``://`` is already addrport form and is returned (slash-stripped) as
    is, so BACKEND_URL="127.0.0.1:9000" also works. stdlib only.
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

#: This script's grandparent directory - the directory containing manage.py.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOCK_FILENAME = ".native_host.lock"
LOG_FILENAME = ".native_host.log"

# Token file path (issue #33): settings.EXTENSION_TOKEN_FILE has no env
# override and is hardcoded in config/settings.py to
# BASE_DIR / ".extension_token" - duplicated here rather than importing
# Django. If that setting's default path is ever changed, update this too.
TOKEN_FILENAME = ".extension_token"


# -- errors ---------------------------------------------------------------


class BadRequest(Exception):
    """Stdin bytes were not a valid framed JSON message."""


class SpawnFailed(Exception):
    """``uv run python manage.py dev`` could not be spawned."""


class TokenUnavailable(Exception):
    """The token could not be read or minted."""


# -- wire protocol ----------------------------------------------------------


def _read_exact(stream, n: int) -> bytes | None:
    """Read exactly *n* bytes from *stream*, or None on EOF before that."""
    data = b""
    while len(data) < n:
        chunk = stream.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_message(stream) -> object:
    """Read one Chrome-native-messaging-framed JSON message from *stream*.

    Raises ``EOFError`` if stdin closes before a complete framed message
    arrives, or ``BadRequest`` if the framed bytes are not valid UTF-8 JSON.
    """
    raw_len = _read_exact(stream, 4)
    if raw_len is None:
        raise EOFError("stdin closed before a length prefix arrived")
    (length,) = struct.unpack("<I", raw_len)
    raw_body = _read_exact(stream, length)
    if raw_body is None:
        raise EOFError("stdin closed before the framed message body arrived")
    try:
        text = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadRequest(f"body is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadRequest(f"body is not valid JSON: {exc}") from exc


def pack_message(obj: object) -> bytes:
    """Frame *obj* as Chrome native-messaging bytes: 4-byte LE length + JSON."""
    payload = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def write_message(obj: object, stream) -> None:
    stream.write(pack_message(obj))
    stream.flush()


# -- reply payload shapes ---------------------------------------------------


def reply_already_running(token: str, base_url: str | None = None) -> dict:
    return {
        "ok": True,
        "already_running": True,
        "token": token,
        "base_url": (base_url or resolve_backend_url()).rstrip("/"),
    }


def reply_spawned(token: str, base_url: str | None = None) -> dict:
    return {
        "ok": True,
        "already_running": False,
        "token": token,
        "base_url": (base_url or resolve_backend_url()).rstrip("/"),
    }


def reply_error(error: str, detail: str) -> dict:
    return {"ok": False, "error": error, "detail": detail}


# -- readiness probe ----------------------------------------------------


def is_up(
    opener=urllib.request.urlopen,
    url: str | None = None,
    timeout: float = PROBE_TIMEOUT_S,
) -> bool:
    """True if *url* answers with any HTTP response within *timeout*."""
    target = url or resolve_backend_url()
    try:
        opener(target, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        # A real HTTP response (even an error status, e.g. this app's own
        # 404 at "/") - the backend is up, it just didn't return 2xx/3xx.
        # HTTPError is a URLError subclass, so it must be checked first.
        return True
    except urllib.error.URLError:
        return False
    except TimeoutError:
        return False


def poll_until_ready(
    is_up_fn,
    *,
    interval: float = POLL_INTERVAL_S,
    timeout: float = READY_TIMEOUT_S,
    sleep_fn=time.sleep,
    now_fn=time.monotonic,
) -> bool:
    """Call *is_up_fn* every *interval* seconds until True or *timeout* elapses."""
    start = now_fn()
    while True:
        if is_up_fn():
            return True
        if now_fn() - start >= timeout:
            return False
        sleep_fn(interval)


# -- child env ------------------------------------------------------------


def build_child_env(base_env: dict) -> dict:
    """A copy of *base_env* safe to spawn ``manage.py dev`` with.

    Strips ``PYTEST_CURRENT_TEST`` (which would make ``dev`` silently
    refuse to run) and ``HUEY_IMMEDIATE`` (which would make it print a
    warning and bypass the consumer) - matching ``dev.py``'s own
    ``child_env()`` convention, one level up.
    """
    env = dict(base_env)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("HUEY_IMMEDIATE", None)
    return env


# -- lock file (double-connectNative race guard) ---------------------------


def acquire_lock(
    lock_path: Path,
    *,
    stale_threshold: float = STALE_LOCK_THRESHOLD_S,
    now_fn=time.time,
) -> bool:
    """Try to atomically claim *lock_path*.

    Returns True if this call created it (caller should spawn). Returns
    False if another invocation already holds a fresh lock (caller should
    not spawn, just piggyback on the readiness poll). A stale lock (older
    than *stale_threshold*) is treated as abandoned and cleared before
    retrying once.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            mtime = lock_path.stat().st_mtime
        except FileNotFoundError:
            # Removed between our open() failing and our stat() - retry once.
            return acquire_lock(
                lock_path, stale_threshold=stale_threshold, now_fn=now_fn
            )
        if now_fn() - mtime > stale_threshold:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            return acquire_lock(
                lock_path, stale_threshold=stale_threshold, now_fn=now_fn
            )
        return False


def release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


# -- spawning manage.py dev -------------------------------------------------


def spawn_backend(
    project_root: Path,
    log_path: Path,
    addrport: str | None = None,
) -> subprocess.Popen:
    """Spawn ``uv run python manage.py dev --addrport <addrport>``.

    *addrport* defaults to the addrport derived from the configured
    BACKEND_URL (issue #47), so the spawned backend always listens where
    the readiness probe looks - no drift. Detached, logging to *log_path*.

    Raises ``SpawnFailed`` if ``manage.py`` is missing (misplaced script)
    or the subprocess could not be started at all (e.g. ``uv`` not on
    ``PATH``).
    """
    manage_py = project_root / "manage.py"
    if not manage_py.exists():
        raise SpawnFailed(f"manage.py not found at {manage_py}")

    target_addrport = addrport or backend_url_to_addrport(resolve_backend_url())
    env = build_child_env(os.environ)
    uv_binary = resolve_uv_binary(env)
    log_file = open(log_path, "a")
    try:
        return subprocess.Popen(
            [uv_binary, "run", "python", "manage.py", "dev", "--addrport", target_addrport],
            cwd=str(project_root),
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,  # detach, matching dev.py's own children
        )
    except FileNotFoundError as exc:
        raise SpawnFailed(_uv_not_found_message(uv_binary)) from exc
    except OSError as exc:
        raise SpawnFailed(str(exc)) from exc
    finally:
        log_file.close()  # the child holds its own dup'd fd after Popen returns


# -- token (issue #33) -------------------------------------------------


def get_token(project_root: Path) -> str:
    """Read the extension token, minting it via manage.py on first run.

    Raises ``TokenUnavailable`` if the mint subprocess fails or produces
    no token.
    """
    token_path = project_root / TOKEN_FILENAME
    if token_path.exists():
        return token_path.read_text().strip()

    uv_binary = resolve_uv_binary()
    try:
        result = subprocess.run(
            [uv_binary, "run", "python", "manage.py", "extension_token", "--mint"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=READY_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise TokenUnavailable(_uv_not_found_message(uv_binary)) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TokenUnavailable(f"minting subprocess failed: {exc}") from exc

    if result.returncode != 0:
        raise TokenUnavailable(
            f"manage.py extension_token --mint exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    token = result.stdout.strip()
    if not token:
        raise TokenUnavailable("manage.py extension_token --mint produced no token")
    return token


# -- request handling -----------------------------------------------------


def handle_request(
    project_root: Path = PROJECT_ROOT,
    opener=urllib.request.urlopen,
    url: str | None = None,
) -> dict:
    """Handle one request end-to-end and return the reply payload dict.

    *url* defaults to the configured BACKEND_URL (issue #47). Mismatch
    policy: the configured URL always wins. If it answers, the reply is
    already_running for it - a backend a user started by hand on some
    other port is ignored, not adopted. If it is down, a backend is
    spawned with the matching --addrport, even if another port answers.
    """
    lock_path = project_root / LOCK_FILENAME
    log_path = project_root / LOG_FILENAME
    target = url or resolve_backend_url()

    def probe() -> bool:
        return is_up(opener, target)

    try:
        # Fast path: already up -> no polling loop, no subprocess.
        if probe():
            return reply_already_running(get_token(project_root), target)

        # Re-probe immediately before spawning - closes most (not all) of
        # the double-connectNative race.
        if probe():
            return reply_already_running(get_token(project_root), target)

        acquired = acquire_lock(lock_path)
        spawn_error: SpawnFailed | None = None
        try:
            if acquired:
                try:
                    spawn_backend(
                        project_root,
                        log_path,
                        backend_url_to_addrport(target),
                    )
                except SpawnFailed as exc:
                    spawn_error = exc
            if spawn_error is None:
                ready = poll_until_ready(probe)
            else:
                ready = False
        finally:
            # Release only once the readiness poll ends (success, timeout,
            # or spawn failure) - not right after spawning - so a second
            # invocation arriving mid-startup waits instead of re-spawning.
            if acquired:
                release_lock(lock_path)

        if spawn_error is not None:
            return reply_error("spawn_failed", str(spawn_error))
        if not ready:
            return reply_error(
                "timeout",
                f"backend did not become ready within {READY_TIMEOUT_S:.0f}s",
            )
        return reply_spawned(get_token(project_root), target)
    except TokenUnavailable as exc:
        return reply_error("token_unavailable", str(exc))


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    try:
        read_message(stdin)  # content is ignored for v1; any value triggers it
    except EOFError:
        return 0
    except BadRequest as exc:
        write_message(reply_error("bad_request", str(exc)), stdout)
        return 0

    try:
        reply = handle_request()
    except Exception as exc:  # never let a traceback hit stdout
        print(f"native_host.host: unexpected error: {exc!r}", file=sys.stderr)
        return 0

    write_message(reply, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
