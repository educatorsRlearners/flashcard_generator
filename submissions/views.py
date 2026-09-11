import re

from django.conf import settings
from django.contrib import messages
from django.db import close_old_connections, transaction
from django.db.models import Count, Q
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

import json
import time

from .forms import URLSubmissionForm
from .models import Batch, BatchRequest, Card, Feedback, SubmittedURL
from .tasks import enqueue_batch, push_accepted_cards_task


def home(request):
    if request.method == "POST":
        form = URLSubmissionForm(request.POST)
        if form.is_valid():
            request.session.pop("submission_error", None)
            if form.valid_urls:
                batch = Batch.objects.create()
                new_count = 0
                existing_count = 0
                for url in form.valid_urls:
                    submitted_url, created = SubmittedURL.objects.get_or_create(
                        url=url, defaults={"batch": batch}
                    )
                    BatchRequest.objects.get_or_create(
                        batch=batch, submitted_url=submitted_url
                    )
                    if created:
                        new_count += 1
                    else:
                        existing_count += 1
                if new_count:
                    messages.success(request, f"Saved {new_count} URL(s).")
                if existing_count:
                    messages.info(
                        request,
                        f"{existing_count} URL(s) were already saved; "
                        "recorded as a new request for this batch.",
                    )
                # Kick off background processing and hand the user straight
                # to the live batch page (the response never waits on a
                # fetch).
                enqueue_batch(batch)
                if form.invalid_lines:
                    messages.warning(
                        request,
                        "Rejected {n} invalid line(s): {lines}".format(
                            n=len(form.invalid_lines),
                            lines=", ".join(form.invalid_lines),
                        ),
                    )
                return redirect("submissions:batch_detail", pk=batch.pk)
            if form.invalid_lines:
                messages.warning(
                    request,
                    "Rejected {n} invalid line(s): {lines}".format(
                        n=len(form.invalid_lines),
                        lines=", ".join(form.invalid_lines),
                    ),
                )
            request.session["submission_error"] = {
                "urls": request.POST.get("urls", ""),
                "errors": [],
            }
            return redirect("submissions:home")
        # Invalid (empty) submission: stash raw text + error strings, PRG.
        raw = request.POST.get("urls", "")
        errors = [str(e) for e in form.errors.get("urls", [])]
        request.session["submission_error"] = {"urls": raw, "errors": errors}
        return redirect("submissions:home")
    else:
        stash = request.session.pop("submission_error", None)
        if stash:
            form = URLSubmissionForm(initial={"urls": stash.get("urls", "")})
            submission_errors = stash.get("errors", [])
        else:
            form = URLSubmissionForm()
            submission_errors = []

    batches = list(Batch.objects.all())
    latest_batch = batches[0] if batches else None
    latest_batch_rows = _batch_rows(latest_batch) if latest_batch else []
    return render(
        request,
        "submissions/home.html",
        {
            "form": form,
            "submission_errors": submission_errors,
            "batches": batches,
            "latest_batch": latest_batch,
            "latest_batch_rows": latest_batch_rows,
        },
    )


def _row(submitted_url, batch):
    return {
        "id": submitted_url.pk,
        "url": submitted_url.url,
        "status": submitted_url.status,
        "failure_reason": submitted_url.failure_reason,
        "failure_kind": submitted_url.failure_kind,
        "failure_kind_label": (
            submitted_url.get_failure_kind_display()
            if submitted_url.failure_kind
            else ""
        ),
        "originated_here": submitted_url.batch_id == batch.pk,
        "origin_batch_id": submitted_url.batch_id,
    }


def _batch_rows(batch):
    # Since #15, BatchRequest is the sole source of truth for what a batch
    # contains. The legacy SubmittedURL.batch origin FK is only an "originated
    # here" marker and must never add or keep a row in a batch's listing
    # (0005_backfill_batchrequest gave every pre-#15 URL a BatchRequest row).
    return [
        _row(request_row.submitted_url, batch)
        for request_row in batch.requests.select_related("submitted_url")
    ]


def _batch_is_empty(batch):
    # Emptiness is defined solely by BatchRequest, matching _batch_rows and
    # Batch.url_count. The origin FK does not keep an otherwise-empty batch alive.
    return not batch.requests.exists()


def _batch_card_count(batch):
    return (
        Card.objects.filter(submitted_url__requests__batch=batch)
        .distinct()
        .count()
    )


def _worker_looks_down(batch, counts):
    """Heuristic: pending work, nothing processed, and the batch is old."""
    pending = counts.get(SubmittedURL.Status.PENDING, 0)
    processed = counts.get(SubmittedURL.Status.OK, 0) + counts.get(
        SubmittedURL.Status.FAILED, 0
    )
    if pending == 0 or processed > 0:
        return False
    age = (timezone.now() - batch.created_at).total_seconds()
    return age >= settings.HUEY_WORKER_STALE_SECONDS


def _status_payload(batch):
    counts = batch.status_counts
    total = batch.url_count
    ok = counts.get(SubmittedURL.Status.OK, 0)
    failed = counts.get(SubmittedURL.Status.FAILED, 0)
    pending = counts.get(SubmittedURL.Status.PENDING, 0)
    processed = ok + failed
    terminal = pending == 0
    worker_down = _worker_looks_down(batch, counts)
    if terminal:
        summary = f"Done: {ok} ok, {failed} failed"
    elif worker_down:
        summary = "Waiting for the background worker to start"
    else:
        summary = f"Processing URL {min(processed + 1, total)} of {total}"
    return {
        "batch_id": batch.pk,
        "total": total,
        "processed": processed,
        "ok": ok,
        "failed": failed,
        "pending": pending,
        "cards": _batch_card_count(batch),
        "terminal": terminal,
        "worker_running": not worker_down,
        "summary": summary,
        "start_command": HUEY_CONSUMER_COMMAND,
        "urls": [
            {
                "id": row["id"],
                "url": row["url"],
                "status": row["status"],
                "failure_reason": row["failure_reason"],
                "failure_kind_label": row["failure_kind_label"],
            }
            for row in _batch_rows(batch)
        ],
    }


HUEY_CONSUMER_COMMAND = "uv run python manage.py run_huey"


def batch_detail(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    payload = _status_payload(batch)
    return render(
        request,
        "submissions/batch_detail.html",
        {
            "batch": batch,
            "urls": _batch_rows(batch),
            "status": payload,
            "huey_command": HUEY_CONSUMER_COMMAND,
        },
    )


def batch_status(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    return JsonResponse(_status_payload(batch))


# --- Batch progress stream (issue #23: SSE instead of polling) ------------
#
# Plain Django StreamingHttpResponse, no new dependency. Each browser tab
# holds one EventSource connection; the generator polls the DB and only
# emits an event when the payload changed, plus heartbeats to keep proxies
# from idling the connection out. Completion arrives as a named "complete"
# event after which the client closes the connection.
#
# Reconnect behaviour: EventSource reconnects natively on a dropped
# connection, and because every reconnect starts with a fresh "snapshot"
# event carrying the full current payload, the client resumes correctly
# even though it missed earlier events. If EventSource is unavailable or
# the stream errors repeatedly, the page falls back to the #8 polling loop
# against batch_status (see batch_detail.html).

#: Seconds between DB checks while streaming batch progress.
SSE_POLL_SECONDS = 1.0
#: Upper bound for one stream connection; the client reconnects (fresh
#: snapshot) if the batch is still running when the cap is hit.
SSE_MAX_SECONDS = 300.0
#: A ": heartbeat" comment this often keeps idle connections alive.
SSE_HEARTBEAT_SECONDS = 15.0


def _sse_format(event, payload):
    """Format one SSE event carrying a JSON payload."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _fetch_batch_payload(batch_pk):
    close_old_connections()
    batch = Batch.objects.filter(pk=batch_pk).first()
    if batch is None:
        return None
    return _status_payload(batch)


def _iter_batch_events(
    batch_pk,
    fetch=None,
    sleep_fn=None,
    poll_seconds=None,
    max_seconds=None,
    heartbeat_seconds=None,
):
    """Yield SSE chunks for *batch_pk* (generator, test seam included).

    The injectable *fetch* / *sleep_fn* / timing args exist so tests can
    drive the generator with a canned payload sequence and no sleeping;
    the view calls it with defaults (live DB + ``time.sleep``).
    """
    fetch = fetch or _fetch_batch_payload
    sleep_fn = sleep_fn or time.sleep
    poll_seconds = SSE_POLL_SECONDS if poll_seconds is None else poll_seconds
    max_seconds = SSE_MAX_SECONDS if max_seconds is None else max_seconds
    heartbeat_seconds = (
        SSE_HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    )

    yield "retry: 3000\n\n"
    payload = fetch(batch_pk)
    if payload is None:
        yield _sse_format("error", {"error": "batch not found"})
        return
    yield _sse_format("snapshot", payload)
    if payload.get("terminal"):
        yield _sse_format("complete", payload)
        return

    last_serialized = json.dumps(payload, sort_keys=True)
    elapsed = 0.0
    since_heartbeat = 0.0
    while elapsed < max_seconds:
        sleep_fn(poll_seconds)
        elapsed += poll_seconds
        since_heartbeat += poll_seconds
        payload = fetch(batch_pk)
        if payload is None:
            yield _sse_format("error", {"error": "batch not found"})
            return
        serialized = json.dumps(payload, sort_keys=True)
        if serialized != last_serialized:
            last_serialized = serialized
            yield _sse_format("progress", payload)
            since_heartbeat = 0.0
        elif since_heartbeat >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            since_heartbeat = 0.0
        if payload.get("terminal"):
            yield _sse_format("complete", payload)
            return


def batch_events(request, pk):
    get_object_or_404(Batch, pk=pk)
    stream = _iter_batch_events(pk)
    response = StreamingHttpResponse(stream, content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def delete_url(request, batch_pk, url_pk):
    batch = get_object_or_404(Batch, pk=batch_pk)
    submitted_url = get_object_or_404(SubmittedURL, pk=url_pk)
    batch_request = BatchRequest.objects.filter(
        batch=batch, submitted_url=submitted_url
    ).first()
    # Membership is defined solely by BatchRequest (#15). A URL whose only tie
    # to this batch is the legacy origin FK is not "in" the batch.
    if batch_request is None:
        raise Http404("URL is not part of this batch")

    came_from_home = request.POST.get("next") == "home"

    if request.method != "POST":
        if came_from_home:
            return redirect("submissions:home")
        return redirect("submissions:batch_detail", pk=batch_pk)

    url_text = submitted_url.url
    batch_request.delete()

    remaining = list(submitted_url.requests.select_related("batch"))
    if not remaining:
        submitted_url.delete()
    elif submitted_url.batch_id == batch.pk:
        # The URL is being removed from the batch it originated in but still
        # lives in other batches. Repoint the origin marker to the oldest batch
        # that still requests it so "originated_here" keeps pointing at a batch
        # that actually contains the URL.
        oldest = min(remaining, key=lambda r: (r.batch.created_at, r.batch_id))
        submitted_url.batch = oldest.batch
        submitted_url.save(update_fields=["batch"])

    batch_emptied = _batch_is_empty(batch)
    if batch_emptied:
        batch.delete()

    messages.success(request, f"Deleted {url_text}.")

    if came_from_home or batch_emptied:
        return redirect("submissions:home")
    return redirect("submissions:batch_detail", pk=batch_pk)


def delete_batch(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    if request.method != "POST":
        return redirect("submissions:batch_detail", pk=pk)
    with transaction.atomic():
        url_ids = list(
            batch.requests.values_list("submitted_url_id", flat=True)
        )
        batch_pk = batch.pk
        batch.delete()
        for submitted_url in SubmittedURL.objects.filter(pk__in=url_ids):
            remaining = list(
                submitted_url.requests.select_related("batch")
            )
            if not remaining:
                submitted_url.delete()
            elif (
                submitted_url.batch_id is None
                or submitted_url.batch_id == batch_pk
            ):
                oldest = min(
                    remaining,
                    key=lambda r: (r.batch.created_at, r.batch_id),
                )
                if submitted_url.batch_id != oldest.batch_id:
                    submitted_url.batch = oldest.batch
                    submitted_url.save(update_fields=["batch"])
    messages.success(request, f"Deleted Batch {batch_pk}.")
    return redirect("submissions:home")


def clear_batches(request):
    if request.method != "POST":
        return redirect("submissions:home")
    with transaction.atomic():
        Batch.objects.all().delete()
        SubmittedURL.objects.filter(requests__isnull=True).delete()
    messages.success(request, "Cleared all batches.")
    return redirect("submissions:home")


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
    return render(
        request,
        "submissions/card_review.html",
        {
            "batch": batch,
            "ready": ready,
            "cards": cards,
            "tally": tally,
        },
    )


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
    batch = get_object_or_404(Batch, pk=pk)
    tally = _review_tally(_batch_review_cards(batch))
    undecided = tally["undecided"]
    if undecided and request.POST.get("confirm") != "1":
        # Ask for an explicit confirm showing the count; nothing is changed.
        return render(
            request,
            "submissions/card_review.html",
            {
                "batch": batch,
                "ready": True,
                "cards": list(_batch_review_cards(batch)),
                "tally": tally,
                "confirm_undecided": undecided,
            },
        )
    messages.success(
        request,
        "Review finished: {accepted} accepted, {rejected} rejected, "
        "{undecided} left undecided.".format(**tally),
    )
    # issue #57: finishing a batch fires a background Anki push. This never
    # blocks the response - the task re-scans accepted-unsynced cards by
    # query when it actually runs, so it always pushes current DB state.
    push_accepted_cards_task()
    return redirect("submissions:batch_detail", pk=pk)
