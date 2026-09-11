"""Install the native-messaging-host manifest for Chrome/Brave (issue #38).

Writes a launcher wrapper script (``native_host/run_host.sh``) with an
absolute interpreter path baked in, plus a native-messaging-host manifest
(``com.flashcard_generator.native_host.json``) naming the extension allowed
to connect, into whichever of Chrome's/Brave's native-messaging-host
directories are present on this machine (macOS only - see #43 for
Linux/Windows). Also mints the extension auth token (#33) on first run, so
a fresh checkout is fully ready after this one command.

Re-running is idempotent: the wrapper and both manifest files are
overwritten in place with the new ``--extension-id``, never left alongside
stale copies under a different name.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from submissions.extension_auth import mint_token

#: This module's grandparent directory - the project root (contains manage.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

NATIVE_HOST_DIR = PROJECT_ROOT / "native_host"
HOST_SCRIPT_PATH = NATIVE_HOST_DIR / "host.py"
WRAPPER_SCRIPT_PATH = NATIVE_HOST_DIR / "run_host.sh"

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


def wrapper_script_body(python_executable: str, host_script: Path) -> str:
    """The exact ``run_host.sh`` contents: exec the interpreter on host.py."""
    return f'#!/bin/sh\nexec {python_executable} {host_script} "$@"\n'


def manifest_contents(wrapper_path: Path, extension_id: str) -> dict:
    """The native-messaging-host manifest dict for *extension_id*."""
    return {
        "name": MANIFEST_NAME,
        "description": "Flashcard Generator native messaging host",
        "path": str(wrapper_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def write_wrapper_script(path: Path, python_executable: str, host_script: Path) -> None:
    """Write the wrapper script at *path* and make it executable by owner."""
    path.write_text(wrapper_script_body(python_executable, host_script))
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

        write_wrapper_script(WRAPPER_SCRIPT_PATH, sys.executable, HOST_SCRIPT_PATH)

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

        self.stdout.write(f"Extension ID registered: {extension_id}")
        self.stdout.write(f"Wrapper script written: {WRAPPER_SCRIPT_PATH}")
        for label, manifest_path in written:
            self.stdout.write(f"{label} manifest written: {manifest_path}")
        for label, browser_dir in skipped:
            self.stdout.write(
                f"{label} not found ({browser_dir}) - skipped."
            )
