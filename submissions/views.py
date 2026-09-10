from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.utils import timezone

from .forms import URLSubmissionForm
from .models import Batch, BatchRequest, Card, Feedback, SubmittedURL
from .tasks import enqueue_batch


def home(request):
    if request.method == "POST":
        form = URLSubmissionForm(request.POST)
        if form.is_valid():
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
            return redirect("submissions:home")
    else:
        form = URLSubmissionForm()

    batches = list(Batch.objects.all())
    latest_batch = batches[0] if batches else None
    latest_batch_rows = _batch_rows(latest_batch) if latest_batch else []
    return render(
        request,
        "submissions/home.html",
        {
            "form": form,
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
    if decision in (Card.ReviewStatus.ACCEPTED, Card.ReviewStatus.REJECTED):
        tags = card.tags if isinstance(card.tags, dict) else {}
        Feedback.objects.create(
            note_type=card.note_type,
            front=card.front,
            back=card.back,
            source_url=tags.get("source_url", "") or "",
            decision=decision,
            reason=card.rejection_reason,
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
    return redirect("submissions:batch_detail", pk=pk)
