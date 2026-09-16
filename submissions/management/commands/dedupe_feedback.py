"""One-off cleanup of pre-existing exact-duplicate Feedback rows (issue #146).

Groups ``Feedback`` rows by exact
``(note_type, front, back, source_url, decision, reason, was_edited)`` and
deletes every row but the oldest (lowest ``id``) in each group.

Usage:
    uv run python manage.py dedupe_feedback [--dry-run]
"""

from django.core.management.base import BaseCommand

from submissions.models import Feedback

KEY_FIELDS = (
    "note_type",
    "front",
    "back",
    "source_url",
    "decision",
    "reason",
    "was_edited",
)


class Command(BaseCommand):
    help = "Delete exact-duplicate Feedback rows, keeping the oldest per group."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show duplicate groups without deleting anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        groups: dict[tuple, list[Feedback]] = {}
        for fb in Feedback.objects.order_by("id"):
            key = tuple(getattr(fb, field) for field in KEY_FIELDS)
            groups.setdefault(key, []).append(fb)

        dup_groups = {key: rows for key, rows in groups.items() if len(rows) > 1}

        if not dup_groups:
            self.stdout.write("No duplicate Feedback rows found.")
            self.stdout.write(self._summary(0, 0, 0, dry_run))
            return

        n_groups = len(dup_groups)
        n_excess = sum(len(rows) - 1 for rows in dup_groups.values())
        n_kept = n_groups

        to_delete_ids: list[int] = []
        for rows in dup_groups.values():
            kept, *dupes = rows  # ordered by id: first is oldest
            to_delete_ids.extend(fb.id for fb in dupes)
            self.stdout.write(
                f"keep id {kept.id}, delete ids "
                f"{[fb.id for fb in dupes]} "
                f"({kept.decision}) front: {kept.front[:50]!r}"
            )

        if not dry_run:
            Feedback.objects.filter(id__in=to_delete_ids).delete()

        self.stdout.write(self._summary(n_groups, n_excess, n_kept, dry_run))

    @staticmethod
    def _summary(n_groups, n_excess, n_kept, dry_run):
        verb = "would be deleted" if dry_run else "deleted"
        return (
            f"{n_groups} duplicate group(s): "
            f"{n_excess} duplicate row(s) {verb}, {n_kept} kept."
        )
