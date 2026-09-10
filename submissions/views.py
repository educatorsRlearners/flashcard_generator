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
        "originated_here": submitted_url.batch_id == batch.pk,
        "origin_batch_id": submitted_url.batch_id,
    }


def _batch_rows(batch):
    rows = []
    seen = set()
    for request_row in batch.requests.select_related("submitted_url"):
        submitted_url = request_row.submitted_url
        seen.add(submitted_url.pk)
        rows.append(_row(submitted_url, batch))
    # URLs that originated in this batch but have no BatchRequest row yet
    # (pre-#15 data that has not been through the backfill migration).
    for submitted_url in batch.urls.all():
        if submitted_url.pk not in seen:
            rows.append(_row(submitted_url, batch))
    return rows


def _batch_is_empty(batch):
    return not batch.requests.exists() and not batch.urls.exists()


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
    if batch_request is None and submitted_url.batch_id != batch.pk:
        raise Http404("URL is not part of this batch")

    came_from_home = request.POST.get("next") == "home"

    if request.method != "POST":
        if came_from_home:
            return redirect("submissions:home")
        return redirect("submissions:batch_detail", pk=batch_pk)

    url_text = submitted_url.url
    if batch_request is not None:
        batch_request.delete()

    if not submitted_url.requests.exists():
        submitted_url.delete()

    batch_emptied = _batch_is_empty(batch)
    if batch_emptied:
        batch.delete()

    messages.success(request, f"Deleted {url_text}.")

    if came_from_home or batch_emptied:
        return redirect("submissions:home")
    return redirect("submissions:batch_detail", pk=batch_pk)
