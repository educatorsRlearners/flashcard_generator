"""Background task for extension-submitted card generation (issue #36).

Fills in the #35 stub: :func:`process_extension_submission` now actually
runs card generation for the ``SubmittedURL`` the extension submitted, so
the #35 status-polling endpoint (``extension_api.submission_status``) has
something terminal to report.

Mirrors ``submissions.tasks``'s ``run_extraction`` / ``process_url``
pattern:

* :func:`run_generation` is a thin one-line seam around the real call
  (``generation.generate_for(..., force=True)``), defined at module level
  purely so tests can ``monkeypatch.setattr(extension_tasks,
  "run_generation", fake)``.
* :func:`process_extension_submission` looks up the row in its own bare
  ``try/except SubmittedURL.DoesNotExist: return`` (silent no-op - there is
  nothing to mark failed on a row that no longer exists), then calls
  :func:`run_generation` inside a second ``try/except Exception``.

That second try/except is load-bearing, not defensive boilerplate:
``generation.generate_for``'s own docstring is explicit that
``llm.LLMError`` subclasses (auth, rate-limit, transient, bad-response) are
**not** caught internally - only its own handled outcomes (insufficient
content, no valid cards) call ``mark_generation_failed`` before returning.
An unhandled raise leaves ``generation_status`` exactly as it was before
the call (for a fresh extension submission, its model default - neither
``ok`` nor ``failed``), which would poll forever. So on an unexpected
exception here we mark the row failed ourselves, via a targeted
``.filter(pk=...).update(...)`` (not ``.save()``) so the update can't
clobber a row mutated elsewhere in the meantime - matching
``tasks.process_url`` exactly.

``force=True`` is always passed: the extension user is looking at the page
right now and expects fresh cards for it, not a stale skip from an earlier
batch run. It is never configurable.
"""

from __future__ import annotations

from huey.contrib.djhuey import db_task

from . import generation
from .models import SubmittedURL


def run_generation(submitted_url: SubmittedURL):
    """Seam around card generation (patched in tests)."""
    return generation.generate_for(submitted_url, force=True)


@db_task()
def process_extension_submission(submitted_url_id: int) -> None:
    """Run card generation for one extension-submitted SubmittedURL."""
    try:
        submitted_url = SubmittedURL.objects.get(pk=submitted_url_id)
    except SubmittedURL.DoesNotExist:
        return

    try:
        run_generation(submitted_url)
    except Exception as exc:  # isolate the failure to this one URL
        SubmittedURL.objects.filter(pk=submitted_url_id).update(
            generation_status=SubmittedURL.GenerationStatus.FAILED,
            generation_error=f"unexpected error: {exc}",
        )
