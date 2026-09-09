import pytest
from django.urls import reverse

from submissions.models import Batch, SubmittedURL

pytestmark = pytest.mark.django_db


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


def test_all_duplicate_submission_creates_no_batch(client, home_url):
    b = Batch.objects.create()
    SubmittedURL.objects.create(url="https://dup.example.com", batch=b)
    resp = client.post(
        home_url, {"urls": "https://dup.example.com"}, follow=True
    )
    assert Batch.objects.count() == 1
    assert SubmittedURL.objects.filter(url="https://dup.example.com").count() == 1
    assert "already" in resp.content.decode().lower()


def test_mixed_new_and_duplicate(client, home_url):
    b = Batch.objects.create()
    SubmittedURL.objects.create(url="https://old.example.com", batch=b)
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
    u1 = SubmittedURL.objects.create(url="https://example.com/1", batch=batch)
    SubmittedURL.objects.create(url="https://example.com/2", batch=batch)
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
    u = SubmittedURL.objects.create(url="https://example.com/f", batch=batch)
    u.status = SubmittedURL.Status.FAILED
    u.failure_reason = "boom happened"
    u.save()
    resp = client.get(reverse("submissions:batch_detail", args=[batch.pk]))
    content = resp.content.decode()
    assert "failed" in content
    assert "boom happened" in content
    assert batch.status_summary == "0 pending, 0 ok, 1 failed"
