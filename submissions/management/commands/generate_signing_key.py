"""Generate the extension's signing keypair via ``openssl`` (issue #51).

Replaces the manual two-``openssl``-command-plus-copy-paste step documented
in root ``README.md``'s "Browser extension setup" step 1 and in
``extension/README.md``: generates an RSA keypair with ``openssl``
(subprocess, no new dependency), writes the private key to a gitignored
dotfile at the project root (following ``.extension_token``'s convention),
and writes the public key's base64 encoding into ``extension/manifest.json``'s
``"key"`` field.

Refuses to overwrite a previously-generated real key unless ``--force`` is
given (detected by exact string match against the placeholder value that
ships in the repo - never by trying to parse/validate the existing value as
a key), matching ``extension_token.py``'s ``--force``/``CommandError``
pattern for the safety flag.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

#: This module's grandparent directory - the project root (contains manage.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

#: Private key output path: a gitignored dotfile at the project root,
#: following ``.extension_token``'s convention.
PRIVATE_KEY_PATH = PROJECT_ROOT / ".extension_signing_key.pem"

MANIFEST_PATH = PROJECT_ROOT / "extension" / "manifest.json"

PLACEHOLDER_KEY = "REPLACE_WITH_YOUR_OWN_OPENSSL_GENERATED_KEY"


def generate_keypair() -> tuple[str, str]:
    """Run ``openssl`` to generate an RSA keypair.

    Returns ``(private_key_pem, public_key_base64)``. Raises
    :class:`subprocess.CalledProcessError` if either ``openssl`` invocation
    fails.
    """
    genrsa = subprocess.run(
        ["openssl", "genrsa", "2048"],
        capture_output=True,
        check=True,
    )
    private_key_pem = genrsa.stdout

    pubout = subprocess.run(
        ["openssl", "rsa", "-pubout", "-outform", "DER"],
        input=private_key_pem,
        capture_output=True,
        check=True,
    )
    public_key_b64 = base64.b64encode(pubout.stdout).decode("ascii")

    return private_key_pem.decode("ascii"), public_key_b64


def write_private_key(path: Path, private_key_pem: str) -> None:
    """Atomically write *private_key_pem* to *path* with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(private_key_pem)
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_manifest(path: Path) -> dict:
    """Read and validate the extension manifest, raising ``CommandError``
    naming the specific problem and *path* if anything is wrong."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise CommandError(f"Extension manifest not found at {path}.")

    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommandError(f"Extension manifest at {path} is not valid JSON: {exc}")

    if "key" not in manifest:
        raise CommandError(f"Extension manifest at {path} has no \"key\" field.")

    return manifest


def write_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=2) + "\n")


class Command(BaseCommand):
    help = (
        "Generate a new RSA signing keypair for the browser extension via "
        "openssl, writing the private key outside the repo's tracked files "
        "and the public key's base64 encoding into "
        "extension/manifest.json's \"key\" field."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Regenerate the keypair even if manifest.json's \"key\" "
                "field already holds a real (non-placeholder) key, "
                "overwriting both the private key file and the public key."
            ),
        )

    def handle(self, *args, **options):
        force = options["force"]

        if shutil.which("openssl") is None:
            raise CommandError(
                "openssl was not found on PATH. Install it (e.g. via Xcode "
                "Command Line Tools: `xcode-select --install`) and re-run "
                "this command."
            )

        manifest = read_manifest(MANIFEST_PATH)

        current_key = manifest["key"]
        if current_key != PLACEHOLDER_KEY and not force:
            raise CommandError(
                f"{MANIFEST_PATH} already has a real signing key (not the "
                "placeholder); pass --force to regenerate and overwrite it."
            )

        try:
            private_key_pem, public_key_b64 = generate_keypair()
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            raise CommandError(f"openssl failed: {stderr or exc}")

        write_private_key(PRIVATE_KEY_PATH, private_key_pem)

        manifest["key"] = public_key_b64
        write_manifest(MANIFEST_PATH, manifest)

        self.stdout.write(f"Private key written to: {PRIVATE_KEY_PATH}")
        self.stdout.write(f"Public key written to: {MANIFEST_PATH}")
        self.stdout.write(
            "Chrome/Brave derives a new extension ID from a new key: "
            "reload the unpacked extension and re-run "
            "`install_native_host --extension-id <new-id>` with that new "
            "ID (see root README.md steps 2-3)."
        )
