from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from .forms import URLSubmissionForm
from .models import Batch, SubmittedURL


def home(request):
    if request.method == "POST":
        form = URLSubmissionForm(request.POST)
        if form.is_valid():
            new_urls = [
                url
                for url in form.valid_urls
                if not SubmittedURL.objects.filter(url=url).exists()
            ]

            if new_urls:
                batch = Batch.objects.create()
                for url in new_urls:
                    SubmittedURL.objects.create(url=url, batch=batch)
                messages.success(request, f"Saved {len(new_urls)} URL(s).")
            if form.valid_urls and not new_urls:
                messages.info(request, "All submitted URLs were already saved.")
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
    return render(
        request,
        "submissions/batch_detail.html",
        {"batch": batch, "urls": batch.urls.all()},
    )
