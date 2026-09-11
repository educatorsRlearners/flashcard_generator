"""Tests for the extension-auth token lifecycle (issue #33).

Every test overrides ``settings.EXTENSION_TOKEN_FILE`` to a path under
``tmp_path`` via the ``settings`` fixture, so the real repo-root
``.extension_token`` is never touched.
"""

import time

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from submissions import extension_auth
from submissions.extension_auth import mint_token, read_token, token_matches


@pytest.fixture(autouse=True)
def token_file(tmp_path, settings):
    path = tmp_path / ".extension_token"
    settings.EXTENSION_TOKEN_FILE = path
    yield path


# --- module-level API --------------------------------------------------


def test_mint_then_read_round_trip(token_file):
    token = mint_token()
    assert read_token() == token
    assert len(token) > 20


def test_read_token_missing_file_returns_none(token_file):
    assert read_token() is None


def test_token_matches_missing_file_returns_false(token_file):
    assert token_matches("anything") is False


def test_token_matches_correct_and_incorrect(token_file):
    token = mint_token()
    assert token_matches(token) is True
    assert token_matches("wrong-token") is False


def test_mint_refuses_to_clobber_without_force(token_file):
    first = mint_token()
    with pytest.raises(FileExistsError):
        mint_token()
    assert read_token() == first


def test_mint_force_overwrites(token_file):
    first = mint_token()
    second = mint_token(force=True)
    assert second != first
    assert read_token() == second


def test_file_permissions_are_0600(token_file):
    mint_token()
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"


def test_read_token_picks_up_rotation_without_restart(token_file):
    mint_token()
    first = read_token()
    # Ensure a distinct mtime even on filesystems with coarse resolution.
    time.sleep(0.01)
    second_token = mint_token(force=True)
    assert read_token() == second_token
    assert second_token != first


def test_atomic_write_uses_tempfile_and_os_replace(token_file, monkeypatch):
    """The write goes to a temp file in the same dir, then os.replace()."""
    calls = []
    real_replace = extension_auth.os.replace

    def spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(extension_auth.os, "replace", spy_replace)
    mint_token()

    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == token_file
    assert str(src).startswith(str(token_file.parent))
    assert src != str(token_file)


# --- management command -------------------------------------------------


def test_command_mint_prints_token_and_writes_file(token_file):
    out = call_command_output("extension_token", "--mint")
    assert out.strip() == read_token()


def test_command_mint_twice_errors_and_does_not_change_token(token_file):
    call_command_output("extension_token", "--mint")
    original = read_token()
    with pytest.raises(CommandError):
        call_command_output("extension_token", "--mint")
    assert read_token() == original


def test_command_rotate_overwrites_whether_or_not_token_exists(token_file):
    first = call_command_output("extension_token", "--rotate").strip()
    second = call_command_output("extension_token", "--rotate").strip()
    assert first != second
    assert read_token() == second


def test_command_show_prints_current_token(token_file):
    minted = call_command_output("extension_token", "--mint").strip()
    shown = call_command_output("extension_token", "--show").strip()
    assert shown == minted


def test_command_show_without_token_errors_and_does_not_mint(token_file):
    with pytest.raises(CommandError):
        call_command_output("extension_token", "--show")
    assert read_token() is None


def test_command_requires_exactly_one_flag(token_file):
    # Django's CommandParser turns argparse's usage-error SystemExit into a
    # CommandError when not invoked from an actual command line (as here).
    with pytest.raises(CommandError):
        call_command("extension_token")

    with pytest.raises(CommandError):
        call_command("extension_token", "--mint", "--rotate")


def call_command_output(*args):
    import io

    out = io.StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()
