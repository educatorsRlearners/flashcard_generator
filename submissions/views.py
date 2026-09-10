from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import URLSubmissionForm
from .models import Batch, BatchRequest, SubmittedURL


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


def batch_detail(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    return render(
        request,
        "submissions/batch_detail.html",
        {"batch": batch, "urls": _batch_rows(batch)},
    )


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
