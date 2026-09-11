"""Tests for the native-messaging-host installer command (issue #38).

Covers the unit-testable pieces per the issue's testing split: manifest and
wrapper-script content generation, directory-existence branching, and
idempotent overwrite behaviour - all against faked/temporary directories,
never real ``~/Library/Application Support/...`` paths. Actual discovery
and launch by a real Chrome/Brave install is manual-verification-only (see
the PR description).
"""

from __future__ import annotations

import io
import json
import stat

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from submissions.extension_auth import mint_token
from submissions.management.commands import install_native_host as cmd

EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"


@pytest.fixture(autouse=True)
def token_file(tmp_path, settings):
    """Point the token file at a tmp path so the suite never touches a real one."""
    settings.EXTENSION_TOKEN_FILE = tmp_path / ".extension_token"


@pytest.fixture
def native_host_dir(tmp_path, monkeypatch):
    """Fake project layout: <tmp>/native_host/{host.py,run_host.sh}."""
    directory = tmp_path / "native_host"
    directory.mkdir()
    host_script = directory / "host.py"
    host_script.write_text("# fake host.py\n")
    wrapper_script = directory / "run_host.sh"
    monkeypatch.setattr(cmd, "NATIVE_HOST_DIR", directory)
    monkeypatch.setattr(cmd, "HOST_SCRIPT_PATH", host_script)
    monkeypatch.setattr(cmd, "WRAPPER_SCRIPT_PATH", wrapper_script)
    return directory


@pytest.fixture
def browser_dirs(tmp_path, monkeypatch):
    """Fake Chrome + Brave app-support directories, both present."""
    chrome_dir = tmp_path / "Chrome"
    chrome_dir.mkdir()
    brave_dir = tmp_path / "Brave-Browser"
    brave_dir.mkdir()
    candidates = [
        ("Chrome", chrome_dir, chrome_dir / "NativeMessagingHosts"),
        ("Brave", brave_dir, brave_dir / "NativeMessagingHosts"),
    ]
    monkeypatch.setattr(cmd, "CANDIDATE_BROWSERS", candidates)
    return {"Chrome": chrome_dir, "Brave": brave_dir}


# -- pure content-generation helpers ---------------------------------------


def test_wrapper_script_body_shape():
    body = cmd.wrapper_script_body("/path/to/python3", cmd.Path("/proj/native_host/host.py"))
    assert body == '#!/bin/sh\nexec /path/to/python3 /proj/native_host/host.py "$@"\n'


def test_manifest_contents_shape():
    contents = cmd.manifest_contents(cmd.Path("/proj/native_host/run_host.sh"), EXTENSION_ID)
    assert contents == {
        "name": "com.flashcard_generator.native_host",
        "description": "Flashcard Generator native messaging host",
        "path": "/proj/native_host/run_host.sh",
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }


# -- command behaviour ------------------------------------------------------


@pytest.mark.django_db
def test_requires_extension_id_flag():
    with pytest.raises(CommandError):
        call_command("install_native_host")


@pytest.mark.django_db
def test_writes_wrapper_script_executable(native_host_dir, browser_dirs):
    call_command("install_native_host", "--extension-id", EXTENSION_ID)

    wrapper = native_host_dir / "run_host.sh"
    assert wrapper.exists()
    body = wrapper.read_text()
    assert body.startswith("#!/bin/sh\n")
    assert "exec " in body
    assert str(native_host_dir / "host.py") in body
    mode = wrapper.stat().st_mode
    assert mode & stat.S_IXUSR


@pytest.mark.django_db
def test_writes_manifest_to_both_browsers(native_host_dir, browser_dirs):
    call_command("install_native_host", "--extension-id", EXTENSION_ID)

    for label, browser_dir in browser_dirs.items():
        manifest_path = (
            browser_dir / "NativeMessagingHosts" / "com.flashcard_generator.native_host.json"
        )
        assert manifest_path.exists(), f"{label} manifest missing"
        contents = json.loads(manifest_path.read_text())
        assert contents["name"] == "com.flashcard_generator.native_host"
        assert contents["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID}/"]
        assert contents["path"] == str(native_host_dir / "run_host.sh")
        assert contents["type"] == "stdio"


@pytest.mark.django_db
def test_creates_native_messaging_hosts_dir_when_missing(native_host_dir, tmp_path, monkeypatch):
    chrome_dir = tmp_path / "Chrome"
    chrome_dir.mkdir()  # browser installed, but NativeMessagingHosts/ absent
    monkeypatch.setattr(
        cmd,
        "CANDIDATE_BROWSERS",
        [("Chrome", chrome_dir, chrome_dir / "NativeMessagingHosts")],
    )

    call_command("install_native_host", "--extension-id", EXTENSION_ID)

    manifest_path = chrome_dir / "NativeMessagingHosts" / "com.flashcard_generator.native_host.json"
    assert manifest_path.exists()


@pytest.mark.django_db
def test_skips_browser_whose_directory_is_absent(native_host_dir, tmp_path, monkeypatch):
    chrome_dir = tmp_path / "Chrome"
    chrome_dir.mkdir()
    brave_dir = tmp_path / "Brave-Browser"  # never created - Brave "not installed"
    monkeypatch.setattr(
        cmd,
        "CANDIDATE_BROWSERS",
        [
            ("Chrome", chrome_dir, chrome_dir / "NativeMessagingHosts"),
            ("Brave", brave_dir, brave_dir / "NativeMessagingHosts"),
        ],
    )

    out = io.StringIO()
    call_command("install_native_host", "--extension-id", EXTENSION_ID, stdout=out)

    assert (chrome_dir / "NativeMessagingHosts" / "com.flashcard_generator.native_host.json").exists()
    assert not brave_dir.exists()  # never fabricated
    assert "Brave" in out.getvalue()
    assert "not found" in out.getvalue() or "skipped" in out.getvalue()


@pytest.mark.django_db
def test_errors_clearly_when_neither_browser_present(native_host_dir, tmp_path, monkeypatch):
    chrome_dir = tmp_path / "Chrome"  # never created
    brave_dir = tmp_path / "Brave-Browser"  # never created
    monkeypatch.setattr(
        cmd,
        "CANDIDATE_BROWSERS",
        [
            ("Chrome", chrome_dir, chrome_dir / "NativeMessagingHosts"),
            ("Brave", brave_dir, brave_dir / "NativeMessagingHosts"),
        ],
    )

    with pytest.raises(CommandError) as exc_info:
        call_command("install_native_host", "--extension-id", EXTENSION_ID)

    message = str(exc_info.value)
    assert str(chrome_dir) in message
    assert str(brave_dir) in message
    assert not chrome_dir.exists()
    assert not brave_dir.exists()


@pytest.mark.django_db
def test_mints_token_when_absent(native_host_dir, browser_dirs, settings):
    assert not settings.EXTENSION_TOKEN_FILE.exists()

    call_command("install_native_host", "--extension-id", EXTENSION_ID)

    assert settings.EXTENSION_TOKEN_FILE.exists()


@pytest.mark.django_db
def test_leaves_existing_token_untouched(native_host_dir, browser_dirs, settings):
    existing = mint_token()

    call_command("install_native_host", "--extension-id", EXTENSION_ID)

    assert settings.EXTENSION_TOKEN_FILE.read_text().strip() == existing


@pytest.mark.django_db
def test_rerun_overwrites_wrapper_and_manifests_idempotently(native_host_dir, browser_dirs):
    call_command("install_native_host", "--extension-id", "a" * 32)
    call_command("install_native_host", "--extension-id", "p" * 32)

    # Only one manifest file per browser dir - no leftover old-named files.
    for browser_dir in browser_dirs.values():
        host_dir = browser_dir / "NativeMessagingHosts"
        assert [f.name for f in host_dir.iterdir()] == [
            "com.flashcard_generator.native_host.json"
        ]
        contents = json.loads(
            (host_dir / "com.flashcard_generator.native_host.json").read_text()
        )
        assert contents["allowed_origins"] == [f"chrome-extension://{'p' * 32}/"]
        assert "a" * 32 not in json.dumps(contents)

    # Only one wrapper script, reflecting the latest run.
    wrapper = native_host_dir / "run_host.sh"
    assert wrapper.exists()


@pytest.mark.django_db
def test_rerun_with_existing_token_does_not_raise(native_host_dir, browser_dirs):
    mint_token()

    call_command("install_native_host", "--extension-id", EXTENSION_ID)
    call_command("install_native_host", "--extension-id", EXTENSION_ID)  # no FileExistsError


@pytest.mark.django_db
def test_prints_required_output(native_host_dir, browser_dirs):
    out = io.StringIO()
    call_command("install_native_host", "--extension-id", EXTENSION_ID, stdout=out)

    output = out.getvalue()
    assert EXTENSION_ID in output
    assert str(native_host_dir / "run_host.sh") in output
    for browser_dir in browser_dirs.values():
        manifest_path = (
            browser_dir / "NativeMessagingHosts" / "com.flashcard_generator.native_host.json"
        )
        assert str(manifest_path) in output


@pytest.mark.django_db
def test_prints_explicit_skip_message_for_missing_browser(native_host_dir, tmp_path, monkeypatch):
    chrome_dir = tmp_path / "Chrome"
    chrome_dir.mkdir()
    brave_dir = tmp_path / "Brave-Browser"
    monkeypatch.setattr(
        cmd,
        "CANDIDATE_BROWSERS",
        [
            ("Chrome", chrome_dir, chrome_dir / "NativeMessagingHosts"),
            ("Brave", brave_dir, brave_dir / "NativeMessagingHosts"),
        ],
    )

    out = io.StringIO()
    call_command("install_native_host", "--extension-id", EXTENSION_ID, stdout=out)

    output = out.getvalue()
    assert "Brave" in output
    assert str(brave_dir) in output
