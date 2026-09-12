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

Media sync (issue #22)
----------------------
A card with ``image`` set gets its bytes uploaded with the AnkiConnect
``storeMediaFile`` action (base64 ``data`` form) *before* ``addNote``, and
the note field carries ``<img src="FILENAME">`` with the bare stored
filename. Filename: ``flashcard-<sha256(data)[:32]>.<ext>`` - deterministic
(same bytes -> same name), collision-safe (sha256), safe charset
``[a-z0-9._-]`` and bounded length (<= ~50 chars).

Skip rule (documented choice): within one :func:`push_accepted_cards` run a
filename uploaded once is never uploaded again (in-memory ``set``), and
before uploading we call ``retrieveMediaFile`` for that filename - if Anki
already stores identical bytes the ``storeMediaFile`` is skipped and the
note still references the file. So pushing the same card twice, or two
cards sharing identical image bytes, yields one stored file.

Failure choice (documented choice): a ``storeMediaFile`` failure
(:class:`AnkiConnectError`) fails the *whole card push* - the note is NOT
added with a broken reference, the card stays unsynced (``synced_at`` null)
so a re-run retries it, and the failure is recorded per-card in
``PushResult.failed``. Already-pushed cards in the same batch are
unaffected (per-card isolation, same as a per-note ``addNote`` error).

Live-deck semantic dedup (issue #29)
------------------------------------
:func:`dedup_cards_against_anki` compares freshly generated cards against
the notes currently in the target Anki deck, fetched live via AnkiConnect
(``findNotes`` + ``notesInfo``) once per deck and cached in-process
(:data:`_DECK_NOTES_CACHE`), never once per card. Cosine similarity reuses
:data:`submissions.dedup.DEDUP_SIMILARITY_THRESHOLD` - the single source of
truth, not re-tuned here. Near-duplicates are marked
``dedup_status=duplicate`` (``duplicate_of`` stays null - there is no local
card to point at) with ``similarity_score`` set, and the matched Anki note
is returned in :class:`AnkiDedupResult.matches`. When Anki is unreachable
the function returns a ``warning`` and leaves the cards untouched
(local-only fallback); generation always completes.

Display note (follow-up, views/templates): the matched note is currently
surfaced at command-output level only - :func:`dedup_cards_against_anki`
returns it, :mod:`submissions.generation` stores it on
``GenerationResult.anki_matches`` and prints it in ``summary_line()``.
Showing the matched note inline in the review grid (views/templates) is an
explicit documented follow-up and is NOT part of this module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import logging
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from .models import Card

logger = logging.getLogger(__name__)

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


def build_note(card: Card, deck_name: str, image_filename: str | None = None) -> dict:
    model_name, fields_fn = FIELD_MAP[card.note_type]
    fields = fields_fn(card)
    if image_filename:
        tag = f'<img src="{image_filename}">'
        if card.note_type == Card.NoteType.CLOZE:
            fields["Text"] = f'{fields.get("Text", "")}<br>{tag}' if fields.get("Text") else tag
        else:
            fields["Back"] = f'{fields.get("Back", "")}<br>{tag}' if fields.get("Back") else tag
    return {
        "deckName": deck_name,
        "modelName": model_name,
        "fields": fields,
        "tags": card_tags(card),
        "options": {"allowDuplicate": False},
    }


# --- media sync (issue #22) ---------------------------------------------

#: Prefix for every media filename we store in Anki.
MEDIA_FILENAME_PREFIX = "flashcard-"
#: Hex chars of the sha256 digest kept in the filename (128 bits - far
#: beyond collision relevance, keeps the name short).
MEDIA_FILENAME_HASH_LEN = 32
#: Fallback extension when the card image name carries no usable suffix.
MEDIA_DEFAULT_EXTENSION = "png"


def media_filename_for_bytes(data: bytes, extension: str = MEDIA_DEFAULT_EXTENSION) -> str:
    """Deterministic, collision-safe, charset-safe media filename.

    ``flashcard-<sha256(data)[:32]>.<ext>`` where ``ext`` is lowercased
    alphanumeric (max 8 chars, default ``png``). Safe charset
    ``[a-z0-9._-]``, bounded length (<= ``len(prefix)+32+1+8``).
    """
    ext = re.sub(r"[^a-z0-9]", "", str(extension or "").lower())[:8] or MEDIA_DEFAULT_EXTENSION
    digest = hashlib.sha256(bytes(data)).hexdigest()[:MEDIA_FILENAME_HASH_LEN]
    return f"{MEDIA_FILENAME_PREFIX}{digest}.{ext}"


def _card_image_extension(card: Card) -> str:
    name = getattr(getattr(card, "image", None), "name", "") or ""
    suffix = name.rsplit(".", 1)[-1] if "." in name else ""
    cleaned = re.sub(r"[^a-z0-9]", "", suffix.lower())[:8]
    return cleaned or MEDIA_DEFAULT_EXTENSION


def card_image_bytes(card: Card) -> bytes | None:
    """Raw bytes of ``card.image``, or ``None`` when the card has no image.

    :raises OSError: when the field is set but the file cannot be read -
        the caller turns this into a per-card (retryable) failure.
    """
    image = getattr(card, "image", None)
    if not image:
        return None
    try:
        if not image.name:
            return None
    except Exception:
        return None
    image.open("rb")
    try:
        data = image.read()
    finally:
        try:
            image.close()
        except Exception:
            pass
    if not data:
        return None
    return bytes(data)


def ensure_media_uploaded(
    client: AnkiConnectClient,
    filename: str,
    data: bytes,
    uploaded: set[str] | None = None,
) -> bool:
    """Store ``data`` under ``filename`` unless Anki already holds it.

    Returns True when a ``storeMediaFile`` was issued, False when skipped
    (already uploaded this run, or ``retrieveMediaFile`` returned identical
    bytes). Raises :class:`AnkiConnectError` when the store fails.
    """
    if uploaded is not None and filename in uploaded:
        return False
    try:
        existing = client.invoke("retrieveMediaFile", filename=filename)
    except AnkiConnectError:
        existing = None  # not present (or not readable) - fall through to store
    if existing:
        try:
            if base64.b64decode(existing) == bytes(data):
                if uploaded is not None:
                    uploaded.add(filename)
                return False
        except (binascii.Error, ValueError, TypeError):
            pass  # undecodable - fall through and (over)write
    client.invoke(
        "storeMediaFile", filename=filename, data=base64.b64encode(bytes(data)).decode("ascii")
    )
    if uploaded is not None:
        uploaded.add(filename)
    return True


# --- deck-name validation (issue #76) -------------------------------------

#: Max deck-name length, mirrors ``Batch.deck_name`` (the model is the
#: single source of truth; this constant is only the non-model fallback).
MAX_DECK_NAME_LENGTH = 255


class DeckNameError(ValueError):
    """A deck name failed server-side validation."""


def deck_name_max_length() -> int:
    """Max allowed deck-name length (from the ``Batch`` model field)."""
    try:
        field = Card._meta.get_field("batch").related_model._meta.get_field(
            "deck_name"
        )
        return int(field.max_length or MAX_DECK_NAME_LENGTH)
    except Exception:
        return MAX_DECK_NAME_LENGTH


def validate_deck_name(value: object) -> str:
    """Strip + validate *value* as an Anki deck name; return the stripped name.

    Rules: non-empty after stripping, within the model max length, no
    embedded quote / newline / carriage return, no empty ``::`` segments
    (covers leading / trailing ``::`` and ``::::``), and no blank segment
    around ``::``. The stored value is used verbatim for ``deckName`` /
    ``deck:"..."`` queries. Raises :class:`DeckNameError` on violation.
    """
    stripped = str(value or "").strip()
    if not stripped:
        raise DeckNameError("Choose an Anki deck (dropdown or new name).")
    if len(stripped) > deck_name_max_length():
        raise DeckNameError(
            f"Deck name is too long (max {deck_name_max_length()} characters)."
        )
    if '"' in stripped or "\n" in stripped or "\r" in stripped:
        raise DeckNameError('Deck name must not contain quotes or newlines.')
    if stripped.startswith("::") or stripped.endswith("::"):
        raise DeckNameError('Deck name must not start or end with "::".')
    for segment in stripped.split("::"):
        if not segment.strip():
            raise DeckNameError('Deck name must not contain empty "::" segments.')
    return stripped


def resolve_deck_choice(new: object = "", existing: object = "") -> str:
    """Resolve the picker choice: typed free text wins over the dropdown.

    Returns the stripped choice ("" when neither is given); validation is
    the caller's job via :func:`validate_deck_name`.
    """
    typed = str(new or "").strip()
    if typed:
        return typed
    return str(existing or "").strip()


def normalize_deck_name(value: object) -> str:
    """Strip *value* to its stored-deck form ("" when None / blank).

    No validation - use :func:`validate_deck_name` when the name must be
    Anki-legal (e.g. reviewer input). Used wherever a stored ``deck_name``
    is read back for grouping / querying.
    """
    return str(value or "").strip()


def escape_deck_query(deck_name: str) -> str:
    """Escape *deck_name* for a ``deck:"..."`` AnkiConnect query."""
    return str(deck_name or "").replace("\\", "\\\\").replace('"', '\\"')

_TAG_RE = re.compile(r"<[^>]+>")

#: In-process cache: deck name -> list of {"noteId", "text"} dicts fetched
#: via ``findNotes`` + ``notesInfo``. Populated once per batch; cleared by
#: :func:`clear_deck_notes_cache` (tests) or process restart.
_DECK_NOTES_CACHE: dict[str, list[dict]] = {}


def clear_deck_notes_cache() -> None:
    """Empty the live-deck note-text cache (tests / deck switched)."""
    _DECK_NOTES_CACHE.clear()


def _strip_html(value: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", str(value or ""))).strip()


def deck_note_text(note_info: dict) -> str:
    """Plain text of one ``notesInfo`` entry (all fields joined)."""
    fields = (note_info or {}).get("fields") or {}
    parts = []
    for details in fields.values():
        if isinstance(details, dict):
            text = _strip_html(details.get("value", ""))
        else:
            text = _strip_html(details)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def fetch_deck_note_texts(
    client: AnkiConnectClient, deck_name: str, *, use_cache: bool = True
) -> tuple[list[dict], bool]:
    """Fetch ``[{"noteId", "text"}]`` for every note in ``deck_name``.

    Exactly two AnkiConnect calls (``findNotes`` + ``notesInfo``) on a cache
    miss; zero calls on a hit. Returns ``(notes, from_cache)``.
    """
    if use_cache and deck_name in _DECK_NOTES_CACHE:
        return _DECK_NOTES_CACHE[deck_name], True
    note_ids = client.invoke("findNotes", query=f'deck:"{escape_deck_query(deck_name)}"') or []
    if not note_ids:
        notes: list[dict] = []
        if use_cache:
            _DECK_NOTES_CACHE[deck_name] = notes
        return notes, False
    infos = client.invoke("notesInfo", notes=list(note_ids)) or []
    notes = []
    for info in infos:
        text = deck_note_text(info)
        if not text:
            continue
        notes.append({"noteId": info.get("noteId"), "text": text})
    if use_cache:
        _DECK_NOTES_CACHE[deck_name] = notes
    return notes, False


@dataclass
class AnkiDedupMatch:
    card: Card
    note_id: object = None
    note_text: str = ""
    similarity: float = 0.0


@dataclass
class AnkiDedupResult:
    """Outcome of :func:`dedup_cards_against_anki`."""

    matches: list = field(default_factory=list)  # [AnkiDedupMatch]
    deck_notes: int = 0
    from_cache: bool = False
    warning: str = ""  # non-empty on the unreachable/local-only fallback

    @property
    def duplicates(self) -> int:
        return len(self.matches)


def _anki_card_text(card: Card) -> str:
    parts = [card.front or "", card.back or "", card.source_term or ""]
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()


def dedup_cards_against_anki(
    cards: list[Card],
    client: AnkiConnectClient | None = None,
    deck_name: str | None = None,
    model=None,
    *,
    use_cache: bool = True,
) -> AnkiDedupResult:
    """Mark cards duplicating notes already in the Anki deck (issue #29).

    Only cards still ``dedup_status == unique`` with non-empty text are
    compared; local ``duplicate`` verdicts are never overridden. Deck text
    is fetched once per deck (cached); embeddings are computed in one
    ``model.encode`` call. Similarity uses
    :data:`submissions.dedup.DEDUP_SIMILARITY_THRESHOLD`.

    The deck is the batch's stored ``deck_name`` (issue #76) - never
    ``settings.ANKI_DECK_NAME``. When *deck_name* is None it is inferred
    from the cards' ``batch`` link; a missing / empty deck skips live dedup
    with a warning (local-only applies) instead of checking the default deck.
    """
    from submissions.dedup import DEDUP_SIMILARITY_THRESHOLD, load_embedding_model

    candidates = [
        c
        for c in list(cards)
        if getattr(c, "dedup_status", Card.DedupStatus.UNIQUE) == Card.DedupStatus.UNIQUE
        and _anki_card_text(c)
    ]
    if not candidates:
        return AnkiDedupResult()
    deck = deck_name
    if deck is None:
        first_batch = getattr(candidates[0], "batch", None)
        deck = getattr(first_batch, "deck_name", None)
    deck = normalize_deck_name(deck)
    if not deck:
        warning = (
            "Anki deck dedup skipped (no deck chosen for this batch); "
            "local-only dedup applied."
        )
        logger.warning("%s", warning)
        return AnkiDedupResult(warning=warning)
    client = client or AnkiConnectClient()
    try:
        deck_notes, from_cache = fetch_deck_note_texts(client, deck, use_cache=use_cache)
    except AnkiError as exc:
        warning = f"Anki deck dedup skipped (Anki unreachable: {exc}); local-only dedup applied."
        logger.warning("%s", warning)
        return AnkiDedupResult(warning=warning)
    result = AnkiDedupResult(deck_notes=len(deck_notes), from_cache=from_cache)
    if not deck_notes:
        return result
    model = model or load_embedding_model()
    import numpy as np

    texts = [n["text"] for n in deck_notes] + [_anki_card_text(c) for c in candidates]
    vectors = np.asarray(model.encode(texts), dtype=float)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors = vectors / norms
    deck_matrix, card_matrix = vectors[: len(deck_notes)], vectors[len(deck_notes):]
    for card, vec in zip(candidates, card_matrix):
        sims = deck_matrix @ vec
        best_idx = int(np.argmax(sims))
        best = float(sims[best_idx])
        if best >= DEDUP_SIMILARITY_THRESHOLD:
            note = deck_notes[best_idx]
            card.dedup_status = Card.DedupStatus.DUPLICATE
            card.duplicate_of = None
            card.similarity_score = best
            card.save(update_fields=["dedup_status", "duplicate_of", "similarity_score"])
            result.matches.append(
                AnkiDedupMatch(
                    card=card, note_id=note["noteId"], note_text=note["text"], similarity=best
                )
            )
    return result


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


def accepted_unsynced_cards(batch=None):
    """Accepted + not-yet-synced cards, optionally scoped to one batch.

    The batch scope matches the review grid (``Card.batch`` or any
    ``BatchRequest`` for the batch), so Finish pushes exactly what the
    reviewer saw.
    """
    from django.db.models import Q

    qs = Card.objects.filter(
        review_status=Card.ReviewStatus.ACCEPTED,
        synced_at__isnull=True,
    )
    if batch is not None:
        batch_id = getattr(batch, "pk", batch)
        qs = qs.filter(
            Q(batch_id=batch_id) | Q(submitted_url__requests__batch_id=batch_id)
        ).distinct()
    return qs.select_related("submitted_url").order_by("pk")


def _push_cards_to_deck(client, deck_name, cards, result, media_uploaded) -> None:
    """Push *cards* to *deck_name*, recording into *result* (per-card isolation)."""
    for card in cards:
        try:
            image_data = card_image_bytes(card)
        except OSError as exc:
            result.failed.append((card.pk, f"could not read card image: {exc}"))
            continue
        image_filename: str | None = None
        if image_data is not None:
            image_filename = media_filename_for_bytes(image_data, _card_image_extension(card))
            try:
                ensure_media_uploaded(client, image_filename, image_data, media_uploaded)
            except AnkiConnectError as exc:
                result.failed.append(
                    (card.pk, f"media upload failed for {image_filename}: {exc}")
                )
                continue
        note = build_note(card, deck_name, image_filename=image_filename)
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


def _ensure_deck(client, deck_name, result) -> None:
    existing_decks = client.invoke("deckNames") or []
    if deck_name not in existing_decks:
        client.invoke("createDeck", deck=deck_name)
        result.deck_created = True


def push_batch_accepted_cards(batch, client: AnkiConnectClient | None = None) -> PushResult:
    """Push one batch's accepted+unsynced cards to its stored ``deck_name``.

    Creates the deck when missing. A batch with no stored deck (NULL/empty)
    is never pushed and never falls back to ``ANKI_DECK_NAME`` - an empty
    :class:`PushResult` (``deck_name == ""``) is returned and the cards stay
    unsynced. Raises :class:`AnkiUnreachableError` before touching any card
    when Anki cannot be contacted.
    """
    from .models import Batch as BatchModel

    client = client or AnkiConnectClient()
    if not isinstance(batch, BatchModel):
        batch = BatchModel.objects.filter(pk=getattr(batch, "pk", batch)).first()
    deck_name = normalize_deck_name(getattr(batch, "deck_name", None))
    result = PushResult(deck_name=deck_name, url=client.url)
    if batch is None or not deck_name:
        return result
    batch_id = batch.pk
    from django.db.models import Q as _Q

    result.skipped_already_synced = Card.objects.filter(
        review_status=Card.ReviewStatus.ACCEPTED,
        synced_at__isnull=False,
    ).filter(
        _Q(batch_id=batch_id) | _Q(submitted_url__requests__batch_id=batch_id),
    ).distinct().count()
    cards = list(accepted_unsynced_cards(batch=batch))
    if not cards:
        return result
    # Connectivity check + deck creation. An AnkiUnreachableError here
    # propagates with nothing marked synced.
    _ensure_deck(client, deck_name, result)
    _push_cards_to_deck(client, deck_name, cards, result, set())
    return result


@dataclass
class MultiPushResult:
    """Grouped outcome of pushing every deck-assigned batch (CLI / all-batch path)."""

    results: list = field(default_factory=list)  # [PushResult], one per deck
    skipped_no_deck: int = 0
    url: str = ""

    @property
    def added_count(self) -> int:
        return sum(len(r.added) for r in self.results)

    @property
    def failed_count(self) -> int:
        return sum(len(r.failed) for r in self.results)

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        for result in self.results:
            lines.extend(result.summary_lines())
        if self.skipped_no_deck:
            lines.append(
                f"skipped {self.skipped_no_deck} card(s) with no deck chosen (not pushed)"
            )
        else:
            lines.append("skipped 0 card(s) with no deck chosen")
        return lines


def push_all_deck_batches(client: AnkiConnectClient | None = None) -> MultiPushResult:
    """Push every batch with a stored deck, grouped per deck (CLI path).

    Batches with NULL/empty ``deck_name`` are counted in
    ``skipped_no_deck`` and never pushed. Cards are grouped by stored deck
    name so two batches with different decks land in two decks. Raises
    :class:`AnkiUnreachableError` with nothing marked synced when Anki is
    unreachable.
    """
    from .models import Batch as BatchModel

    client = client or AnkiConnectClient()
    grouped = MultiPushResult(url=client.url)
    deck_to_cards: dict[str, list] = {}
    deck_order: list[str] = []
    for card in accepted_unsynced_cards():
        deck = ""
        batch = getattr(card, "batch", None)
        if batch is not None:
            try:
                deck = normalize_deck_name(getattr(batch, "deck_name", ""))
            except Exception:
                deck = ""
        if not deck:
            grouped.skipped_no_deck += 1
            continue
        if deck not in deck_to_cards:
            deck_to_cards[deck] = []
            deck_order.append(deck)
        deck_to_cards[deck].append(card)
    if not deck_to_cards:
        return grouped
    # One connectivity check up front so unreachable pushes nothing.
    existing_decks = client.invoke("deckNames") or []
    media_uploaded: set[str] = set()
    for deck in deck_order:
        result = PushResult(deck_name=deck, url=client.url)
        if deck not in existing_decks:
            client.invoke("createDeck", deck=deck)
            result.deck_created = True
            existing_decks.append(deck)
        _push_cards_to_deck(client, deck, deck_to_cards[deck], result, media_uploaded)
        grouped.results.append(result)
    return grouped


def push_accepted_cards(
    client: AnkiConnectClient | None = None, batch=None, batch_id=None
) -> PushResult | MultiPushResult:
    """Push accepted+unsynced cards to their batch's stored deck (issue #76).

    * With ``batch`` / ``batch_id``: pushes only that batch (see
      :func:`push_batch_accepted_cards`).
    * Without either: pushes every deck-assigned batch grouped per deck
      (see :func:`push_all_deck_batches`); deck-less cards are skipped,
      never sent to ``ANKI_DECK_NAME``.

    ``ANKI_DECK_NAME`` is never used as a push target.
    """
    if batch is None and batch_id is not None:
        batch = batch_id
    if batch is not None:
        return push_batch_accepted_cards(batch, client=client)
    # Legacy / unscoped call: group across batches by stored deck.
    return push_all_deck_batches(client=client)
