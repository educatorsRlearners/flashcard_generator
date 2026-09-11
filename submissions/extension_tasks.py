"""Background task stub for extension-submitted card generation (issue #35).

The real body is filled in by issue #36 (calls
``generation.generate_for(..., force=True)`` and isolates failures the way
``submissions.tasks.process_url`` does). This issue only establishes the
agreed name/signature so :mod:`submissions.extension_api` has something to
enqueue against without the two issues' engineers needing to coordinate
live.
"""

from __future__ import annotations

from huey.contrib.djhuey import db_task


@db_task()
def process_extension_submission(submitted_url_id: int) -> None:
    """Run card generation for one extension-submitted SubmittedURL.

    Body implemented in #36 (calls generation.generate_for(..., force=True)
    and isolates failures the way tasks.process_url does).
    """
    # no-op until #36
