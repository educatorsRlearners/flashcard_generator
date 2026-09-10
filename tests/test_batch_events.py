"""Issue #23: batch progress via SSE (StreamingHttpResponse, no new dep)."""

import json

import pytest
from django.urls import reverse

from submissions import views
from submissions.models import Batch, BatchRequest, SubmittedURL

pytestmark = pytest.mark.django_db


def _make_batch(*statuses):
    batch = Batch.objects.create()
    for i, status in enumerate(statuses):
        su = SubmittedURL.objects.create(
            url=f"https://sse{i}.example.com", batch=batch, status=status
        )
        BatchRequest.objects.create(batch=batch, submitted_url=su)
    return batch


def _collect_events(response):
    body = b"".join(response.streaming_content).decode()
    events = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk or chunk.startswith(":") or chunk.startswith("retry:"):
            continue
        name, _, data = chunk.partition("\ndata: ")
        assert name.startswith("event: ")
        events.append((name[len("event: "):], json.loads(data)))
    return events


def test_events_terminal_batch_snapshot_then_complete(client):
    batch = _make_batch(SubmittedURL.Status.OK)
    resp = client.get(reverse("submissions:batch_events", args=[batch.pk]))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/event-stream"
    events = _collect_events(resp)
    names = [name for name, _ in events]
    assert names[0] == "snapshot"
    assert names[-1] == "complete"
    snapshot = events[0][1]
    assert snapshot["batch_id"] == batch.pk
    assert snapshot["terminal"] is True
    assert snapshot == views._status_payload(batch)
    assert events[-1][1]["terminal"] is True


def test_events_unknown_batch_404s(client):
    assert (
        client.get(reverse("submissions:batch_events", args=[999999])).status_code
        == 404
    )


def test_iter_emits_progress_event_on_change():
    pending = {"batch_id": 1, "terminal": False, "ok": 0, "cards": 0}
    progressed = {"batch_id": 1, "terminal": False, "ok": 1, "cards": 2}
    done = {"batch_id": 1, "terminal": True, "ok": 1, "cards": 2}
    seq = [pending, dict(pending), progressed, done]
    chunks = list(
        views._iter_batch_events(
            1,
            fetch=lambda pk: seq.pop(0),
            sleep_fn=lambda s: None,
            poll_seconds=1,
            max_seconds=60,
            heartbeat_seconds=10_000,
        )
    )
    body = "".join(chunks)
    names = [
        line[len("event: "):]
        for line in body.splitlines()
        if line.startswith("event: ")
    ]
    assert names[0] == "snapshot"
    assert "progress" in names
    assert names[-1] == "complete"
    assert json.dumps(progressed) in body


def test_batch_detail_wires_events_stream(client):
    batch = _make_batch(SubmittedURL.Status.PENDING)
    content = client.get(
        reverse("submissions:batch_detail", args=[batch.pk])
    ).content.decode()
    assert reverse("submissions:batch_events", args=[batch.pk]) in content
    assert "EventSource" in content
    # Polling survives only as the documented reconnect fallback.
    assert "setInterval" in content
    assert "fallback" in content.lower()


def test_batch_detail_terminal_page_does_not_stream(client):
    batch = _make_batch(SubmittedURL.Status.OK)
    content = client.get(
        reverse("submissions:batch_detail", args=[batch.pk])
    ).content.decode()
    assert 'data-terminal="1"' in content
