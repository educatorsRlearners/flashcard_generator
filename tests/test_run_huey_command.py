"""Issue #80: standalone `run_huey` fail-fast migration check.

Mirrors test_pending_migrations_block_dev_startup /
test_no_pending_migrations_starts_normally in tests/test_dev_command.py,
but against the overriding run_huey command instead of dev.
"""

import pytest
from django.core.management import call_command

from submissions.management.commands import dev as dev_mod
from submissions.management.commands import run_huey as run_huey_mod


def test_pending_migrations_block_run_huey_startup(monkeypatch, capsys):
    """Issue #80: unapplied migrations -> non-zero exit, no task consumed."""
    fake_migration = type("Migration", (), {"app_label": "submissions", "name": "0099_fake"})()
    monkeypatch.setattr(
        dev_mod, "pending_migrations", lambda alias=None: [(fake_migration, False)]
    )

    consumed = []
    monkeypatch.setattr(
        run_huey_mod.HueyRunHueyCommand,
        "handle",
        lambda self, *a, **k: consumed.append(True),
    )

    with pytest.raises(SystemExit) as exc_info:
        call_command("run_huey")

    assert exc_info.value.code != 0
    assert consumed == []
    err = capsys.readouterr().err
    assert "submissions.0099_fake" in err


def test_no_pending_migrations_run_huey_starts_normally(monkeypatch):
    """Issue #80: no pending migrations -> normal startup, no new output."""
    monkeypatch.setattr(dev_mod, "pending_migrations", lambda alias=None: [])

    consumed = []
    monkeypatch.setattr(
        run_huey_mod.HueyRunHueyCommand,
        "handle",
        lambda self, *a, **k: consumed.append(True),
    )

    call_command("run_huey")

    assert consumed == [True]
