from django.contrib import messages
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
    return render(
        request,
        "submissions/home.html",
        {
            "form": form,
            "batches": batches,
            "latest_batch": latest_batch,
        },
    )


def batch_detail(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    rows = []
    for request_row in batch.requests.select_related("submitted_url"):
        submitted_url = request_row.submitted_url
        rows.append(
            {
                "url": submitted_url.url,
                "status": submitted_url.status,
                "failure_reason": submitted_url.failure_reason,
                "originated_here": submitted_url.batch_id == batch.pk,
                "origin_batch_id": submitted_url.batch_id,
            }
        )
    return render(
        request,
        "submissions/batch_detail.html",
        {"batch": batch, "urls": rows},
    )
