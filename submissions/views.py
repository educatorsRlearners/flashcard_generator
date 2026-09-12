import re

from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from .models import Batch, Card, Feedback, SubmittedURL
from .tasks import push_accepted_cards_task


# --- Card review grid (issue #9) --------------------------------------


def _batch_review_cards(batch):
    """Cards from *batch* shown in the review grid, dedup duplicates excluded."""
    return (
        Card.objects.for_review()
        .filter(submitted_url__requests__batch=batch)
        .select_related("submitted_url")
        .distinct()
    )


def _batch_cards_ready(batch):
    """True when the batch has finished extracting + generating cards.

    Not ready => some URL is still ``pending`` extraction, or an extracted
    URL has not had card generation attempted yet, and no cards exist yet.
    Once any card exists for the batch the grid is always shown.
    """
    if _batch_review_cards(batch).exists():
        return True
    urls = SubmittedURL.objects.filter(requests__batch=batch).distinct()
    if not urls.exists():
        return True
    if urls.filter(status=SubmittedURL.Status.PENDING).exists():
        return False
    ok_pending_generation = urls.filter(
        status=SubmittedURL.Status.OK,
        generation_status=SubmittedURL.GenerationStatus.NOT_STARTED,
    )
    return not ok_pending_generation.exists()


def _review_tally(cards):
    agg = cards.aggregate(
        total=Count("id"),
        accepted=Count("id", filter=Q(review_status=Card.ReviewStatus.ACCEPTED)),
        rejected=Count("id", filter=Q(review_status=Card.ReviewStatus.REJECTED)),
    )
    agg["undecided"] = agg["total"] - agg["accepted"] - agg["rejected"]
    return agg


def card_review(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    ready = _batch_cards_ready(batch)
    cards = list(_batch_review_cards(batch)) if ready else []
    tally = _review_tally(_batch_review_cards(batch)) if ready else None
    context = {
        "batch": batch,
        "ready": ready,
        "cards": cards,
        "tally": tally,
    }
    context.update(_deck_picker_context(batch))
    return render(request, "submissions/card_review.html", context)


def _deck_picker_context(batch):
    """Deck-picker context for the Finish form (issue #76).

    Best-effort live ``deckNames`` fetch: when Anki is unreachable the
    dropdown degrades to an unavailable state and the free-text input
    remains usable (Finish with a typed name is never blocked).
    """
    try:
        from .anki import AnkiConnectClient
    except ImportError:
        return {
            "deck_names": [],
            "deck_unavailable": True,
            "stored_deck": (getattr(batch, "deck_name", None) or ""),
        }
    try:
        decks = AnkiConnectClient().invoke("deckNames") or []
        deck_names = sorted(str(d) for d in decks)
        unavailable = False
    except Exception:
        deck_names = []
        unavailable = True
    return {
        "deck_names": deck_names,
        "deck_unavailable": unavailable,
        "stored_deck": (getattr(batch, "deck_name", None) or ""),
    }


def _render_finish_with_deck_error(request, batch, error, attempted=""):
    """Re-render the review page with a visible deck error; nothing enqueued."""
    tally = _review_tally(_batch_review_cards(batch))
    undecided = tally["undecided"]
    context = {
        "batch": batch,
        "ready": True,
        "cards": list(_batch_review_cards(batch)),
        "tally": tally,
        "deck_error": error,
        "stored_deck": attempted,
    }
    if undecided and request.POST.get("confirm") != "1":
        context["confirm_undecided"] = undecided
    context.update(
        {k: v for k, v in _deck_picker_context(batch).items() if k != "stored_deck"}
    )
    context["stored_deck"] = attempted or (getattr(batch, "deck_name", None) or "")
    return render(request, "submissions/card_review.html", context)


@require_POST
def card_review_decision(request, batch_pk, card_pk):
    batch = get_object_or_404(Batch, pk=batch_pk)
    card = get_object_or_404(_batch_review_cards(batch), pk=card_pk)

    decision = request.POST.get("decision", "")
    if decision not in Card.ReviewStatus.values:
        return JsonResponse({"error": "invalid decision"}, status=400)

    card.review_status = decision
    if decision == Card.ReviewStatus.REJECTED:
        card.rejection_reason = request.POST.get("reason", "").strip()
    else:
        # Accept / undecided never carry a reason.
        card.rejection_reason = ""
    card.save(update_fields=["review_status", "rejection_reason"])

    # issue #10: persist a durable, batch-deletion-proof snapshot of the
    # decision. Only accept / reject are recorded (undecided is not feedback).
    # The snapshot reads the card's *current* fields, so inline edits (#24)
    # are what get stored; ``was_edited`` notes that the card was edited.
    if decision in (Card.ReviewStatus.ACCEPTED, Card.ReviewStatus.REJECTED):
        tags = card.tags if isinstance(card.tags, dict) else {}
        Feedback.objects.create(
            note_type=card.note_type,
            front=card.front,
            back=card.back,
            source_url=tags.get("source_url", "") or "",
            decision=decision,
            reason=card.rejection_reason,
            was_edited=card.is_edited,
        )

    payload = {
        "card_id": card.pk,
        "review_status": card.review_status,
        "rejection_reason": card.rejection_reason,
        "tally": _review_tally(_batch_review_cards(batch)),
    }
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(payload)
    return redirect("submissions:card_review", pk=batch_pk)


# --- Inline edit of card text (issue #24) ------------------------------


#: A valid cloze deletion marker: ``{{c1::...}}``, ``{{c2::...}}``, ... with
#: non-empty content. Same rule the #6 generator validates against.
CLOZE_MARKER_RE = re.compile(r"\{\{c\d+::.+?\}\}", re.DOTALL)


def _card_payload(card, batch):
    """Per-card JSON payload for the #24 / #26 fetch endpoints."""
    image_url = ""
    try:
        if card.image:
            image_url = card.image.url
    except (ValueError, AttributeError):
        image_url = ""
    return {
        "card_id": card.pk,
        "note_type": card.note_type,
        "front": card.front,
        "back": card.back,
        "review_status": card.review_status,
        "rejection_reason": card.rejection_reason,
        "is_edited": card.is_edited,
        "edited_at": card.edited_at.isoformat() if card.edited_at else None,
        "image_url": image_url,
        "image_source": card.image_source,
        "image_manually_set": card.image_manually_set,
        "image_placement": card.image_placement,
        "tally": _review_tally(_batch_review_cards(batch)),
    }


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@require_POST
def card_review_edit(request, batch_pk, card_pk):
    """Save an inline text edit for one card (#24).

    Basic cards take ``front`` + ``back``; cloze cards take ``front`` (the
    cloze text, also accepted as ``text``) and keep their existing ``back``.
    Server-validated; the decision state is never touched and only this
    card's row is updated, so other cards' state is preserved.
    """
    batch = get_object_or_404(Batch, pk=batch_pk)
    card = get_object_or_404(_batch_review_cards(batch), pk=card_pk)

    if card.note_type == Card.NoteType.CLOZE:
        front = request.POST.get("front", request.POST.get("text", ""))
        if not CLOZE_MARKER_RE.search(front or ""):
            error = "Cloze text must contain a valid {{cN::...}} deletion."
            if _wants_json(request):
                return JsonResponse({"error": error}, status=400)
            messages.error(request, error)
            return redirect("submissions:card_review", pk=batch_pk)
        if not card.is_edited:
            card.original_front = card.front
            card.original_back = card.back
        card.front = front
        card.is_edited = True
        card.edited_at = timezone.now()
        card.save(
            update_fields=[
                "original_front",
                "original_back",
                "front",
                "is_edited",
                "edited_at",
            ]
        )
    else:
        front = request.POST.get("front", "")
        back = request.POST.get("back", "")
        if not (front or "").strip() or not (back or "").strip():
            error = "Front and back must both be non-empty."
            if _wants_json(request):
                return JsonResponse({"error": error}, status=400)
            messages.error(request, error)
            return redirect("submissions:card_review", pk=batch_pk)
        if not card.is_edited:
            card.original_front = card.front
            card.original_back = card.back
        card.front = front
        card.back = back
        card.is_edited = True
        card.edited_at = timezone.now()
        card.save(
            update_fields=[
                "original_front",
                "original_back",
                "front",
                "back",
                "is_edited",
                "edited_at",
            ]
        )

    if _wants_json(request):
        return JsonResponse(_card_payload(card, batch))
    return redirect("submissions:card_review", pk=batch_pk)


@require_POST
def card_review_revert_edit(request, batch_pk, card_pk):
    """Restore a card's generated text, clearing the edited indicator (#24)."""
    batch = get_object_or_404(Batch, pk=batch_pk)
    card = get_object_or_404(_batch_review_cards(batch), pk=card_pk)

    if card.is_edited:
        card.front = card.original_front
        card.back = card.original_back
        card.original_front = ""
        card.original_back = ""
        card.is_edited = False
        card.edited_at = None
        card.save(
            update_fields=[
                "front",
                "back",
                "original_front",
                "original_back",
                "is_edited",
                "edited_at",
            ]
        )

    if _wants_json(request):
        return JsonResponse(_card_payload(card, batch))
    return redirect("submissions:card_review", pk=batch_pk)


# --- Image replacement from the review grid (issue #26) ----------------


def _snapshot_image_original(card):
    """Retain the #12 auto pick before the first manual image change."""
    if not card.image_manually_set:
        card.original_image = card.image.name or ""
        card.original_image_source = card.image_source or Card.ImageSource.NONE


def _set_card_image(card, data, source):
    """Store *data* (bytes) as the card's zero-or-one image.

    Old files are intentionally left on disk (orphans are out of scope for
    #26) so ``original_image`` always keeps pointing at a valid file.
    """
    from django.core.files.base import ContentFile

    from .images import _extension_for

    filename = f"card_{card.pk}{_extension_for(data)}"
    card.image.save(filename, ContentFile(data), save=False)
    card.image_source = source
    card.image_manually_set = True


def card_review_image_candidates(request, batch_pk, card_pk):
    """List the source-page candidate image URLs for one card (#26).

    Reuses #12's discovery (re-fetch + ``image_candidates``) as-is; the
    thumbnails are the candidate URLs themselves. Usability is validated
    server-side when one is selected.
    """
    from django.conf import settings as dj_settings

    from . import images as images_mod

    batch = get_object_or_404(Batch, pk=batch_pk)
    card = get_object_or_404(_batch_review_cards(batch), pk=card_pk)
    source_url = card.submitted_url.url
    html = images_mod._fetch_page_html(source_url)
    candidates = images_mod.image_candidates(html, source_url)
    return JsonResponse(
        {
            "card_id": card.pk,
            "candidates": candidates,
            "draw_things_enabled": bool(dj_settings.DRAW_THINGS_ENABLED),
        }
    )


@require_POST
def card_review_image_select(request, batch_pk, card_pk):
    """Replace one card's image with a source-page candidate (#26)."""
    from . import images as images_mod

    batch = get_object_or_404(Batch, pk=batch_pk)
    card = get_object_or_404(_batch_review_cards(batch), pk=card_pk)

    candidate_url = (request.POST.get("candidate_url") or "").strip()
    if not candidate_url:
        error = "No candidate image was chosen."
        if _wants_json(request):
            return JsonResponse({"error": error}, status=400)
        messages.error(request, error)
        return redirect("submissions:card_review", pk=batch_pk)
    try:
        fetched = images_mod._fetch_image(candidate_url)
    except Exception as exc:  # noqa: BLE001 - any fetch failure keeps the old image
        error = f"Could not fetch that image ({exc}); kept the previous image."
        if _wants_json(request):
            return JsonResponse({"error": error}, status=400)
        messages.error(request, error)
        return redirect("submissions:card_review", pk=batch_pk)
    if not images_mod.is_usable_image(fetched.content, fetched.content_type):
        error = "That image is not usable (too small or unsupported type); kept the previous image."
        if _wants_json(request):
            return JsonResponse({"error": error}, status=400)
        messages.error(request, error)
        return redirect("submissions:card_review", pk=batch_pk)

    _snapshot_image_original(card)
    _set_card_image(card, fetched.content, Card.ImageSource.SOURCE_PAGE)
    card.save(
        update_fields=[
            "original_image",
            "original_image_source",
            "image",
            "image_source",
            "image_manually_set",
        ]
    )
    if _wants_json(request):
        return JsonResponse(_card_payload(card, batch))
    return redirect("submissions:card_review", pk=batch_pk)


@require_POST
def card_review_image_regenerate(request, batch_pk, card_pk):
    """Generate a fresh Draw Things image for one card (#26).

    Reuses #12's client, prompt builder and timeout as-is. On failure the
    card keeps its previous image (or stays imageless) with an inline
    message; nothing else on the page is touched.
    """
    from django.conf import settings as dj_settings

    from . import images as images_mod

    batch = get_object_or_404(Batch, pk=batch_pk)
    card = get_object_or_404(_batch_review_cards(batch), pk=card_pk)

    if not dj_settings.DRAW_THINGS_ENABLED:
        error = "Draw Things generation is disabled; image unchanged."
        if _wants_json(request):
            return JsonResponse({"error": error}, status=400)
        messages.error(request, error)
        return redirect("submissions:card_review", pk=batch_pk)
    client = images_mod.DrawThingsClient()
    data = client.generate(images_mod._draw_things_prompt(card))
    if not data:
        error = (
            "Draw Things did not return an image (unreachable, error or "
            "empty result); kept the previous image."
        )
        if _wants_json(request):
            return JsonResponse({"error": error}, status=400)
        messages.error(request, error)
        return redirect("submissions:card_review", pk=batch_pk)

    _snapshot_image_original(card)
    _set_card_image(card, data, Card.ImageSource.DRAW_THINGS)
    card.save(
        update_fields=[
            "original_image",
            "original_image_source",
            "image",
            "image_source",
            "image_manually_set",
        ]
    )
    if _wants_json(request):
        return JsonResponse(_card_payload(card, batch))
    return redirect("submissions:card_review", pk=batch_pk)


@require_POST
def card_review_image_remove(request, batch_pk, card_pk):
    """Set one card to no image, retaining the original reference (#26)."""
    batch = get_object_or_404(Batch, pk=batch_pk)
    card = get_object_or_404(_batch_review_cards(batch), pk=card_pk)

    _snapshot_image_original(card)
    card.image = ""
    card.image_source = Card.ImageSource.NONE
    card.image_manually_set = True
    card.save(
        update_fields=[
            "original_image",
            "original_image_source",
            "image",
            "image_source",
            "image_manually_set",
        ]
    )
    if _wants_json(request):
        return JsonResponse(_card_payload(card, batch))
    return redirect("submissions:card_review", pk=batch_pk)


@require_POST
def card_review_image_revert(request, batch_pk, card_pk):
    """Restore the image #12 originally chose for one card (#26)."""
    batch = get_object_or_404(Batch, pk=batch_pk)
    card = get_object_or_404(_batch_review_cards(batch), pk=card_pk)

    if card.image_manually_set:
        card.image = card.original_image.name or ""
        card.image_source = (
            card.original_image_source or Card.ImageSource.NONE
        )
        card.image_manually_set = False
        card.save(
            update_fields=["image", "image_source", "image_manually_set"]
        )
    if _wants_json(request):
        return JsonResponse(_card_payload(card, batch))
    return redirect("submissions:card_review", pk=batch_pk)


@require_POST
def card_review_finish(request, pk):
    try:
        # Engineer A's validator (issue #76); import, don't duplicate.
        from .anki import DeckNameError, resolve_deck_choice, validate_deck_name
    except ImportError:
        # Local fallback when the helper is unavailable: strip + empty
        # check plus the same Anki-illegal rules (empty :: segments,
        # leading/trailing ::, quotes/newlines).
        class DeckNameError(ValueError):
            """A deck name failed server-side validation."""

        def resolve_deck_choice(new="", existing=""):
            typed = str(new or "").strip()
            if typed:
                return typed
            return str(existing or "").strip()

        def validate_deck_name(value):
            stripped = str(value or "").strip()
            if not stripped:
                raise DeckNameError("Choose an Anki deck (dropdown or new name).")
            if len(stripped) > 255:
                raise DeckNameError("Deck name is too long (max 255 characters).")
            if '"' in stripped or "\n" in stripped or "\r" in stripped:
                raise DeckNameError("Deck name must not contain quotes or newlines.")
            if stripped.startswith("::") or stripped.endswith("::"):
                raise DeckNameError('Deck name must not start or end with "::".')
            for segment in stripped.split("::"):
                if not segment.strip():
                    raise DeckNameError('Deck name must not contain empty "::" segments.')
            return stripped

    batch = get_object_or_404(Batch, pk=pk)
    # Deck picker (issue #76): typed free text wins over the dropdown.
    # Missing choice => visible error, nothing pushed, no task enqueued.
    # Anki-unreachable at pick time never blocks a typed name (the deck
    # list is display-only here).
    attempted = resolve_deck_choice(
        request.POST.get("deck_name", ""),
        request.POST.get("deck_choice", ""),
    )
    if not attempted:
        return _render_finish_with_deck_error(
            request, batch, "Choose an Anki deck (dropdown or new name).", ""
        )
    try:
        deck_name = validate_deck_name(attempted)
    except DeckNameError as exc:
        return _render_finish_with_deck_error(request, batch, str(exc), attempted)
    if (getattr(batch, "deck_name", None) or "") != deck_name:
        batch.deck_name = deck_name
        batch.save(update_fields=["deck_name"])
    tally = _review_tally(_batch_review_cards(batch))
    undecided = tally["undecided"]
    if undecided and request.POST.get("confirm") != "1":
        # Ask for an explicit confirm showing the count; nothing is changed.
        confirm_context = {
            "batch": batch,
            "ready": True,
            "cards": list(_batch_review_cards(batch)),
            "tally": tally,
            "confirm_undecided": undecided,
        }
        confirm_context.update(_deck_picker_context(batch))
        return render(request, "submissions/card_review.html", confirm_context)
    messages.success(
        request,
        "Review finished: {accepted} accepted, {rejected} rejected, "
        "{undecided} left undecided.".format(**tally),
    )
    # issue #57: finishing a batch fires a background Anki push. This never
    # blocks the response - the task pushes only this batch's cards to its
    # stored deck_name (issue #76), so it always pushes current DB state.
    # The batch id travels with the call; old no-arg task signatures still
    # work via the TypeError fallback (tasks.py itself is not touched here).
    try:
        push_accepted_cards_task(batch.pk)
    except TypeError:
        push_accepted_cards_task()
    return redirect("submissions:card_review", pk=pk)
