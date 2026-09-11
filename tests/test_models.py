"""Model-level tests for submissions models."""

import pytest

from submissions.models import Batch, SubmittedURL

pytestmark = pytest.mark.django_db


def test_extension_extraction_method_round_trips():
    batch = Batch.objects.create()
    row = SubmittedURL.objects.create(
        url="https://example.com/extension",
        batch=batch,
        extraction_method=SubmittedURL.ExtractionMethod.EXTENSION,
    )
    row.refresh_from_db()

    assert row.extraction_method == SubmittedURL.ExtractionMethod.EXTENSION
    assert row.get_extraction_method_display() == "Extension"


def test_extension_extraction_method_included_in_not_none_query():
    batch = Batch.objects.create()
    row = SubmittedURL.objects.create(
        url="https://example.com/extension",
        batch=batch,
        extraction_method=SubmittedURL.ExtractionMethod.EXTENSION,
    )

    not_none = SubmittedURL.objects.exclude(
        extraction_method=SubmittedURL.ExtractionMethod.NONE
    )

    assert row in not_none
