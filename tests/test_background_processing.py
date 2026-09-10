"""Issue #8: background batch processing via Huey + live progress."""

import pytest
from django.urls import reverse

from submissions import tasks
from submissions.models import Batch, BatchRequest, Card, SubmittedURL

pytestmark = pytest.mark.django_db


@pytest.fixture
def home_url():
    return reverse("submissions:home")


def _mark(status, reason=""):
    def _run(submitted_url):
        submitted_url.status = status
        submitted_url.failure_reason = reason
        if status == SubmittedURL.Status.FAILED:
            submitted_url.failure_kind = SubmittedURL.FailureKind.UNKNOWN
        submitted_url.save()

    return _run


def test_submit_enqueues_one_task_per_url(client, home_url, monkeypatch):
    seen = []
    monkeypatch.setattr(tasks, "run_extraction", lambda su: seen.append(su.pk))

    body = "https://a.example.com\nhttps://b.example.com\nhttps://c.example.com"
    client.post(home_url, {"urls": body}, follow=True)

    ids = set(SubmittedURL.objects.values_list("pk", flat=True))
    assert sorted(seen) == sorted(ids)
    assert len(seen) == 3


def test_submit_redirects_to_batch_detail(client, home_url):
    resp = client.post(home_url, {"urls": "https://x.example.com"})
    batch = Batch.objects.get()
    assert resp.status_code == 302
    assert resp["Location"] == reverse("submissions:batch_detail", args=[batch.pk])


def test_running_tasks_moves_every_url_to_terminal(client, home_url, monkeypatch):
    monkeypatch.setattr(tasks, "run_extraction", _mark(SubmittedURL.Status.OK))

    client.post(
        home_url,
        {"urls": "https://1.example.com\nhttps://2.example.com"},
        follow=True,
    )
    batch = Batch.objects.get()
    assert batch.overall_status == "complete"
    for u in batch.urls.all():
        assert u.status == SubmittedURL.Status.OK


def test_all_failing_batch_reaches_complete(client, home_url, monkeypatch):
    monkeypatch.setattr(
        tasks, "run_extraction", _mark(SubmittedURL.Status.FAILED, "kaboom")
    )

    resp = client.post(
        home_url,
        {"urls": "https://f1.example.com\nhttps://f2.example.com"},
        follow=True,
    )
    batch = Batch.objects.get()
    assert batch.overall_status == "complete"
    content = resp.content.decode()
    assert "Done: 0 ok, 2 failed" in content
    assert content.count("kaboom") >= 2

    payload = client.get(
        reverse("submissions:batch_status", args=[batch.pk])
    ).json()
    assert payload["terminal"] is True
    assert payload["summary"] == "Done: 0 ok, 2 failed"
    assert payload["ok"] == 0 and payload["failed"] == 2


def test_task_exception_isolated_to_one_url(client, home_url, monkeypatch):
    def _run(submitted_url):
        if "bad" in submitted_url.url:
            raise RuntimeError("boom")
        submitted_url.status = SubmittedURL.Status.OK
        submitted_url.save()

    monkeypatch.setattr(tasks, "run_extraction", _run)

    client.post(
        home_url,
        {"urls": "https://ok.example.com\nhttps://bad.example.com"},
        follow=True,
    )
    good = SubmittedURL.objects.get(url="https://ok.example.com")
    bad = SubmittedURL.objects.get(url="https://bad.example.com")
    assert good.status == SubmittedURL.Status.OK
    assert bad.status == SubmittedURL.Status.FAILED
    assert "unexpected error" in bad.failure_reason
    assert Batch.objects.get().overall_status == "complete"


def test_worker_not_running_page_still_loads(client):
    batch = Batch.objects.create()
    su = SubmittedURL.objects.create(url="https://pending.example.com", batch=batch)
    BatchRequest.objects.create(batch=batch, submitted_url=su)
    old = batch.created_at.replace(year=2020)
    Batch.objects.filter(pk=batch.pk).update(created_at=old)

    resp = client.get(reverse("submissions:batch_detail", args=[batch.pk]))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "does not appear to be running" in content
    assert "run_huey" in content
    # Worker-down: suppress the misleading "Processing URL 1 of N" line
    assert "Processing URL" not in content
    assert "Waiting for the background worker to start" in content

    payload = client.get(
        reverse("submissions:batch_status", args=[batch.pk])
    ).json()
    assert payload["worker_running"] is False
    assert payload["terminal"] is False
    assert payload["summary"] == "Waiting for the background worker to start"


def test_status_endpoint_reports_card_count(client, home_url, monkeypatch):
    monkeypatch.setattr(tasks, "run_extraction", _mark(SubmittedURL.Status.OK))
    client.post(home_url, {"urls": "https://card.example.com"}, follow=True)
    batch = Batch.objects.get()
    su = batch.urls.get()
    Card.objects.create(
        submitted_url=su, batch=batch, note_type=Card.NoteType.BASIC,
        front="q", back="a", source_term="q",
    )
    payload = client.get(
        reverse("submissions:batch_status", args=[batch.pk])
    ).json()
    assert payload["cards"] == 1


def test_pending_batch_reports_progress(client, home_url):
    # default autouse stub is a no-op, so URLs stay pending
    client.post(
        home_url,
        {"urls": "https://p1.example.com\nhttps://p2.example.com"},
        follow=True,
    )
    batch = Batch.objects.get()
    payload = client.get(
        reverse("submissions:batch_status", args=[batch.pk])
    ).json()
    assert payload["terminal"] is False
    assert payload["summary"] == "Processing URL 1 of 2"
    assert payload["total"] == 2
