"""Standalone `run_huey` fail-fast migration check (issue #80).

`dev` (issue #79) refuses to start when migrations are pending, but that
check lives entirely in `dev`'s own handle()/run_supervised() and never
runs when `run_huey` is invoked directly (e.g. ``uv run python manage.py
run_huey``, without going through `dev`). This module overrides the
vendored ``huey.contrib.djhuey`` ``run_huey`` command - see the
INSTALLED_APPS ordering comment in config/settings.py: Django resolves a
management command name to the earliest-listed installed app that
provides it, and "submissions" is listed before "huey.contrib.djhuey"
there specifically so this module wins that lookup instead of the
vendored one. The vendored command itself is never edited.

Subclassing (rather than reimplementing) the vendored Command means all
of its consumer/worker/scheduler CLI options keep working unchanged; only
``handle()`` gains a check that runs first.

Reuses `pending_migrations()`/`format_pending_migrations()` from `dev.py`
rather than re-implementing the migration-plan logic (issue #79).
"""

from huey.contrib.djhuey.management.commands.run_huey import Command as HueyRunHueyCommand

from submissions.management.commands import dev as dev_mod


class Command(HueyRunHueyCommand):
    def handle(self, *args, **options):
        # Call through the dev module (rather than importing the functions
        # directly) so tests can monkeypatch dev_mod.pending_migrations the
        # same way tests/test_dev_command.py does.
        plan = dev_mod.pending_migrations()
        if plan:
            self.stderr.write(dev_mod.format_pending_migrations(plan))
            raise SystemExit(1)
        return super().handle(*args, **options)
