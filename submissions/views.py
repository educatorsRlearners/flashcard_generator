import re
from datetime import timedelta

from django.contrib import messages
from django.db.models import Avg, Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate, TruncHour
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import date as date_filter
from django.template.defaultfilters import floatformat
from django.views.decorators.http import require_POST
from django.utils import timezone

from .models import Batch, Card, Feedback, LLMCall, SubmittedURL
from .tasks import push_accepted_cards_task


# --- LLM usage dashboard (issue #88) -----------------------------------

#: window key -> timedelta used to compute the window start against
#: ``timezone.now()`` (UTC; see ``config/settings.py``'s ``TIME_ZONE`` /
#: ``USE_TZ``). Order matters for the page's window links.
LLM_USAGE_WINDOWS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
LLM_USAGE_DEFAULT_WINDOW = "7d"

_ZERO_COST = DecimalField(max_digits=12, decimal_places=6)


def _llm_usage_context(window_key):
    """Build the aggregate context for the ``llm_usage`` dashboard.

    Shared by the HTML render and the JSON polling endpoint (issue #89)
    so both always reflect the exact same query/aggregation logic.

    Everything shown is computed with database aggregation
    (``Sum``/``Count``/``Avg``/``annotate().values()``) rather than by
    iterating matching rows in Python, so the page stays cheap regardless
    of how many ``LLMCall`` rows exist in the selected window.
    """
    if window_key not in LLM_USAGE_WINDOWS:
        window_key = LLM_USAGE_DEFAULT_WINDOW
    window_delta = LLM_USAGE_WINDOWS[window_key]
    now = timezone.now()
    window_start = now - window_delta

    calls = LLMCall.objects.filter(created_at__gte=window_start)

    totals = calls.aggregate(
        total_calls=Count("id"),
        total_cost=Coalesce(Sum("estimated_cost_usd"), Value(0), output_field=_ZERO_COST),
        failed_calls=Count("id", filter=Q(status=LLMCall.Status.FAILED)),
        avg_latency_ms=Avg("latency_ms"),
    )
    total_calls = totals["total_calls"]
    failed_calls = totals["failed_calls"]
    failure_rate = round((failed_calls / total_calls * 100), 1) if total_calls else 0.0
    avg_latency_ms = totals["avg_latency_ms"] or 0

    #: Per-model breakdown, blank model folded into an explicit
    #: "(unknown model)" label so it sorts/groups instead of being dropped.
    by_model = list(
        calls.values("model")
        .annotate(
            call_count=Count("id"),
            total_cost=Coalesce(Sum("estimated_cost_usd"), Value(0), output_field=_ZERO_COST),
            failed_count=Count("id", filter=Q(status=LLMCall.Status.FAILED)),
            avg_latency_ms=Avg("latency_ms"),
        )
        .order_by("-total_cost")
    )
    for row in by_model:
        row["model_display"] = row["model"] or "(unknown model)"
        row["avg_latency_ms"] = row["avg_latency_ms"] or 0

    #: Failed-call breakdown by error_class, descending by count.
    by_error_class = list(
        calls.filter(status=LLMCall.Status.FAILED)
        .values("error_class")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    for row in by_error_class:
        row["error_class_display"] = row["error_class"] or "(unknown error)"

    #: Time-bucketed trend: hourly for 24h, daily (UTC calendar day)
    #: otherwise.
    if window_key == "24h":
        trunc = TruncHour("created_at")
    else:
        trunc = TruncDate("created_at")
    trend = list(
        calls.annotate(bucket=trunc)
        .values("bucket")
        .annotate(
            call_count=Count("id"),
            total_cost=Coalesce(Sum("estimated_cost_usd"), Value(0), output_field=_ZERO_COST),
            avg_latency_ms=Avg("latency_ms"),
        )
        .order_by("bucket")
    )
    for row in trend:
        row["avg_latency_ms"] = row["avg_latency_ms"] or 0

    return {
        "window": window_key,
        "windows": list(LLM_USAGE_WINDOWS.keys()),
        "window_start": window_start,
        "total_calls": total_calls,
        "total_cost": totals["total_cost"],
        "failed_calls": failed_calls,
        "failure_rate": failure_rate,
        "avg_latency_ms": avg_latency_ms,
        "by_model": by_model,
        "by_error_class": by_error_class,
        "trend": trend,
        "trend_bucket_label": "hour (UTC)" if window_key == "24h" else "day (UTC)",
    }


def _llm_usage_json(context):
    """Serialize an ``_llm_usage_context`` dict for the polling script.

    Numbers are formatted exactly like the template's filters
    (``floatformat``/``date``) so the polled values are byte-identical to
    what a full reload of the same window would render, and the live
    refresh never visibly "jumps" to a different rounding/format.
    """
    return {
        "window": context["window"],
        "window_start": date_filter(context["window_start"], "Y-m-d H:i:s"),
        "total_calls": context["total_calls"],
        "total_cost": floatformat(context["total_cost"], 2),
        "failed_calls": context["failed_calls"],
        "failure_rate": floatformat(context["failure_rate"], 1),
        "avg_latency_ms": floatformat(context["avg_latency_ms"], 0),
        "by_model": [
            {
                "model_display": row["model_display"],
                "call_count": row["call_count"],
                "total_cost": floatformat(row["total_cost"], 2),
                "failed_count": row["failed_count"],
                "avg_latency_ms": floatformat(row["avg_latency_ms"], 0),
            }
            for row in context["by_model"]
        ],
        "by_error_class": [
            {
                "error_class_display": row["error_class_display"],
                "count": row["count"],
            }
            for row in context["by_error_class"]
        ],
        "trend": [
            {
                "bucket": str(row["bucket"]),
                "call_count": row["call_count"],
                "total_cost": floatformat(row["total_cost"], 2),
                "avg_latency_ms": floatformat(row["avg_latency_ms"], 0),
            }
            for row in context["trend"]
        ],
        "trend_bucket_label": context["trend_bucket_label"],
    }


def llm_usage(request):
    """Aggregated ``LLMCall`` cost/latency/volume/failure dashboard (#88).

    With ``X-Requested-With: XMLHttpRequest`` (the same convention used
    elsewhere in this view module, see ``_wants_json``), returns the same
    aggregates as JSON instead of rendering the template - this is what
    the page's own inline polling script (issue #89) fetches every 15s to
    live-refresh the numbers/tables in place, without a new endpoint.
    """
    window_key = request.GET.get("window")
    if window_key not in LLM_USAGE_WINDOWS:
        window_key = LLM_USAGE_DEFAULT_WINDOW
    context = _llm_usage_context(window_key)
    if _wants_json(request):
        return JsonResponse(_llm_usage_json(context))
    return render(request, "submissions/llm_usage.html", context)


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


#: Message shown when a per-card review action targets a card whose
#: ``dedup_status`` flipped to ``duplicate`` after the review grid was
#: rendered (issue #78) - the dedup-timing race, not a real 404.
_DEDUP_RACE_MESSAGE = (
    "This card was just identified as a duplicate of another card and is "
    "no longer part of the review; no action was taken. You can continue "
    "with the rest of the batch."
)


def _review_card_or_error(request, batch, card_pk):
    """Look up one card for a per-card review action (issue #78).

    Returns ``(card, None)`` when the card is still reviewable. Returns
    ``(None, response)`` when it is not - either it never existed / isn't
    part of *batch* (a genuine 404, raised here exactly like
    ``get_object_or_404`` would), or it did exist in the review grid but its
    ``dedup_status`` has since flipped to ``duplicate`` (the #78 race
    between the review page rendering and semantic dedup finishing): that
    case gets a clear, handled response - JSON for the grid's XHR calls, an
    error message + redirect otherwise - instead of letting a raw
    ``Http404`` propagate, so the reviewer can keep working the rest of the
    batch without a blank error page or a full page reload.
    """
    card = _batch_review_cards(batch).filter(pk=card_pk).first()
    if card is not None:
        return card, None

    is_dedup_race = Card.objects.filter(
        pk=card_pk,
        submitted_url__requests__batch=batch,
        dedup_status=Card.DedupStatus.DUPLICATE,
    ).exists()
    if not is_dedup_race:
        raise Http404("No Card matches the given query.")

    if _wants_json(request):
        response = JsonResponse(
            {"error": _DEDUP_RACE_MESSAGE, "dedup_duplicate": True}, status=409
        )
    else:
        messages.error(request, _DEDUP_RACE_MESSAGE)
        response = redirect("submissions:card_review", pk=batch.pk)
    return None, response


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
    stored_raw = (getattr(batch, "deck_name", None) or "")
    # Prefill exactly one field: dropdown when the stored deck is in the
    # live list, otherwise free text only (never both).
    if unavailable or (stored_raw and stored_raw not in deck_names):
        return {
            "deck_names": deck_names,
            "deck_unavailable": unavailable,
            "stored_deck": "",
            "deck_text_value": stored_raw,
        }
    return {
        "deck_names": deck_names,
        "deck_unavailable": unavailable,
        "stored_deck": stored_raw,
        "deck_text_value": "",
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
        {k: v for k, v in _deck_picker_context(batch).items() if k != "stored_deck" and k != "deck_text_value"}
    )
    raw = attempted or (getattr(batch, "deck_name", None) or "")
    if context.get("deck_unavailable") or (raw and raw not in context.get("deck_names", [])):
        context["stored_deck"] = ""
        context["deck_text_value"] = raw
    else:
        context["stored_deck"] = raw
        context["deck_text_value"] = ""
    return render(request, "submissions/card_review.html", context)


@require_POST
def card_review_decision(request, batch_pk, card_pk):
    batch = get_object_or_404(Batch, pk=batch_pk)
    card, error_response = _review_card_or_error(request, batch, card_pk)
    if error_response is not None:
        return error_response

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
    card, error_response = _review_card_or_error(request, batch, card_pk)
    if error_response is not None:
        return error_response

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
    card, error_response = _review_card_or_error(request, batch, card_pk)
    if error_response is not None:
        return error_response

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
    card, error_response = _review_card_or_error(request, batch, card_pk)
    if error_response is not None:
        return error_response
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
    card, error_response = _review_card_or_error(request, batch, card_pk)
    if error_response is not None:
        return error_response

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
    card, error_response = _review_card_or_error(request, batch, card_pk)
    if error_response is not None:
        return error_response

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
    card, error_response = _review_card_or_error(request, batch, card_pk)
    if error_response is not None:
        return error_response

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
    card, error_response = _review_card_or_error(request, batch, card_pk)
    if error_response is not None:
        return error_response

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
