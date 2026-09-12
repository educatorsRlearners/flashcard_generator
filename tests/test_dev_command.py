"""Issue #20: dev supervisor (runserver + run_huey together).

No live processes are spawned here: handle()/run_supervised() are tested
with fakes, and Popen is asserted never to run under pytest.
"""

import os
import signal

from django.core.management import call_command

from submissions.management.commands import dev as dev_mod


class _FakeProc:
    """A child process that never exits on its own (poll() -> None)."""

    def __init__(self):
        self.stdout = iter([])

    def poll(self):
        return None


def test_build_cmds_target_manage_py():
    web = dev_mod.build_web_cmd("127.0.0.1:8000")
    worker = dev_mod.build_worker_cmd([])
    assert web[-2:] == ["runserver", "127.0.0.1:8000"]
    assert worker[-1:] == ["run_huey"]
    assert web[1].endswith("manage.py") and worker[1].endswith("manage.py")
    assert os.path.exists(web[1])
    assert dev_mod.build_worker_cmd(["--workers", "2"])[-2:] == ["--workers", "2"]


def test_child_env_drops_huey_immediate(monkeypatch):
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")
    assert "HUEY_IMMEDIATE" not in dev_mod.child_env()
    assert dev_mod.child_env()["PYTHONUNBUFFERED"] == "1"


def test_prune_restarts_window():
    assert dev_mod.prune_restarts([0.0, 50.0, 59.5], 60.0, 60.0) == [50.0, 59.5]
    assert dev_mod.prune_restarts([], 10.0, 60.0) == []


def test_handle_delegates_without_spawning(monkeypatch):
    seen = {}

    def fake_run(self, addrport, huey_args, restart_worker=True):
        seen.update(
            {"addrport": addrport, "huey_args": huey_args, "restart": restart_worker}
        )
        return 0

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(dev_mod.Command, "run_supervised", fake_run)
    monkeypatch.setattr(dev_mod.Command, "_spawn", lambda *a, **k: None)
    import subprocess

    called = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
            AssertionError("must not spawn")
        ),
    )
    call_command("dev", addrport="127.0.0.1:9000", huey_args="--workers 2")
    assert seen == {
        "addrport": "127.0.0.1:9000",
        "huey_args": ["--workers", "2"],
        "restart": True,
    }
    assert called == []


def test_pending_migrations_block_dev_startup(monkeypatch, capsys):
    """Issue #79: unapplied migrations -> non-zero exit, no children spawned."""
    fake_migration = type("Migration", (), {"app_label": "submissions", "name": "0099_fake"})()
    monkeypatch.setattr(
        dev_mod, "pending_migrations", lambda alias=None: [(fake_migration, False)]
    )
    spawned = []
    monkeypatch.setattr(
        dev_mod.Command,
        "_spawn",
        lambda self, cmd, env: spawned.append(cmd) or (_ for _ in ()).throw(
            AssertionError("must not spawn")
        ),
    )
    code = dev_mod.Command().run_supervised("127.0.0.1:8000", [])
    assert code != 0
    assert spawned == []
    err = capsys.readouterr().err
    assert "submissions.0099_fake" in err


def test_no_pending_migrations_starts_normally(monkeypatch):
    """Issue #79: no pending migrations -> normal startup, children spawned."""
    monkeypatch.setattr(dev_mod, "pending_migrations", lambda alias=None: [])
    spawned = []

    class FakeProc:
        def __init__(self):
            self.stdout = iter([])

        def poll(self):
            return 0

    def fake_spawn(self, cmd, env):
        spawned.append(cmd)
        return FakeProc()

    monkeypatch.setattr(dev_mod.Command, "_spawn", fake_spawn)
    dev_mod.Command().run_supervised("127.0.0.1:8000", [])
    assert len(spawned) == 2
    assert spawned[0][-2:] == ["runserver", "127.0.0.1:8000"]
    assert spawned[1][-1] == "run_huey"


def test_handle_refuses_under_pytest(monkeypatch, capsys):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_dev_command.py::x")
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    call_command("dev")  # returns without spawning
    assert "under pytest" in capsys.readouterr().err


# -- issue #54: dev watches .env and restarts children on change -----------


def test_hash_env_file_is_content_based_not_mtime(tmp_path):
    """Content hash, so a touch with no content change never triggers a restart."""
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=abc\n")
    first = dev_mod.hash_env_file(env_file)
    # Touch: change mtime only, content identical.
    os.utime(env_file, None)
    assert dev_mod.hash_env_file(env_file) == first

    env_file.write_text("EXTENSION_ID=xyz\n")
    assert dev_mod.hash_env_file(env_file) != first


def test_hash_env_file_missing_file_is_none(tmp_path):
    assert dev_mod.hash_env_file(tmp_path / "does-not-exist.env") is None


def test_env_file_vars_parses_names(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "EXTENSION_ID=abc123\n"
        "export BACKEND_URL=http://x\n"
        "MALFORMED_LINE_NO_EQUALS\n"
    )
    assert dev_mod.env_file_vars(env_file) == {"EXTENSION_ID", "BACKEND_URL"}


def test_env_file_vars_missing_file_is_empty_set(tmp_path):
    assert dev_mod.env_file_vars(tmp_path / "does-not-exist.env") == set()


def test_format_shell_override_warning_names_the_vars():
    msg = dev_mod.format_shell_override_warning(["EXTENSION_ID"])
    assert "EXTENSION_ID" in msg
    assert "override=False" in msg


def _run_supervised_with_env_watch(monkeypatch, tmp_path, sleep_side_effects):
    """Common harness: fakes migrations/spawn, runs supervised loop with a
    scripted sequence of time.sleep() side effects (one per tick)."""
    monkeypatch.setattr(dev_mod, "pending_migrations", lambda alias=None: [])
    monkeypatch.setattr(dev_mod, "env_path", lambda: tmp_path / ".env")

    spawned = []

    def fake_spawn(self, cmd, env):
        spawned.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(dev_mod.Command, "_spawn", fake_spawn)
    monkeypatch.setattr(dev_mod.Command, "_pump", lambda self, proc, prefix: None)
    monkeypatch.setattr(dev_mod.Command, "_terminate", lambda self, proc: None)

    calls = iter(sleep_side_effects)

    def fake_sleep(_seconds):
        effect = next(calls, None)
        if effect is not None:
            effect()

    monkeypatch.setattr(dev_mod.time, "sleep", fake_sleep)
    code = dev_mod.Command().run_supervised("127.0.0.1:8000", [])
    return code, spawned


def test_no_restart_on_first_tick(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=abc\n")

    def stop():
        raise KeyboardInterrupt

    code, spawned = _run_supervised_with_env_watch(monkeypatch, tmp_path, [stop])
    assert code == 0
    assert len(spawned) == 2  # only the initial spawn; no restart


def test_no_restart_on_unchanged_touch(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=abc\n")

    def touch():
        os.utime(env_file, None)

    def stop():
        raise KeyboardInterrupt

    code, spawned = _run_supervised_with_env_watch(
        monkeypatch, tmp_path, [touch, touch, stop]
    )
    assert code == 0
    assert len(spawned) == 2  # still just the initial spawn


def test_restart_triggered_on_changed_env_content(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=abc\n")

    def change():
        env_file.write_text("EXTENSION_ID=xyz\n")

    def stop():
        raise KeyboardInterrupt

    code, spawned = _run_supervised_with_env_watch(
        monkeypatch, tmp_path, [change, stop]
    )
    assert code == 0
    assert len(spawned) == 4  # initial pair + restarted pair
    out = capsys.readouterr().out
    assert ".env changed" in out
    assert "restarting" in out


def test_env_created_after_start_triggers_restart(monkeypatch, tmp_path):
    """.env not present at dev startup, created later -> still a restart (#54)."""
    env_file = tmp_path / ".env"  # does not exist yet

    def create():
        env_file.write_text("EXTENSION_ID=abc\n")

    def stop():
        raise KeyboardInterrupt

    code, spawned = _run_supervised_with_env_watch(
        monkeypatch, tmp_path, [create, stop]
    )
    assert code == 0
    assert len(spawned) == 4


def test_env_deleted_while_running_triggers_restart(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=abc\n")

    def delete():
        env_file.unlink()

    def stop():
        raise KeyboardInterrupt

    code, spawned = _run_supervised_with_env_watch(
        monkeypatch, tmp_path, [delete, stop]
    )
    assert code == 0
    assert len(spawned) == 4


def test_env_restart_warns_when_var_shadowed_by_shell(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=abc\n")
    monkeypatch.setenv("EXTENSION_ID", "shell-value")

    def change():
        env_file.write_text("EXTENSION_ID=xyz\n")

    def stop():
        raise KeyboardInterrupt

    _run_supervised_with_env_watch(monkeypatch, tmp_path, [change, stop])
    out = capsys.readouterr().out
    assert "EXTENSION_ID" in out
    assert "override=False" in out


def test_env_restart_does_not_warn_when_not_shadowed(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=abc\n")
    monkeypatch.delenv("EXTENSION_ID", raising=False)

    def change():
        env_file.write_text("EXTENSION_ID=xyz\n")

    def stop():
        raise KeyboardInterrupt

    _run_supervised_with_env_watch(monkeypatch, tmp_path, [change, stop])
    out = capsys.readouterr().out
    assert "override=False" not in out


def test_env_triggered_restarts_do_not_consume_crash_loop_budget(monkeypatch, tmp_path):
    """Repeated .env edits alone must never trip the worker crash-loop guard."""
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=v0\n")

    edits = [f"EXTENSION_ID=v{i}\n" for i in range(1, dev_mod.MAX_WORKER_RESTARTS + 3)]

    def make_edit(content):
        return lambda: env_file.write_text(content)

    def stop():
        raise KeyboardInterrupt

    effects = [make_edit(c) for c in edits] + [stop]
    code, spawned = _run_supervised_with_env_watch(monkeypatch, tmp_path, effects)
    # Would be a non-zero crash-loop exit if these restarts wrongly counted
    # against MAX_WORKER_RESTARTS; they must not.
    assert code == 0
    assert len(spawned) == 2 * (len(edits) + 1)


def test_ctrl_c_wins_over_pending_env_restart(monkeypatch, tmp_path):
    """If .env changes while dev is already stopping, no restart is attempted."""
    env_file = tmp_path / ".env"
    env_file.write_text("EXTENSION_ID=abc\n")
    monkeypatch.setattr(dev_mod, "pending_migrations", lambda alias=None: [])
    monkeypatch.setattr(dev_mod, "env_path", lambda: env_file)

    spawned = []

    def fake_spawn(self, cmd, env):
        spawned.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(dev_mod.Command, "_spawn", fake_spawn)
    monkeypatch.setattr(dev_mod.Command, "_pump", lambda self, proc, prefix: None)
    monkeypatch.setattr(dev_mod.Command, "_terminate", lambda self, proc: None)

    state = {"tick": 0}
    real_hash_env_file = dev_mod.hash_env_file

    def fake_hash_env_file(path=None):
        state["tick"] += 1
        if state["tick"] == 2:
            # Simulate a SIGTERM arriving concurrently with the .env change.
            os.kill(os.getpid(), signal.SIGTERM)
            env_file.write_text("EXTENSION_ID=xyz\n")
        return real_hash_env_file(path if path is not None else env_file)

    monkeypatch.setattr(dev_mod, "hash_env_file", fake_hash_env_file)
    monkeypatch.setattr(
        dev_mod.time, "sleep", lambda _s: None
    )

    code = dev_mod.Command().run_supervised("127.0.0.1:8000", [])
    assert code == 0
    assert len(spawned) == 2  # no restart: shutdown won over the env change
