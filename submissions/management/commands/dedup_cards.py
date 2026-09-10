"""Standalone semantic dedup of generated cards (issue #7).

Thin CLI wrapper over :func:`submissions.dedup.dedup_cards`: it selects
cards, (re)computes their embeddings and dedup status, and prints one line
per card saying whether it is ``unique`` or a ``duplicate`` (and of which
card).

Selectors (mutually exclusive, one required):

* ``--batch <pk>`` - every card whose ``batch`` is this batch
* ``--id <pk>``    - the single card with this primary key
* ``--all``        - every card in the database

Flags:

* ``--force``              - recompute embeddings even if cached on the row
* ``--include-duplicates`` - also re-check cards already marked ``duplicate``
                             (by default those are left as they are)

A model-load failure prints the one-time download step and exits non-zero
without marking anything.
"""

from django.core.management.base import BaseCommand, CommandError

from submissions import dedup
from submissions.models import Card


class Command(BaseCommand):
    help = "(Re)compute local semantic dedup status for generated cards."

    def add_arguments(self, parser):
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument("--batch", type=int, help="Cards in this batch.")
        selector.add_argument("--id", type=int, help="The single card with this pk.")
        selector.add_argument(
            "--all", action="store_true", help="Every card in the database."
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Recompute embeddings even when a cached vector exists.",
        )
        parser.add_argument(
            "--include-duplicates",
            action="store_true",
            help="Also re-check cards already marked duplicate.",
        )

    def handle(self, *args, **options):
        cards = self._select(options)
        if not cards:
            self.stdout.write("No matching cards.")
            return

        try:
            summary = dedup.dedup_cards(cards, force=options["force"])
        except dedup.ModelLoadError as exc:
            raise CommandError(str(exc))

        for result in summary.results:
            self.stdout.write(result.line())
        self.stdout.write(
            f"{len(summary.results)} card(s): "
            f"{summary.unique} unique, {summary.duplicates} duplicate."
        )

    def _select(self, options):
        qs = Card.objects.all().order_by("pk")
        if options["id"]:
            qs = qs.filter(pk=options["id"])
            if not qs.exists():
                raise CommandError(f"No Card with id {options['id']}")
        elif options["batch"] is not None:
            qs = qs.filter(batch_id=options["batch"])
        # --all: no extra filter.

        if not options["include_duplicates"]:
            qs = qs.exclude(dedup_status=Card.DedupStatus.DUPLICATE)
        return list(qs)
