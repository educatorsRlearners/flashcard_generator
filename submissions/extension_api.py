"""JSON API for the browser extension (issue #35).

Two endpoints, mounted at ``/api/extension/`` (see
``submissions/extension_urls.py`` and ``config/urls.py``):

* ``POST /api/extension/submit/`` - the extension posts one page's already
  extracted content (``url`` / ``title`` / ``text``). This creates the
  ``Batch`` / ``SubmittedURL`` / ``BatchRequest`` rows directly (see
  ``submissions/models.py``), writes the extraction fields onto
  the ``SubmittedURL`` (mirroring ``extraction._save_success`` field-for-
  field, with ``extraction_method = SubmittedURL.ExtractionMethod.EXTENSION``),
  and enqueues :func:`submissions.extension_tasks.process_extension_submission`
  to generate cards.
* ``GET /api/extension/submit/<id>/status/`` - the extension polls this
  until generation is finished, to learn the ``review_url`` to open.

Auth is the #33 shared-secret token (``Authorization: Bearer <token>``);
these views are CSRF-exempt because that token check is the real access
control (matching the reasoning already implied by #33's design). CORS is
handled by hand (no new dependency): every response, including the
``OPTIONS`` preflight, carries ``Access-Control-Allow-Origin`` for
``settings.EXTENSION_ID``'s ``chrome-extension://`` origin only when that
setting is configured; an unconfigured ``EXTENSION_ID`` omits the header
entirely (fail closed).
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import extraction
from .extension_auth import token_matches
from .extension_tasks import process_extension_submission
from .models import Batch, BatchRequest, SubmittedURL

_validate_url = URLValidator(schemes=["http", "https"])


def _apply_cors(response, methods=None):
    """Attach the CORS headers acceptance criteria require to *response*.

    ``Access-Control-Allow-Origin`` is set only when ``EXTENSION_ID`` is
    configured (fail closed otherwise). ``methods`` is only passed by the
    ``OPTIONS`` preflight handlers, which additionally need
    ``Access-Control-Allow-Methods`` / ``Access-Control-Allow-Headers``.
    """
    if settings.EXTENSION_ID:
        response["Access-Control-Allow-Origin"] = (
            f"chrome-extension://{settings.EXTENSION_ID}"
        )
    if methods:
        response["Access-Control-Allow-Methods"] = methods
        response["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    return response


def _is_authorized(request) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token:
        return False
    return token_matches(token)


def _unauthorized():
    return JsonResponse({"error": "unauthorized"}, status=401)


@csrf_exempt
def submit(request):
    if request.method == "OPTIONS":
        return _apply_cors(HttpResponse(status=200), methods="POST, OPTIONS")

    if request.method != "POST":
        return _apply_cors(JsonResponse({"error": "method not allowed"}, status=405))

    if not _is_authorized(request):
        return _apply_cors(_unauthorized())

    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError("not a JSON object")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _apply_cors(
            JsonResponse({"error": "invalid JSON body"}, status=400)
        )

    url = payload.get("url")
    if not isinstance(url, str) or not url:
        return _apply_cors(
            JsonResponse({"error": "invalid or missing url"}, status=400)
        )
    try:
        _validate_url(url)
    except ValidationError:
        return _apply_cors(
            JsonResponse({"error": "invalid or missing url"}, status=400)
        )

    text = payload.get("text")
    if not isinstance(text, str):
        text = ""
    non_whitespace_len = len("".join(text.split()))
    if non_whitespace_len < extraction.MIN_CONTENT_CHARS:
        return _apply_cors(
            JsonResponse(
                {
                    "error": "text too short",
                    "min_chars": extraction.MIN_CONTENT_CHARS,
                },
                status=400,
            )
        )

    title = payload.get("title", "")
    if not isinstance(title, str):
        title = ""

    # Optional candidate image URLs collected by the content script from
    # the live DOM (issue #42). Missing / null / absent means "no
    # extension candidates" (same as before this field existed).
    # Malformed entries are dropped individually - never a whole-request
    # failure - mirroring how title/text are coerced above. Deeper
    # filtering (chrome markers, usability) stays server-side in
    # images.attach_images and applies to both candidate sources.
    raw_images = payload.get("images", [])
    if raw_images is None:
        raw_images = []
    extension_image_urls: list[str] = []
    if isinstance(raw_images, list):
        for entry in raw_images:
            if not isinstance(entry, str):
                continue
            candidate = entry.strip()
            if not candidate.lower().startswith(("http://", "https://")):
                continue
            extension_image_urls.append(candidate)

    # Creates the Batch / SubmittedURL / BatchRequest rows directly (a new
    # Batch every call - including a re-submission of an already-known URL,
    # matching the double-submit behaviour the model layer already allows).
    batch = Batch.objects.create()
    submitted_url, _created = SubmittedURL.objects.get_or_create(
        url=url, defaults={"batch": batch}
    )
    BatchRequest.objects.get_or_create(batch=batch, submitted_url=submitted_url)

    # Mirrors extraction._save_success field-for-field.
    submitted_url.extracted_text = text
    submitted_url.extracted_title = title
    submitted_url.extraction_method = SubmittedURL.ExtractionMethod.EXTENSION
    submitted_url.extracted_at = timezone.now()
    submitted_url.status = SubmittedURL.Status.OK
    submitted_url.failure_kind = ""
    submitted_url.failure_reason = ""
    submitted_url.extension_image_urls = extension_image_urls
    submitted_url.save()

    process_extension_submission(submitted_url.pk)

    return _apply_cors(
        JsonResponse(
            {"batch_id": batch.pk, "submitted_url_id": submitted_url.pk},
            status=202,
        )
    )


@csrf_exempt
def submission_status(request, submitted_url_id):
    if request.method == "OPTIONS":
        return _apply_cors(HttpResponse(status=200), methods="GET, OPTIONS")

    if request.method != "GET":
        return _apply_cors(JsonResponse({"error": "method not allowed"}, status=405))

    if not _is_authorized(request):
        return _apply_cors(_unauthorized())

    try:
        submitted_url = SubmittedURL.objects.get(pk=submitted_url_id)
    except SubmittedURL.DoesNotExist:
        return _apply_cors(JsonResponse({"error": "not found"}, status=404))

    Generation = SubmittedURL.GenerationStatus
    generation_status = submitted_url.generation_status
    terminal = (
        submitted_url.status == SubmittedURL.Status.FAILED
        or generation_status in (Generation.OK, Generation.FAILED)
    )

    review_url = None
    if generation_status == Generation.OK:
        # Most recent BatchRequest, not submitted_url.batch (the stale
        # first-ever origin batch) - see the issue's decision.
        latest_request = submitted_url.requests.order_by("-created_at").first()
        if latest_request is not None:
            review_url = request.build_absolute_uri(
                reverse(
                    "submissions:card_review",
                    kwargs={"pk": latest_request.batch_id},
                )
            )

    return _apply_cors(
        JsonResponse(
            {
                "submitted_url_id": submitted_url.pk,
                "url": submitted_url.url,
                "status": submitted_url.status,
                "generation_status": generation_status,
                "generation_error": submitted_url.generation_error,
                "terminal": terminal,
                "review_url": review_url,
            }
        )
    )
