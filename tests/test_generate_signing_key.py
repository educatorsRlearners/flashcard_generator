"""Tests for the extension signing-key generator command (issue #51).

Runs the real ``openssl`` binary (matching how the manual steps it
replaces already invoke it) against faked manifest/private-key paths -
never the real ``extension/manifest.json`` or a real dotfile at the
project root.
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from submissions.management.commands import generate_signing_key as cmd

PLACEHOLDER = cmd.PLACEHOLDER_KEY


@pytest.fixture
def manifest_path(tmp_path, monkeypatch):
    path = tmp_path / "extension" / "manifest.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Flashcard Generator",
                "version": "1.0.0",
                "description": "Generate flashcards from the page you're viewing.",
                "key": PLACEHOLDER,
                "permissions": ["nativeMessaging", "activeTab", "scripting", "tabs"],
                "host_permissions": ["http://127.0.0.1:8000/*"],
                "action": {"default_popup": "popup.html"},
            },
            indent=2,
        )
        + "\n"
    )
    monkeypatch.setattr(cmd, "MANIFEST_PATH", path)
    return path


@pytest.fixture
def private_key_path(tmp_path, monkeypatch):
    path = tmp_path / ".extension_signing_key.pem"
    monkeypatch.setattr(cmd, "PRIVATE_KEY_PATH", path)
    return path


# -- pure helper -------------------------------------------------------------


def test_generate_keypair_produces_pem_and_valid_base64_der_pubkey():
    private_key_pem, public_key_b64 = cmd.generate_keypair()

    assert private_key_pem.startswith("-----BEGIN")
    assert "PRIVATE KEY-----" in private_key_pem

    # Valid base64, and openssl can parse it back as a DER public key.
    der = base64.b64decode(public_key_b64)
    result = subprocess.run(
        ["openssl", "rsa", "-pubin", "-inform", "DER", "-noout", "-text"],
        input=der,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


# -- command behaviour --------------------------------------------------------


@pytest.mark.django_db
def test_fresh_checkout_generates_key_and_updates_manifest_only_key_field(
    manifest_path, private_key_path
):
    before = json.loads(manifest_path.read_text())

    call_command("generate_signing_key")

    after = json.loads(manifest_path.read_text())
    assert after["key"] != PLACEHOLDER
    assert after["key"] != before["key"]
    for field in before:
        if field == "key":
            continue
        assert after[field] == before[field]
    assert set(after.keys()) == set(before.keys())


@pytest.mark.django_db
def test_private_key_written_outside_manifest_dir(manifest_path, private_key_path):
    call_command("generate_signing_key")

    assert private_key_path.exists()
    assert private_key_path.read_text().startswith("-----BEGIN")


@pytest.mark.django_db
def test_rerun_without_force_fails_and_leaves_files_unchanged(
    manifest_path, private_key_path
):
    call_command("generate_signing_key")
    manifest_before = manifest_path.read_bytes()
    key_before = private_key_path.read_bytes()

    with pytest.raises(CommandError, match="--force"):
        call_command("generate_signing_key")

    assert manifest_path.read_bytes() == manifest_before
    assert private_key_path.read_bytes() == key_before


@pytest.mark.django_db
def test_force_regenerates_both_files(manifest_path, private_key_path):
    call_command("generate_signing_key")
    manifest_before = manifest_path.read_bytes()
    key_before = private_key_path.read_bytes()

    call_command("generate_signing_key", "--force")

    assert manifest_path.read_bytes() != manifest_before
    assert private_key_path.read_bytes() != key_before


@pytest.mark.django_db
def test_openssl_missing_raises_and_does_not_touch_manifest_or_key(
    manifest_path, private_key_path, monkeypatch
):
    monkeypatch.setattr(cmd.shutil, "which", lambda _name: None)
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(CommandError, match="openssl"):
        call_command("generate_signing_key")

    assert manifest_path.read_bytes() == manifest_before
    assert not private_key_path.exists()


@pytest.mark.django_db
def test_manifest_missing_raises_and_writes_no_key_file(tmp_path, monkeypatch, private_key_path):
    missing_path = tmp_path / "extension" / "manifest.json"
    monkeypatch.setattr(cmd, "MANIFEST_PATH", missing_path)

    with pytest.raises(CommandError, match=str(missing_path)):
        call_command("generate_signing_key")

    assert not private_key_path.exists()


@pytest.mark.django_db
def test_manifest_invalid_json_raises_and_writes_no_key_file(
    tmp_path, monkeypatch, private_key_path
):
    bad_path = tmp_path / "extension" / "manifest.json"
    bad_path.parent.mkdir()
    bad_path.write_text("{not valid json")
    monkeypatch.setattr(cmd, "MANIFEST_PATH", bad_path)

    with pytest.raises(CommandError, match="not valid JSON"):
        call_command("generate_signing_key")

    assert not private_key_path.exists()


@pytest.mark.django_db
def test_manifest_missing_key_field_raises_and_writes_no_key_file(
    tmp_path, monkeypatch, private_key_path
):
    no_key_path = tmp_path / "extension" / "manifest.json"
    no_key_path.parent.mkdir()
    no_key_path.write_text(json.dumps({"manifest_version": 3}))
    monkeypatch.setattr(cmd, "MANIFEST_PATH", no_key_path)

    with pytest.raises(CommandError, match='"key" field'):
        call_command("generate_signing_key")

    assert not private_key_path.exists()


@pytest.mark.django_db
def test_openssl_failure_leaves_no_partial_key_or_manifest_change(
    manifest_path, private_key_path, monkeypatch
):
    def failing_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr=b"boom")

    monkeypatch.setattr(cmd.subprocess, "run", failing_run)
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(CommandError, match="openssl failed"):
        call_command("generate_signing_key")

    assert manifest_path.read_bytes() == manifest_before
    assert not private_key_path.exists()


@pytest.mark.django_db
def test_prints_required_output(manifest_path, private_key_path):
    import io

    out = io.StringIO()
    call_command("generate_signing_key", stdout=out)

    output = out.getvalue()
    assert str(private_key_path) in output
    assert "reload" in output.lower()
    assert "install_native_host" in output
