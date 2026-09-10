"""AnkiConnect transport + the accepted-card push (issue #11).

Anki is talked to *only* through the AnkiConnect JSON HTTP API
(https://foosoft.net/projects/anki-connect/) using the standard library -
``urllib.request`` + ``json``, no third-party HTTP client.

Two layers live here:

* :class:`AnkiConnectClient` - the transport. One method, :meth:`invoke`,
  which POSTs ``{"action", "version", "params"}`` to a single endpoint and
  returns the ``result`` (or raises). It is deliberately tiny and is the
  seam tests replace with a fake - no live Anki needed.
* :func:`push_accepted_cards` - the orchestration: select the cards, make
  the deck, add one note per card, record ``anki_note_id`` / ``synced_at``,
  and return a :class:`PushResult` with added / skipped / failed counts.

Idempotency: a card with a non-null ``synced_at`` is never sent again, so
re-running the push adds zero new notes for already-synced cards.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from .models import Card

ANKI_CONNECT_VERSION = 6


class AnkiError(Exception):
    """Base class for every Anki push failure."""


class AnkiUnreachableError(AnkiError):
    """Anki / AnkiConnect could not be contacted at all (refused, timeout,
    DNS, malformed response). The whole push aborts and nothing is marked
    synced."""


class AnkiConnectError(AnkiError):
    """AnkiConnect accepted the request but returned an ``error`` string -
    e.g. a duplicate note or an unknown note type. Raised per call so one
    bad note does not abort the batch."""


class AnkiConnectClient:
    """Minimal AnkiConnect HTTP transport.

    ``url`` / ``timeout`` default to the Django settings so the call site
    never hard-codes them.
    """

    def __init__(self, url: str | None = None, timeout: float | None = None):
        self.url = url or settings.ANKI_CONNECT_URL
        self.timeout = (
            timeout
            if timeout is not None
            else getattr(settings, "ANKI_CONNECT_TIMEOUT", 10)
        )

    def invoke(self, action: str, **params):
        payload = json.dumps(
            {
                "action": action,
                "version": ANKI_CONNECT_VERSION,
                "params": params,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise AnkiUnreachableError(
                f"Could not reach AnkiConnect at {self.url} ({reason}). "
                "Is Anki running with the AnkiConnect add-on installed?"
            ) from exc
        except json.JSONDecodeError as exc:
            raise AnkiUnreachableError(
                f"AnkiConnect at {self.url} returned a non-JSON response; "
                "check that the URL points at AnkiConnect."
            ) from exc

        if not isinstance(body, dict) or "error" not in body or "result" not in body:
            raise AnkiUnreachableError(
                f"AnkiConnect at {self.url} returned an unexpected payload: {body!r}"
            )
        if body["error"] is not None:
            raise AnkiConnectError(str(body["error"]))
        return body["result"]


# --- note construction -------------------------------------------------

FIELD_MAP = {
    Card.NoteType.BASIC: ("Basic", lambda c: {"Front": c.front, "Back": c.back}),
    Card.NoteType.CLOZE: ("Cloze", lambda c: {"Text": c.front, "Back Extra": c.back}),
}


def _sanitize_tag(value: str) -> str:
    """Anki splits tags on whitespace, so collapse it to underscores."""
    return "_".join(str(value).split())


def card_tags(card: Card) -> list[str]:
    """The tags a pushed note carries: source URL, ISO date added, topic.

    Values come from the card's own ``tags`` JSON dict (written by the #6
    generator) with sensible fallbacks so a tag is always present.
    """
    meta = card.tags if isinstance(card.tags, dict) else {}
    source_url = meta.get("source_url") or card.submitted_url.url
    date_added = meta.get("date_added") or card.created_at.date().isoformat()
    topic = meta.get("topic") or card.source_term
    return [
        _sanitize_tag(f"source:{source_url}"),
        _sanitize_tag(f"added:{date_added}"),
        _sanitize_tag(f"topic:{topic}"),
    ]


def build_note(card: Card, deck_name: str) -> dict:
    model_name, fields_fn = FIELD_MAP[card.note_type]
    return {
        "deckName": deck_name,
        "modelName": model_name,
        "fields": fields_fn(card),
        "tags": card_tags(card),
        "options": {"allowDuplicate": False},
    }


# --- push orchestration ----------------------------------------------


@dataclass
class PushResult:
    deck_name: str
    url: str
    added: list = field(default_factory=list)          # [(card_id, note_id)]
    skipped_duplicate: list = field(default_factory=list)  # [(card_id, reason)]
    skipped_already_synced: int = 0
    failed: list = field(default_factory=list)          # [(card_id, reason)]
    deck_created: bool = False

    @property
    def added_count(self) -> int:
        return len(self.added)

    @property
    def skipped_count(self) -> int:
        return self.skipped_already_synced + len(self.skipped_duplicate)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def summary_lines(self) -> list[str]:
        lines = [
            f'Anki deck "{self.deck_name}" at {self.url}'
            + (" (created)" if self.deck_created else ""),
            f"added {self.added_count}, "
            f"skipped {self.skipped_count} "
            f"({self.skipped_already_synced} already synced, "
            f"{len(self.skipped_duplicate)} duplicate), "
            f"failed {self.failed_count}",
        ]
        for card_id, reason in self.skipped_duplicate:
            lines.append(f"  skipped-duplicate card {card_id}: {reason}")
        for card_id, reason in self.failed:
            lines.append(f"  failed card {card_id}: {reason}")
        return lines


def accepted_unsynced_cards():
    return Card.objects.filter(
        review_status=Card.ReviewStatus.ACCEPTED,
        synced_at__isnull=True,
    ).select_related("submitted_url").order_by("pk")


def push_accepted_cards(client: AnkiConnectClient | None = None) -> PushResult:
    """Push every ``accepted`` + not-yet-synced card to the configured deck.

    Raises :class:`AnkiUnreachableError` (before touching any card) if Anki
    cannot be contacted. A per-note AnkiConnect error is caught: that card
    is reported failed (or skipped-duplicate) and the batch continues.
    """
    client = client or AnkiConnectClient()
    deck_name = settings.ANKI_DECK_NAME
    result = PushResult(deck_name=deck_name, url=client.url)

    result.skipped_already_synced = Card.objects.filter(
        review_status=Card.ReviewStatus.ACCEPTED,
        synced_at__isnull=False,
    ).count()

    cards = list(accepted_unsynced_cards())
    if not cards:
        return result

    # Connectivity check + deck creation. An AnkiUnreachableError here
    # propagates with nothing marked synced.
    existing_decks = client.invoke("deckNames") or []
    if deck_name not in existing_decks:
        client.invoke("createDeck", deck=deck_name)
        result.deck_created = True

    for card in cards:
        note = build_note(card, deck_name)
        try:
            note_id = client.invoke("addNote", note=note)
        except AnkiConnectError as exc:
            reason = str(exc)
            if "duplicate" in reason.lower():
                result.skipped_duplicate.append((card.pk, reason))
            else:
                result.failed.append((card.pk, reason))
            continue

        card.anki_note_id = note_id
        card.synced_at = timezone.now()
        card.save(update_fields=["anki_note_id", "synced_at"])
        result.added.append((card.pk, note_id))

    return result
