"""Background tasks for batch processing (issue #8).

Submitting a batch enqueues one :func:`process_url` task per submitted URL.
Each task runs the existing synchronous extraction path
(:func:`submissions.extraction.extract`), which fetches + extracts the page
and persists ``status`` / ``failure_kind`` / ``failure_reason`` on the row
(the failure taxonomy from #4 - tasks never re-classify).

Tasks are isolated: an unexpected exception in one URL's task marks only
that URL ``failed`` and never blocks the other URLs in the batch. The queue
is a local SQLite file, so pending tasks survive a consumer crash and are
picked up when it restarts.

Note: the in-process robots.txt cache and per-domain rate-limit clock in
``submissions.extraction`` are per-worker-process state. Sharing them across
consumer workers is out of scope here and tracked in #17.
"""

from __future__ import annotations

from huey.contrib.djhuey import db_task

from . import extraction
from .models import SubmittedURL


def run_extraction(submitted_url: SubmittedURL):
    """Seam around the synchronous extraction path (patched in tests)."""
    return extraction.extract(submitted_url)


@db_task()
def process_url(submitted_url_id: int) -> None:
    """Fetch + extract a single :class:`SubmittedURL` in the background."""
    try:
        submitted_url = SubmittedURL.objects.get(pk=submitted_url_id)
    except SubmittedURL.DoesNotExist:
        return

    try:
        run_extraction(submitted_url)
    except Exception as exc:  # isolate the failure to this one URL
        SubmittedURL.objects.filter(pk=submitted_url_id).update(
            status=SubmittedURL.Status.FAILED,
            failure_kind=SubmittedURL.FailureKind.UNKNOWN,
            failure_reason=f"unexpected error: {exc}",
            extracted_text="",
            extracted_title="",
            extracted_at=None,
        )


def enqueue_batch(batch) -> int:
    """Enqueue one :func:`process_url` task per distinct URL in *batch*.

    Returns the number of tasks enqueued.
    """
    url_ids = (
        batch.requests.order_by("submitted_url_id")
        .values_list("submitted_url_id", flat=True)
        .distinct()
    )
    count = 0
    for url_id in url_ids:
        process_url(url_id)
        count += 1
    return count
