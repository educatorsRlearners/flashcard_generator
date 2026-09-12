"""Push accepted flashcards into Anki via AnkiConnect (issue #11, per-batch deck #76).

Sends every card with ``review_status == accepted`` that has not already
been synced to its batch's stored ``deck_name``, creating decks as needed
and tagging each note with its source URL, ISO date added, and topic.
Re-running is safe: already-synced cards are skipped, so no duplicate
notes are created. Batches with no stored deck (NULL/empty - "not chosen")
are reported skipped and never fall back to ``ANKI_DECK_NAME``.

Anki must be running with the AnkiConnect add-on installed and listening at
``settings.ANKI_CONNECT_URL`` (default http://127.0.0.1:8765).
"""

from django.core.management.base import BaseCommand, CommandError

from submissions.anki import AnkiUnreachableError, push_accepted_cards


class Command(BaseCommand):
    help = (
        "Push accepted, not-yet-synced flashcards to each batch's stored "
        "Anki deck via AnkiConnect. Idempotent: safe to re-run."
    )

    def handle(self, *args, **options):
        try:
            result = push_accepted_cards()
        except AnkiUnreachableError as exc:
            raise CommandError(str(exc))

        for line in result.summary_lines():
            self.stdout.write(line)

        if result.failed_count:
            self.stderr.write(
                self.style.WARNING(
                    f"{result.failed_count} card(s) failed to sync; see above."
                )
            )
