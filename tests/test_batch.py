import pytest
from django.urls import reverse

from submissions.models import Batch, BatchRequest, SubmittedURL

pytestmark = pytest.mark.django_db


def _make_url(url, batch, **kwargs):
    """Create a SubmittedURL and its originating BatchRequest, matching the
    invariant guaranteed by the data migration (every URL reachable via a
    BatchRequest)."""
    submitted = SubmittedURL.objects.create(url=url, batch=batch, **kwargs)
    BatchRequest.objects.create(batch=batch, submitted_url=submitted)
    return submitted


@pytest.fixture
def home_url():
    return reverse("submissions:home")


def test_one_submission_one_batch_all_pending(client, home_url):
    body = "https://example.com/a\nhttps://example.com/b"
    client.post(home_url, {"urls": body}, follow=True)
    assert Batch.objects.count() == 1
    batch = Batch.objects.get()
    assert batch.urls.count() == 2
    for u in batch.urls.all():
        assert u.status == SubmittedURL.Status.PENDING
        assert u.failure_reason == ""


def test_two_submissions_two_batches(client, home_url):
    client.post(home_url, {"urls": "https://example.com/1"}, follow=True)
    first = Batch.objects.get()
    first_urls = set(first.urls.values_list("url", flat=True))
    client.post(home_url, {"urls": "https://example.com/2"}, follow=True)
    assert Batch.objects.count() == 2
    assert set(first.urls.values_list("url", flat=True)) == first_urls


def test_all_duplicate_submission_records_a_new_request(client, home_url):
    b = Batch.objects.create()
    _make_url("https://dup.example.com", b)
    resp = client.post(
        home_url, {"urls": "https://dup.example.com"}, follow=True
    )
    # A new batch is created to record the re-request...
    assert Batch.objects.count() == 2
    # ...but no duplicate SubmittedURL row.
    assert SubmittedURL.objects.filter(url="https://dup.example.com").count() == 1
    new_batch = Batch.objects.exclude(pk=b.pk).get()
    assert new_batch.requests.count() == 1
    assert b.requests.count() == 1
    assert "already" in resp.content.decode().lower()


def test_mixed_new_and_duplicate(client, home_url):
    b = Batch.objects.create()
    _make_url("https://old.example.com", b)
    client.post(
        home_url,
        {"urls": "https://old.example.com\nhttps://new.example.com"},
        follow=True,
    )
    assert Batch.objects.count() == 2
    new_batch = Batch.objects.exclude(pk=b.pk).get()
    assert set(new_batch.urls.values_list("url", flat=True)) == {
        "https://new.example.com"
    }
    old = SubmittedURL.objects.get(url="https://old.example.com")
    assert old.batch_id == b.pk


def test_empty_submission_creates_no_batch(client, home_url):
    client.post(home_url, {"urls": "   \n  "})
    assert Batch.objects.count() == 0
    assert SubmittedURL.objects.count() == 0


def test_batch_detail_renders_and_404s(client, home_url):
    client.post(home_url, {"urls": "https://example.com/x"}, follow=True)
    batch = Batch.objects.get()
    resp = client.get(reverse("submissions:batch_detail", args=[batch.pk]))
    assert resp.status_code == 200
    assert "https://example.com/x" in resp.content.decode()
    assert "pending" in resp.content.decode()
    missing = client.get(reverse("submissions:batch_detail", args=[999999]))
    assert missing.status_code == 404


def test_derived_overall_status(client):
    batch = Batch.objects.create()
    u1 = _make_url("https://example.com/1", batch)
    _make_url("https://example.com/2", batch)
    assert batch.overall_status == "pending"
    u1.status = SubmittedURL.Status.OK
    u1.save()
    assert batch.overall_status == "pending"
    for u in batch.urls.all():
        u.status = SubmittedURL.Status.OK
        u.save()
    assert batch.overall_status == "complete"


def test_failed_status_and_reason_shown_on_detail(client):
    batch = Batch.objects.create()
    u = _make_url("https://example.com/f", batch)
    u.status = SubmittedURL.Status.FAILED
    u.failure_reason = "boom happened"
    u.save()
    resp = client.get(reverse("submissions:batch_detail", args=[batch.pk]))
    content = resp.content.decode()
    assert "failed" in content
    assert "boom happened" in content
    assert batch.status_summary == "0 pending, 0 ok, 1 failed"


# --- Issue #15: BatchRequest through-model ---


def test_new_url_creates_one_submittedurl_and_one_batchrequest(client, home_url):
    client.post(home_url, {"urls": "https://fresh.example.com"}, follow=True)
    batch = Batch.objects.get()
    assert SubmittedURL.objects.filter(url="https://fresh.example.com").count() == 1
    assert BatchRequest.objects.count() == 1
    br = BatchRequest.objects.get()
    assert br.batch_id == batch.pk
    assert br.submitted_url.url == "https://fresh.example.com"


def test_existing_url_new_batch_adds_one_batchrequest_only(client, home_url):
    b1 = Batch.objects.create()
    existing = _make_url("https://shared.example.com", b1)
    first_br = BatchRequest.objects.get()

    client.post(home_url, {"urls": "https://shared.example.com"}, follow=True)

    assert SubmittedURL.objects.filter(url="https://shared.example.com").count() == 1
    assert existing.requests.count() == 2
    new_batch = Batch.objects.exclude(pk=b1.pk).get()
    assert BatchRequest.objects.filter(
        batch=new_batch, submitted_url=existing
    ).count() == 1
    # earlier batch's request untouched
    first_br.refresh_from_db()
    assert first_br.batch_id == b1.pk


def test_same_url_twice_in_one_batch_makes_one_batchrequest(client, home_url):
    body = "https://twice.example.com\nhttps://twice.example.com"
    client.post(home_url, {"urls": body}, follow=True)
    batch = Batch.objects.get()
    assert BatchRequest.objects.filter(batch=batch).count() == 1
    assert SubmittedURL.objects.filter(url="https://twice.example.com").count() == 1


def test_counts_include_re_requested_urls(client, home_url):
    b1 = Batch.objects.create()
    _make_url("https://re.example.com", b1, status=SubmittedURL.Status.OK)
    client.post(home_url, {"urls": "https://re.example.com"}, follow=True)
    new_batch = Batch.objects.exclude(pk=b1.pk).get()
    assert new_batch.url_count == 1
    assert new_batch.status_counts[SubmittedURL.Status.OK] == 1
    assert new_batch.status_summary == "0 pending, 1 ok, 0 failed"


def test_batch_detail_lists_re_requested_url_with_origin_marker(client, home_url):
    b1 = Batch.objects.create()
    _make_url("https://origin.example.com", b1)
    client.post(home_url, {"urls": "https://origin.example.com"}, follow=True)
    new_batch = Batch.objects.exclude(pk=b1.pk).get()
    content = client.get(
        reverse("submissions:batch_detail", args=[new_batch.pk])
    ).content.decode()
    assert "https://origin.example.com" in content
    assert f"first requested in Batch {b1.pk}" in content


def test_deleting_batch_keeps_shared_submittedurl(client, home_url):
    b1 = Batch.objects.create()
    shared = _make_url("https://keep.example.com", b1)
    client.post(home_url, {"urls": "https://keep.example.com"}, follow=True)
    new_batch = Batch.objects.exclude(pk=b1.pk).get()
    new_batch.delete()
    assert SubmittedURL.objects.filter(pk=shared.pk).exists()
    assert BatchRequest.objects.filter(submitted_url=shared).count() == 1


def test_deleting_originating_batch_keeps_shared_url_and_other_request(
    client, home_url
):
    b1 = Batch.objects.create()
    shared = _make_url("https://origin-del.example.com", b1)
    client.post(
        home_url, {"urls": "https://origin-del.example.com"}, follow=True
    )
    b2 = Batch.objects.exclude(pk=b1.pk).get()

    b1.delete()

    shared.refresh_from_db()
    assert shared.batch_id is None
    assert BatchRequest.objects.filter(
        batch=b2, submitted_url=shared
    ).exists()
    assert BatchRequest.objects.filter(submitted_url=shared).count() == 1


def test_backfill_migration_links_every_preexisting_url():
    from importlib import import_module

    from django.apps import apps as django_apps

    _0005 = import_module(
        "submissions.migrations.0005_backfill_batchrequest"
    )

    b1 = Batch.objects.create()
    b2 = Batch.objects.create()
    su1 = SubmittedURL.objects.create(url="https://bf1.example.com", batch=b1)
    su2 = SubmittedURL.objects.create(url="https://bf2.example.com", batch=b2)
    # simulate pre-#15 state
    BatchRequest.objects.all().delete()

    _0005.backfill(django_apps, None)

    assert su1.requests.get().batch_id == b1.pk
    assert su2.requests.get().batch_id == b2.pk
    assert BatchRequest.objects.count() == 2
    # idempotent
    _0005.backfill(django_apps, None)
    assert BatchRequest.objects.count() == 2
    # reversible
    _0005.unbackfill(django_apps, None)
    assert BatchRequest.objects.count() == 0
