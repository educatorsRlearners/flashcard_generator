"""Issue #20: dev supervisor (runserver + run_huey together).

No live processes are spawned here: handle()/run_supervised() are tested
with fakes, and Popen is asserted never to run under pytest.
"""

import os

from django.core.management import call_command

from submissions.management.commands import dev as dev_mod


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
