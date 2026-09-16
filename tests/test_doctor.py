"""Tests for the ``make doctor`` diagnostics helper (issue #136).

``doctor`` is a Makefile-only target (shell + stdlib python one-liners),
so these tests assert on the recipe text itself plus one live headless run:

- the target exists, is phony, and has ``make help`` text;
- every FAIL names its fix (exact commands / env vars from the issue);
- the recipe is strictly read-only: no ``--force``, no mint/write calls,
  no network, no browser launch;
- it does not duplicate ``make check``'s probes;
- a live ``make doctor`` run completes headless with per-check lines
  (exit 0 when all pass, non-zero when any FAIL -- both accepted here
  since the suite must stay green on machines with missing artifacts).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"


def _makefile_text() -> str:
    return MAKEFILE.read_text()


def _doctor_block() -> str:
    """The ``doctor:`` recipe block: from its target line to the next
    target line (or EOF)."""
    lines = _makefile_text().splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith("doctor:")
    )
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^[A-Za-z0-9_.-]+:", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def test_doctor_target_exists_phony_and_documented():
    text = _makefile_text()
    assert re.search(r"^doctor:.*##", text, re.M)
    phony = next(
        line for line in text.splitlines() if line.startswith(".PHONY:")
    )
    assert "doctor" in phony.split()


def test_doctor_emits_pass_fail_and_skip_lines():
    block = _doctor_block()
    assert "[PASS]" in block
    assert "[FAIL]" in block
    assert "[SKIP]" in block
    assert "not installed" in block


def test_doctor_fail_hints_name_exact_fixes():
    block = _doctor_block()
    # openssl check names the binary and the Xcode fix (same wording as
    # generate_signing_key).
    assert "openssl" in block
    assert "xcode-select --install" in block
    # key/token/wrapper/manifest hints name the exact commands.
    assert "uv run python manage.py generate_signing_key" in block
    assert "uv run python manage.py install_native_host" in block
    assert "extension_token --mint" in block
    # EXTENSION_ID hints cover the .env line and shell shadowing.
    assert "EXTENSION_ID" in block
    assert "RESTART_NOTE" in block or "read once at process start" in block
    assert "override=False" in block
    # LLM hint uses check-llm's own resolution wording.
    assert "api_key_env_var" in block
    assert "LLM_PROVIDER" in block
    # .env hint notes the .env.example bootstrap (via install_native_host's
    # own ENV_EXAMPLE_PATH constant).
    assert "ENV_EXAMPLE_PATH" in block


def test_doctor_reuses_detection_logic():
    block = _doctor_block()
    for name in (
        "read_manifest_key",
        "derive_extension_id",
        "PLACEHOLDER_KEY",
        "CANDIDATE_BROWSERS",
        "EXTENSION_TOKEN_FILE",
        "get_provider",
    ):
        assert name in block, name


def _assert_no_force_flag(text: str) -> None:
    """`--force` may only appear inside prose that forbids it ("never
    --force", "no --force anywhere", "--force regenerates ..."); it must
    never be *passed* as a flag to a command."""
    allowed = ("never --force", "no --force anywhere", "--force regenerates")
    for i, line in enumerate(text.splitlines()):
        if "--force" in line:
            assert any(a in line for a in allowed), f"line {i + 1}: {line}"


def test_doctor_is_read_only():
    block = _doctor_block()
    _assert_no_force_flag(block)
    for forbidden in (
        "set_key(",
        "mint_token(",
        "write_manifest(",
        "write_wrapper_script(",
        "write_private_key(",
    ):
        assert forbidden not in block, forbidden
    # No network probes and no browser launch.
    for forbidden in ("curl", "wget", "open -a", "manage.py dev", "runserver"):
        assert forbidden not in block, forbidden


def test_doctor_does_not_duplicate_make_check():
    block = _doctor_block()
    for probe in ("Anki", "DRAW_THINGS", "pgrep", "virtualenv"):
        assert probe not in block, probe


def test_doctor_never_passes_force_flag():
    # The whole Makefile must never *pass* --force to a command (doctor
    # only ever says "never --force"; the only legitimate --force mention
    # elsewhere is README/setup docs for generate_signing_key itself).
    _assert_no_force_flag(_makefile_text())


_LIVE_MARKS = (
    "openssl",
    "signing key pinned",
    "private key present",
    "extension token present",
    "wrapper present and executable",
    "native-messaging manifest",
    "EXTENSION_ID three-way match",
    "LLM key",
    ".env present",
)


@pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("uv") is None,
    reason="make doctor live run needs make + uv",
)
def test_doctor_live_run_completes_headless_with_per_check_lines():
    """Headless-safety proof: ``make doctor`` runs to completion with
    per-check lines and exits 0/1 (never crashes), launching no browser
    and making no network calls."""
    proc = subprocess.run(
        ["make", "doctor"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode in (0, 1), proc.stderr[-2000:] + proc.stdout[-2000:]
    combined = proc.stdout + proc.stderr
    assert "[PASS]" in combined or "[FAIL]" in combined
    for mark in _LIVE_MARKS:
        assert mark in combined, mark
    # No traceback: completion means diagnosed, not crashed.
    assert "Traceback" not in combined
