"""Install the native-messaging-host manifest for Chrome/Brave (issue #38).

Writes a launcher wrapper script (``native_host/run_host.sh``) with an
absolute interpreter path and an absolute ``uv`` path (issue #55) baked
in, plus a native-messaging-host manifest
(``com.flashcard_generator.native_host.json``) naming the extension allowed
to connect, into whichever of Chrome's/Brave's native-messaging-host
directories are present on this machine (macOS only - see #43 for
Linux/Windows). Also mints the extension auth token (#33) on first run, and
writes ``EXTENSION_ID=<id>`` into ``.env`` (#52) so the backend's CORS
allowlist matches the loaded extension without any hand-editing - a fresh
checkout is fully ready after this one command.

Re-running is idempotent: the wrapper and both manifest files are
overwritten in place with the new ``--extension-id``, never left alongside
stale copies under a different name, and the ``.env`` ``EXTENSION_ID=``
line is updated in place rather than duplicated.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dotenv import set_key

from submissions.extension_auth import mint_token

#: This module's grandparent directory - the project root (contains manage.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

NATIVE_HOST_DIR = PROJECT_ROOT / "native_host"
HOST_SCRIPT_PATH = NATIVE_HOST_DIR / "host.py"
WRAPPER_SCRIPT_PATH = NATIVE_HOST_DIR / "run_host.sh"

ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"

RESTART_NOTE = (
    "Restart any already-running backend process (manage.py dev, or "
    "runserver/run_huey started manually) for the new EXTENSION_ID to take "
    "effect - config/settings.py reads .env once at process start."
)

#: Env var run_host.sh exports before exec'ing host.py, carrying uv's
#: install-time-resolved absolute path (issue #55). Duplicated here
#: rather than imported - host.py is stdlib-only and never imported by
#: submissions/config (see its module docstring) - so if this name ever
#: changes, update native_host/host.py's UV_ENV_VAR to match.
UV_ENV_VAR = "FLASHCARD_GENERATOR_UV"

MANIFEST_NAME = "com.flashcard_generator.native_host"
MANIFEST_FILENAME = f"{MANIFEST_NAME}.json"

#: (browser label, browser's own directory, its NativeMessagingHosts dir).
#: The browser directory's existence is the evidence used to decide whether
#: that browser is installed; NativeMessagingHosts/ is created under it
#: on demand, never the browser directory itself.
_APP_SUPPORT = Path.home() / "Library" / "Application Support"
CANDIDATE_BROWSERS = [
    (
        "Chrome",
        _APP_SUPPORT / "Google" / "Chrome",
        _APP_SUPPORT / "Google" / "Chrome" / "NativeMessagingHosts",
    ),
    (
        "Brave",
        _APP_SUPPORT / "BraveSoftware" / "Brave-Browser",
        _APP_SUPPORT / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts",
    ),
]


def resolve_uv_binary() -> str:
    """Resolve ``uv``'s absolute path via the current PATH (issue #55).

    Called at ``install_native_host`` run time, when a full user PATH
    (the invoking shell's) is available - unlike host.py's runtime PATH
    when launched by Brave/Chrome, which is launchd's minimal GUI-app
    PATH and often lacks user-local uv install locations (e.g.
    ``~/.local/bin``, installed by the astral.sh installer per this
    repo's README).

    If more than one ``uv`` is on PATH (e.g. a Homebrew install and an
    astral.sh ``~/.local/bin`` install), whichever ``shutil.which("uv")``
    returns first per PATH order is the one baked in - a deliberate
    choice, not left implicit; no further disambiguation is added.

    Returns None if ``uv`` is not found anywhere on PATH.
    """
    return shutil.which("uv")


def wrapper_script_body(python_executable: str, host_script: Path, uv_binary: str) -> str:
    """The exact ``run_host.sh`` contents: exec the interpreter on host.py.

    Exports *uv_binary* (uv's install-time-resolved absolute path, issue
    #55) as UV_ENV_VAR before the exec line, so host.py never has to
    guess uv's location from the browser's stripped-down runtime PATH.
    """
    return (
        f"#!/bin/sh\n"
        f"export {UV_ENV_VAR}={uv_binary}\n"
        f'exec {python_executable} {host_script} "$@"\n'
    )


def manifest_contents(wrapper_path: Path, extension_id: str) -> dict:
    """The native-messaging-host manifest dict for *extension_id*."""
    return {
        "name": MANIFEST_NAME,
        "description": "Flashcard Generator native messaging host",
        "path": str(wrapper_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def write_wrapper_script(
    path: Path, python_executable: str, host_script: Path, uv_binary: str
) -> None:
    """Write the wrapper script at *path* and make it executable by owner."""
    path.write_text(wrapper_script_body(python_executable, host_script, uv_binary))
    # Ensure the owner-executable bit is set regardless of the file's prior
    # mode (umask on creation, or a leftover mode from a previous run).
    path.chmod(path.stat().st_mode | stat.S_IRWXU)


def write_manifest(directory: Path, contents: dict) -> Path:
    """Create *directory* if needed and write the manifest into it.

    Overwrites any existing manifest at that exact path in place.
    """
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(contents, indent=2) + "\n")
    return manifest_path


def set_extension_id_in_env(extension_id: str) -> None:
    """Add/update ``EXTENSION_ID=<extension_id>`` in the ``.env`` at ``ENV_PATH``.

    If ``ENV_PATH`` doesn't exist yet, it's created first from
    ``ENV_EXAMPLE_PATH``'s contents (or empty, if that template is itself
    missing). Uses ``python-dotenv``'s ``set_key`` so any other
    variables/comments/ordering already in the file survive untouched, and
    re-running updates the existing ``EXTENSION_ID=`` line in place rather
    than appending a second one.

    Reads ``ENV_PATH``/``ENV_EXAMPLE_PATH`` as module globals (rather than
    default-argument values) so tests can ``monkeypatch`` them per-case.
    """
    if not ENV_PATH.exists():
        if ENV_EXAMPLE_PATH.exists():
            ENV_PATH.write_text(ENV_EXAMPLE_PATH.read_text())
        else:
            ENV_PATH.touch()
    set_key(str(ENV_PATH), "EXTENSION_ID", extension_id, quote_mode="never")


class Command(BaseCommand):
    help = (
        "Write the native-messaging-host manifest and launcher wrapper so "
        "Chrome/Brave can discover and run native_host/host.py, and mint "
        "the extension auth token if one doesn't exist yet."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--extension-id",
            required=True,
            help=(
                "The extension ID Chrome/Brave assigned when the unpacked "
                "extension was loaded (chrome://extensions)."
            ),
        )

    def handle(self, *args, **options):
        extension_id = options["extension_id"]

        candidates = [
            (label, native_dir)
            for label, browser_dir, native_dir in CANDIDATE_BROWSERS
            if browser_dir.exists()
        ]
        skipped = [
            (label, browser_dir)
            for label, browser_dir, _native_dir in CANDIDATE_BROWSERS
            if not browser_dir.exists()
        ]

        if not candidates:
            checked = ", ".join(
                f"{label} ({browser_dir})" for label, browser_dir in skipped
            )
            raise CommandError(
                "Neither Chrome nor Brave appears to be installed - none of "
                f"the following directories exist: {checked}. Install "
                "Chrome or Brave, then re-run this command."
            )

        uv_binary = resolve_uv_binary()
        if uv_binary is None:
            raise CommandError(
                "uv not found on PATH. Install it (see README) or ensure "
                "it's on PATH, then re-run this command."
            )

        write_wrapper_script(WRAPPER_SCRIPT_PATH, sys.executable, HOST_SCRIPT_PATH, uv_binary)

        contents = manifest_contents(WRAPPER_SCRIPT_PATH, extension_id)
        written = [
            (label, write_manifest(native_dir, contents))
            for label, native_dir in candidates
        ]

        if not settings.EXTENSION_TOKEN_FILE.exists():
            try:
                mint_token(force=False)
            except FileExistsError:
                pass  # minted concurrently between the exists() check and here

        set_extension_id_in_env(extension_id)

        self.stdout.write(f"Extension ID registered: {extension_id}")
        self.stdout.write(f"Wrapper script written: {WRAPPER_SCRIPT_PATH}")
        for label, manifest_path in written:
            self.stdout.write(f"{label} manifest written: {manifest_path}")
        for label, browser_dir in skipped:
            self.stdout.write(
                f"{label} not found ({browser_dir}) - skipped."
            )
        self.stdout.write(f"EXTENSION_ID set in .env: {extension_id}")
        self.stdout.write(RESTART_NOTE)
        if os.environ.get("EXTENSION_ID"):
            self.stdout.write(
                "Warning: EXTENSION_ID is also set as a real environment "
                "variable in this shell - that takes precedence over the "
                "value just written to .env (config/settings.py calls "
                "load_dotenv with its default override=False), so unset it "
                "or update it too for the new ID to actually take effect."
            )
