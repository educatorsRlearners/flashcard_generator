from django.contrib import messages
from django.shortcuts import redirect, render

from .forms import URLSubmissionForm
from .models import SubmittedURL


def home(request):
    if request.method == "POST":
        form = URLSubmissionForm(request.POST)
        if form.is_valid():
            saved = 0
            for url in form.valid_urls:
                _, created = SubmittedURL.objects.get_or_create(url=url)
                if created:
                    saved += 1

            if saved:
                messages.success(request, f"Saved {saved} URL(s).")
            if form.valid_urls and not saved:
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

    return render(
        request,
        "submissions/home.html",
        {"form": form, "submitted_urls": SubmittedURL.objects.all()},
    )
