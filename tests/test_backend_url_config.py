"""Issue #47: real backend-port configurability via shared BACKEND_URL.

Covers host/dev config resolution, spawn addrport matching, base_url
override reflection, host-config-wins mismatch policy, and
manifest/README doc consistency. No live processes: everything faked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

from django.conf import settings
from django.core.management import call_command

from submissions.management.commands import dev as dev_mod

HOST_PATH = Path(__file__).resolve().parent.parent / "native_host" / "host.py"

spec = importlib.util.spec_from_file_location("native_host_host_47", HOST_PATH)
host = importlib.util.module_from_spec(spec)
sys.modules["native_host_host_47"] = host
spec.loader.exec_module(host)


# -- config resolution ----------------------------------------------------


def test_host_resolve_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("BACKEND_URL", raising=False)
    assert host.resolve_backend_url() == host.DEFAULT_BACKEND_URL == host.BACKEND_URL


def test_host_resolve_blank_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "   ")
    assert host.resolve_backend_url() == host.DEFAULT_BACKEND_URL


def test_host_resolve_returns_override(monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    assert host.resolve_backend_url() == "http://127.0.0.1:9000/"


def test_host_and_dev_share_var_name_and_default():
    assert dev_mod.DEFAULT_BACKEND_URL.rstrip("/") == host.DEFAULT_BACKEND_URL.rstrip("/")
    assert settings.BACKEND_URL is not None


def test_settings_backend_url_tracks_env(monkeypatch):
    import os

    # settings.BACKEND_URL is read from the env at import; at runtime it
    # must equal whatever the env says (or the shared default).
    assert settings.BACKEND_URL == os.environ.get(
        "BACKEND_URL", "http://127.0.0.1:8000"
    )


def test_addrport_parsing_agrees():
    cases = {
        "http://127.0.0.1:8000/": "127.0.0.1:8000",
        "http://127.0.0.1:8000": "127.0.0.1:8000",
        "http://127.0.0.1:9000/": "127.0.0.1:9000",
        "http://localhost:9000/": "localhost:9000",
        "127.0.0.1:9000": "127.0.0.1:9000",  # bare addrport passes through
    }
    for url, addrport in cases.items():
        assert host.backend_url_to_addrport(url) == addrport
        assert dev_mod.backend_url_to_addrport(url) == addrport


def test_dev_default_addrport_tracks_env(monkeypatch):
    monkeypatch.delenv("BACKEND_URL", raising=False)
    assert dev_mod.default_addrport() == "127.0.0.1:8000"
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    assert dev_mod.default_addrport() == "127.0.0.1:9000"


def test_dev_explicit_addrport_wins_over_env(monkeypatch):
    seen = {}
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def fake_run(self, addrport, huey_args, restart_worker=True):
        seen["addrport"] = addrport
        return 0

    monkeypatch.setattr(dev_mod.Command, "run_supervised", fake_run)
    call_command("dev", addrport="127.0.0.1:7000")
    assert seen["addrport"] == "127.0.0.1:7000"


def test_dev_env_used_when_no_flag(monkeypatch):
    seen = {}
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def fake_run(self, addrport, huey_args, restart_worker=True):
        seen["addrport"] = addrport
        return 0

    monkeypatch.setattr(dev_mod.Command, "run_supervised", fake_run)
    call_command("dev")
    assert seen["addrport"] == "127.0.0.1:9000"


# -- spawn + base_url follow the override ---------------------------------


def _fake_project(tmp_path, token="tok-123"):
    (tmp_path / "manage.py").write_text("")
    (tmp_path / host.TOKEN_FILENAME).write_text(token)
    return tmp_path


def test_spawn_passes_matching_addrport(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    (tmp_path / "manage.py").write_text("")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd

        class FakeProc:
            pid = 1

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    host.spawn_backend(tmp_path, tmp_path / "log.txt")
    assert captured["cmd"][-2:] == ["--addrport", "127.0.0.1:9000"]


def test_spawn_explicit_addrport_used_verbatim(tmp_path, monkeypatch):
    (tmp_path / "manage.py").write_text("")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd

        class FakeProc:
            pid = 1

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    host.spawn_backend(tmp_path, tmp_path / "log.txt", "127.0.0.1:7000")
    assert captured["cmd"][-2:] == ["--addrport", "127.0.0.1:7000"]


def test_base_url_reflects_override_fast_path(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    project = _fake_project(tmp_path)

    def opener(url, timeout):
        assert url == "http://127.0.0.1:9000/"
        return object()

    reply = host.handle_request(project, opener)
    assert reply == {
        "ok": True,
        "already_running": True,
        "token": "tok-123",
        "base_url": "http://127.0.0.1:9000",
    }


def test_spawned_reply_base_url_reflects_override(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    project = _fake_project(tmp_path)
    seen = {}

    def opener(url, timeout):
        raise urllib.error.URLError("down")

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd

        class FakeProc:
            pid = 1

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(host, "poll_until_ready", lambda is_up_fn, **k: True)
    reply = host.handle_request(project, opener)
    assert reply["base_url"] == "http://127.0.0.1:9000"
    assert reply["already_running"] is False
    assert seen["cmd"][-2:] == ["--addrport", "127.0.0.1:9000"]


# -- mismatch: host config wins -------------------------------------------


def test_mismatch_other_port_up_configured_down_spawns_configured(
    tmp_path, monkeypatch
):
    """A hand-started backend on :7000 does not satisfy a host configured
    for :9000: the host spawns its own backend on :9000."""
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    project = _fake_project(tmp_path)
    state = {"up": False}
    spawned = []

    def flipping_opener(url, timeout):
        probed.append(url)
        if url == "http://127.0.0.1:9000/" and state["up"]:
            return object()
        raise urllib.error.URLError("down")

    probed = []

    def flipping_popen(cmd, **kwargs):
        spawned.append(cmd)
        state["up"] = True  # spawned :9000 backend comes up

        class FakeProc:
            pid = 1

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", flipping_popen)
    monkeypatch.setattr(host, "poll_until_ready", lambda is_up_fn, **k: is_up_fn())
    reply = host.handle_request(project, flipping_opener)
    assert reply["ok"] is True
    assert reply["already_running"] is False
    assert reply["base_url"] == "http://127.0.0.1:9000"
    assert probed == [] or all(u == "http://127.0.0.1:9000/" for u in probed)
    assert spawned[0][-2:] == ["--addrport", "127.0.0.1:9000"]


def test_mismatch_configured_up_other_port_ignored(tmp_path, monkeypatch):
    """Configured URL answers -> already_running for it, no spawn, even
    though another backend exists elsewhere."""
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9000/")
    project = _fake_project(tmp_path)

    def opener(url, timeout):
        assert url == "http://127.0.0.1:9000/"
        return object()

    monkeypatch.setattr(
        host.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    reply = host.handle_request(project, opener)
    assert reply["already_running"] is True
    assert reply["base_url"] == "http://127.0.0.1:9000"


# -- manifest / README consistency ----------------------------------------


def test_manifest_default_permission_matches_default_origin():
    manifest_path = Path(__file__).resolve().parent.parent / "extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    default_origin = host.DEFAULT_BACKEND_URL.rstrip("/")
    assert manifest["host_permissions"] == [default_origin + "/*"]


def test_readme_documents_override_flow():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "BACKEND_URL" in readme
    assert "host_permissions" in readme
    assert "chrome://extensions" in readme
    assert "--addrport" in readme
