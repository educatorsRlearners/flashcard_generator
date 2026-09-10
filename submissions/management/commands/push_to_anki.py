"""Push accepted flashcards into Anki via AnkiConnect (issue #11).

Sends every card with ``review_status == accepted`` that has not already
been synced to the single deck named by ``settings.ANKI_DECK_NAME``, tagging
each note with its source URL, ISO date added, and topic. Re-running is safe:
already-synced cards are skipped, so no duplicate notes are created.

Anki must be running with the AnkiConnect add-on installed and listening at
``settings.ANKI_CONNECT_URL`` (default http://127.0.0.1:8765).
"""

from django.core.management.base import BaseCommand, CommandError

from submissions.anki import AnkiUnreachableError, push_accepted_cards


class Command(BaseCommand):
    help = (
        "Push accepted, not-yet-synced flashcards to the configured Anki "
        "deck via AnkiConnect. Idempotent: safe to re-run."
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
