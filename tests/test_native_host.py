"""Tests for the native messaging host script (issue #37).

``native_host/host.py`` lives outside ``submissions/`` (never imported by
the Django project, only invoked by Chrome as a subprocess) but
``pyproject.toml``'s ``testpaths = ["tests"]`` means a bare
``native_host/test_*.py`` would not be discovered - so these tests live
here instead and import the script as a plain module. No Django DB access
is needed; everything (subprocess spawns, sockets, the clock) is faked out.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

HOST_PATH = Path(__file__).resolve().parent.parent / "native_host" / "host.py"

spec = importlib.util.spec_from_file_location("native_host_host", HOST_PATH)
host = importlib.util.module_from_spec(spec)
sys.modules["native_host_host"] = host
spec.loader.exec_module(host)


# -- wire protocol --------------------------------------------------------


def frame(obj) -> bytes:
    payload = json.dumps(obj).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def test_pack_message_round_trips():
    packed = host.pack_message({"a": 1})
    (length,) = struct.unpack("<I", packed[:4])
    body = packed[4 : 4 + length]
    assert json.loads(body) == {"a": 1}


def test_read_message_parses_framed_json():
    stream = io.BytesIO(frame({"hello": "world"}))
    assert host.read_message(stream) == {"hello": "world"}


def test_read_message_accepts_any_json_value():
    # v1: content is ignored, any valid JSON value is accepted.
    stream = io.BytesIO(frame(["just", "an", "array"]))
    assert host.read_message(stream) == ["just", "an", "array"]


def test_read_message_raises_eof_on_empty_stdin():
    stream = io.BytesIO(b"")
    with pytest.raises(EOFError):
        host.read_message(stream)


def test_read_message_raises_eof_on_truncated_body():
    # Length prefix claims more bytes than actually follow.
    stream = io.BytesIO(struct.pack("<I", 100) + b"{}")
    with pytest.raises(EOFError):
        host.read_message(stream)


def test_read_message_raises_bad_request_on_invalid_json():
    body = b"not json"
    stream = io.BytesIO(struct.pack("<I", len(body)) + body)
    with pytest.raises(host.BadRequest):
        host.read_message(stream)


def test_read_message_raises_bad_request_on_invalid_utf8():
    body = b"\xff\xfe\xfa"
    stream = io.BytesIO(struct.pack("<I", len(body)) + body)
    with pytest.raises(host.BadRequest):
        host.read_message(stream)


def test_write_message_writes_single_frame():
    buf = io.BytesIO()
    host.write_message({"ok": True}, buf)
    data = buf.getvalue()
    (length,) = struct.unpack("<I", data[:4])
    assert json.loads(data[4 : 4 + length]) == {"ok": True}
    assert len(data) == 4 + length  # nothing extra written


# -- readiness probe --------------------------------------------------------


def test_is_up_true_on_any_response():
    def fake_opener(url, timeout):
        return object()  # any response object, status ignored

    assert host.is_up(fake_opener, "http://x/", 1.0) is True


def test_is_up_false_on_url_error():
    def fake_opener(url, timeout):
        raise urllib.error.URLError("connection refused")

    assert host.is_up(fake_opener, "http://x/", 1.0) is False


def test_is_up_false_on_timeout():
    def fake_opener(url, timeout):
        raise TimeoutError("timed out")

    assert host.is_up(fake_opener, "http://x/", 1.0) is False


# -- poll backoff/timeout math -----------------------------------------


def test_poll_until_ready_succeeds_once_up():
    calls = {"n": 0}

    def is_up_fn():
        calls["n"] += 1
        return calls["n"] >= 3

    clock = {"t": 0.0}
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s

    assert host.poll_until_ready(
        is_up_fn,
        interval=0.5,
        timeout=30.0,
        sleep_fn=fake_sleep,
        now_fn=lambda: clock["t"],
    )
    assert calls["n"] == 3
    assert sleeps == [0.5, 0.5]


def test_poll_until_ready_gives_up_exactly_at_boundary():
    """30s timeout / 0.5s interval: attempts at t=0,0.5,...,29.5,30.0 (61
    attempts), then gives up - not one short or long."""
    clock = {"t": 0.0}
    attempts = {"n": 0}

    def is_up_fn():
        attempts["n"] += 1
        return False

    def fake_sleep(s):
        clock["t"] += s

    ok = host.poll_until_ready(
        is_up_fn,
        interval=0.5,
        timeout=30.0,
        sleep_fn=fake_sleep,
        now_fn=lambda: clock["t"],
    )
    assert ok is False
    assert attempts["n"] == 61
    assert clock["t"] == pytest.approx(30.0)


def test_poll_until_ready_uses_documented_defaults():
    assert host.POLL_INTERVAL_S == 0.5
    assert host.READY_TIMEOUT_S == 30.0
    assert host.PROBE_TIMEOUT_S == 1.0
    assert host.STALE_LOCK_THRESHOLD_S == 35.0


# -- child env construction -------------------------------------------


def test_build_child_env_strips_pytest_and_huey_flags():
    base = {
        "PATH": "/usr/bin",
        "PYTEST_CURRENT_TEST": "tests/test_x.py::y",
        "HUEY_IMMEDIATE": "1",
    }
    env = host.build_child_env(base)
    assert "PYTEST_CURRENT_TEST" not in env
    assert "HUEY_IMMEDIATE" not in env
    assert env["PATH"] == "/usr/bin"


def test_build_child_env_fine_when_flags_absent():
    env = host.build_child_env({"PATH": "/usr/bin"})
    assert "PYTEST_CURRENT_TEST" not in env
    assert "HUEY_IMMEDIATE" not in env


# -- lock file ------------------------------------------------------------


def test_acquire_lock_creates_when_absent(tmp_path):
    lock_path = tmp_path / ".native_host.lock"
    assert host.acquire_lock(lock_path) is True
    assert lock_path.exists()


def test_acquire_lock_detects_when_held(tmp_path):
    lock_path = tmp_path / ".native_host.lock"
    lock_path.write_text("")
    assert host.acquire_lock(lock_path) is False
    assert lock_path.exists()  # untouched, still held


def test_acquire_lock_clears_stale_lock(tmp_path):
    lock_path = tmp_path / ".native_host.lock"
    lock_path.write_text("")
    # Fake mtime rather than a real 35s sleep: set it in the past relative
    # to the injected now_fn.
    old_mtime = 1_000.0
    os.utime(lock_path, (old_mtime, old_mtime))
    now_fn = lambda: old_mtime + host.STALE_LOCK_THRESHOLD_S + 1.0
    assert host.acquire_lock(lock_path, now_fn=now_fn) is True


def test_acquire_lock_keeps_fresh_lock(tmp_path):
    lock_path = tmp_path / ".native_host.lock"
    lock_path.write_text("")
    fresh_mtime = 1_000.0
    os.utime(lock_path, (fresh_mtime, fresh_mtime))
    now_fn = lambda: fresh_mtime + host.STALE_LOCK_THRESHOLD_S - 1.0
    assert host.acquire_lock(lock_path, now_fn=now_fn) is False


def test_release_lock_removes_file(tmp_path):
    lock_path = tmp_path / ".native_host.lock"
    lock_path.write_text("")
    host.release_lock(lock_path)
    assert not lock_path.exists()


def test_release_lock_missing_file_does_not_raise(tmp_path):
    host.release_lock(tmp_path / "nope.lock")  # no error


# -- reply payload construction -----------------------------------------


def test_reply_already_running():
    assert host.reply_already_running("tok") == {
        "ok": True,
        "already_running": True,
        "token": "tok",
        "base_url": host.BACKEND_URL.rstrip("/"),
    }


def test_reply_spawned():
    assert host.reply_spawned("tok") == {
        "ok": True,
        "already_running": False,
        "token": "tok",
        "base_url": host.BACKEND_URL.rstrip("/"),
    }


def test_reply_error_shapes():
    for error in ("bad_request", "spawn_failed", "timeout", "token_unavailable"):
        payload = host.reply_error(error, "details here")
        assert payload == {"ok": False, "error": error, "detail": "details here"}
        assert "base_url" not in payload


# -- get_token --------------------------------------------------------------


def test_get_token_reads_existing_file(tmp_path):
    (tmp_path / host.TOKEN_FILENAME).write_text("existing-token\n")
    assert host.get_token(tmp_path) == "existing-token"


def test_get_token_mints_via_subprocess_when_missing(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, cwd, capture_output, text, timeout):
        calls.append((cmd, cwd))
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout="minted-token\n", stderr=""
        )

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    token = host.get_token(tmp_path)
    assert token == "minted-token"
    assert calls[0][0] == [
        "uv",
        "run",
        "python",
        "manage.py",
        "extension_token",
        "--mint",
    ]
    assert calls[0][1] == str(tmp_path)


def test_get_token_raises_when_mint_subprocess_fails(tmp_path, monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout="", stderr="boom"
        )

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    with pytest.raises(host.TokenUnavailable):
        host.get_token(tmp_path)


def test_get_token_raises_when_mint_produces_no_token(tmp_path, monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="  \n", stderr="")

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    with pytest.raises(host.TokenUnavailable):
        host.get_token(tmp_path)


def test_get_token_raises_when_uv_not_found(tmp_path, monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        raise FileNotFoundError("uv not found")

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    with pytest.raises(host.TokenUnavailable):
        host.get_token(tmp_path)


# -- spawn_backend ------------------------------------------------------


def test_spawn_backend_fails_fast_when_manage_py_missing(tmp_path):
    with pytest.raises(host.SpawnFailed):
        host.spawn_backend(tmp_path, tmp_path / "log.txt")


def test_spawn_backend_never_inherits_our_stdout(tmp_path, monkeypatch):
    (tmp_path / "manage.py").write_text("")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        captured["cmd"] = cmd

        class FakeProc:
            pid = 1234

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    host.spawn_backend(tmp_path, tmp_path / "log.txt")

    assert captured["cmd"] == [
        "uv",
        "run",
        "python",
        "manage.py",
        "dev",
        "--addrport",
        host.backend_url_to_addrport(host.resolve_backend_url()),
    ]
    assert captured["cwd"] == str(tmp_path)
    assert captured["start_new_session"] is True
    assert captured["stdin"] == subprocess.DEVNULL
    # stdout/stderr must not be our own stdout - they're the log file object.
    assert captured["stdout"] is not sys.stdout
    assert captured["stderr"] is not sys.stdout
    assert captured["stdout"].name == str(tmp_path / "log.txt")


def test_spawn_backend_strips_pytest_and_huey_env(tmp_path, monkeypatch):
    (tmp_path / "manage.py").write_text("")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::y")
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)

        class FakeProc:
            pid = 1234

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    host.spawn_backend(tmp_path, tmp_path / "log.txt")
    assert "PYTEST_CURRENT_TEST" not in captured["env"]
    assert "HUEY_IMMEDIATE" not in captured["env"]


def test_spawn_backend_raises_spawn_failed_on_oserror(tmp_path, monkeypatch):
    (tmp_path / "manage.py").write_text("")

    def fake_popen(cmd, **kwargs):
        raise FileNotFoundError("uv not found")

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    with pytest.raises(host.SpawnFailed):
        host.spawn_backend(tmp_path, tmp_path / "log.txt")


# -- resolve_uv_binary / baked-in uv path (issue #55) -----------------


def test_resolve_uv_binary_uses_env_var_when_set():
    env = {host.UV_ENV_VAR: "/abs/path/to/uv"}
    assert host.resolve_uv_binary(env) == "/abs/path/to/uv"


def test_resolve_uv_binary_falls_back_to_bare_uv_when_unset():
    assert host.resolve_uv_binary({}) == "uv"


def test_resolve_uv_binary_falls_back_to_bare_uv_when_blank():
    assert host.resolve_uv_binary({host.UV_ENV_VAR: "  "}) == "uv"


def test_spawn_backend_uses_baked_in_uv_path(tmp_path, monkeypatch):
    (tmp_path / "manage.py").write_text("")
    monkeypatch.setenv(host.UV_ENV_VAR, "/opt/homebrew/bin/uv")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd

        class FakeProc:
            pid = 1234

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    host.spawn_backend(tmp_path, tmp_path / "log.txt")

    assert captured["cmd"][0] == "/opt/homebrew/bin/uv"


def test_spawn_backend_raises_specific_message_for_stale_baked_in_path(tmp_path, monkeypatch):
    (tmp_path / "manage.py").write_text("")
    monkeypatch.setenv(host.UV_ENV_VAR, "/Users/x/.local/bin/uv")

    def fake_popen(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    with pytest.raises(host.SpawnFailed) as exc_info:
        host.spawn_backend(tmp_path, tmp_path / "log.txt")

    message = str(exc_info.value)
    assert "/Users/x/.local/bin/uv" in message
    assert "install_native_host" in message


def test_get_token_uses_baked_in_uv_path(tmp_path, monkeypatch):
    monkeypatch.setenv(host.UV_ENV_VAR, "/opt/homebrew/bin/uv")
    calls = []

    def fake_run(cmd, cwd, capture_output, text, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="tok\n", stderr="")

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    host.get_token(tmp_path)

    assert calls[0][0] == "/opt/homebrew/bin/uv"


def test_get_token_raises_specific_message_for_stale_baked_in_path(tmp_path, monkeypatch):
    monkeypatch.setenv(host.UV_ENV_VAR, "/Users/x/.local/bin/uv")

    def fake_run(cmd, cwd, capture_output, text, timeout):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    with pytest.raises(host.TokenUnavailable) as exc_info:
        host.get_token(tmp_path)

    message = str(exc_info.value)
    assert "/Users/x/.local/bin/uv" in message
    assert "install_native_host" in message


# -- handle_request (end-to-end within the module, all I/O faked) ------


def _fake_project(tmp_path, token="tok-123"):
    (tmp_path / "manage.py").write_text("")
    (tmp_path / host.TOKEN_FILENAME).write_text(token)
    return tmp_path


def test_handle_request_already_running_fast_path_no_spawn(tmp_path, monkeypatch):
    project = _fake_project(tmp_path)

    def opener(url, timeout):
        return object()

    monkeypatch.setattr(
        host.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    reply = host.handle_request(project, opener, host.BACKEND_URL)
    assert reply == {
        "ok": True,
        "already_running": True,
        "token": "tok-123",
        "base_url": host.BACKEND_URL.rstrip("/"),
    }


def test_handle_request_spawns_and_becomes_ready(tmp_path, monkeypatch):
    project = _fake_project(tmp_path)
    state = {"up": False}

    def opener(url, timeout):
        if not state["up"]:
            raise urllib.error.URLError("down")
        return object()

    def fake_popen(cmd, **kwargs):
        state["up"] = True  # pretend the spawned backend comes up immediately

        class FakeProc:
            pid = 1

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(host, "poll_until_ready", lambda is_up_fn, **k: is_up_fn())

    reply = host.handle_request(project, opener, host.BACKEND_URL)
    assert reply == {
        "ok": True,
        "already_running": False,
        "token": "tok-123",
        "base_url": host.BACKEND_URL.rstrip("/"),
    }
    assert not (project / host.LOCK_FILENAME).exists()  # released


def test_handle_request_timeout_reply(tmp_path, monkeypatch):
    project = _fake_project(tmp_path)

    def opener(url, timeout):
        raise urllib.error.URLError("down")

    def fake_popen(cmd, **kwargs):
        class FakeProc:
            pid = 1

        return FakeProc()

    monkeypatch.setattr(host.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(host, "poll_until_ready", lambda is_up_fn, **k: False)

    reply = host.handle_request(project, opener, host.BACKEND_URL)
    assert reply["ok"] is False
    assert reply["error"] == "timeout"
    assert not (project / host.LOCK_FILENAME).exists()  # released even on timeout


def test_handle_request_spawn_failed_reply(tmp_path, monkeypatch):
    # manage.py missing -> spawn_backend raises SpawnFailed
    project = tmp_path
    (project / host.TOKEN_FILENAME).write_text("tok")

    def opener(url, timeout):
        raise urllib.error.URLError("down")

    reply = host.handle_request(project, opener, host.BACKEND_URL)
    assert reply == {
        "ok": False,
        "error": "spawn_failed",
        "detail": f"manage.py not found at {project / 'manage.py'}",
    }
    assert not (project / host.LOCK_FILENAME).exists()


def test_handle_request_token_unavailable_reply_on_fast_path(tmp_path, monkeypatch):
    project = tmp_path
    (project / "manage.py").write_text("")
    # No token file, and minting fails.

    def opener(url, timeout):
        return object()  # already up

    def fake_run(cmd, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="no")

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    reply = host.handle_request(project, opener, host.BACKEND_URL)
    assert reply["ok"] is False
    assert reply["error"] == "token_unavailable"


def test_handle_request_does_not_spawn_when_lock_held(tmp_path, monkeypatch):
    project = _fake_project(tmp_path)
    lock_path = project / host.LOCK_FILENAME
    lock_path.write_text("")  # simulate another invocation holding the lock

    def opener(url, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(
        host.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    monkeypatch.setattr(host, "poll_until_ready", lambda is_up_fn, **k: False)

    reply = host.handle_request(project, opener, host.BACKEND_URL)
    assert reply["error"] == "timeout"
    # This invocation didn't create the lock, so it must not remove it either.
    assert lock_path.exists()


# -- main() stdin/stdout wiring ------------------------------------------


def test_main_clean_eof_exits_0_no_output(monkeypatch):
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(b"")})())
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", type("S", (), {"buffer": out})())
    assert host.main() == 0
    assert out.getvalue() == b""


def test_main_bad_request_writes_one_framed_error_reply(monkeypatch):
    bad = struct.pack("<I", 3) + b"xyz"  # not valid JSON
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(bad)})())
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", type("S", (), {"buffer": out})())
    assert host.main() == 0
    data = out.getvalue()
    (length,) = struct.unpack("<I", data[:4])
    payload = json.loads(data[4 : 4 + length])
    assert payload["ok"] is False
    assert payload["error"] == "bad_request"
    assert len(data) == 4 + length  # exactly one frame, nothing else


def test_main_writes_exactly_one_reply_on_success(monkeypatch):
    monkeypatch.setattr(
        sys, "stdin", type("S", (), {"buffer": io.BytesIO(frame({}))})()
    )
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", type("S", (), {"buffer": out})())
    monkeypatch.setattr(
        host, "handle_request", lambda: {"ok": True, "already_running": True, "token": "t"}
    )
    assert host.main() == 0
    data = out.getvalue()
    (length,) = struct.unpack("<I", data[:4])
    assert len(data) == 4 + length
    assert json.loads(data[4:]) == {"ok": True, "already_running": True, "token": "t"}
