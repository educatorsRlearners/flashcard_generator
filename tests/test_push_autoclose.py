"""Auto-close polling hook for the review tab (issue #155).

After Finish, the review page polls a lightweight push-status endpoint
about every 1s and closes its own tab ~3s after the push reaches ``done``.
These tests cover the endpoint (all push states + unknown batch) and the
template hook (poll URL + close script present when a push is in flight or
done, absent when Finish was never clicked).
"""

import pytest
from django.urls import reverse

from submissions.models import Batch

pytestmark = pytest.mark.django_db


def _status_url(batch):
    return reverse("submissions:card_review_push_status", args=[batch.pk])


def _review_page(client, batch):
    return client.get(reverse("submissions:card_review", args=[batch.pk]))


# --- endpoint ------------------------------------------------------------


def test_blank_status_when_finish_never_clicked(client):
    batch = Batch.objects.create()
    resp = client.get(_status_url(batch))
    assert resp.status_code == 200
    assert resp.json() == {"push_status": "", "message": ""}


def test_pending_status(client):
    batch = Batch.objects.create()
    batch.mark_push_pending()
    resp = client.get(_status_url(batch))
    assert resp.status_code == 200
    data = resp.json()
    assert data["push_status"] == "pending"
    assert "in progress" in data["message"]


def test_done_status_reports_pushed_count(client):
    batch = Batch.objects.create()
    batch.record_push_done(deck_name="My Deck", pushed=3, skipped=0, failed=0)
    resp = client.get(_status_url(batch))
    assert resp.status_code == 200
    data = resp.json()
    assert data["push_status"] == "done"
    assert "3 card(s) pushed to deck 'My Deck'" in data["message"]


def test_unreachable_status(client):
    batch = Batch.objects.create()
    batch.record_push_unreachable()
    resp = client.get(_status_url(batch))
    assert resp.status_code == 200
    data = resp.json()
    assert data["push_status"] == "unreachable"
    assert "unreachable" in data["message"].lower()


def test_failed_status(client):
    batch = Batch.objects.create()
    batch.record_push_failed()
    resp = client.get(_status_url(batch))
    assert resp.status_code == 200
    data = resp.json()
    assert data["push_status"] == "failed"
    assert data["message"]


def test_unknown_batch_404(client):
    resp = client.get(reverse("submissions:card_review_push_status", args=[999999]))
    assert resp.status_code == 404


def test_endpoint_is_read_only(client):
    batch = Batch.objects.create()
    batch.mark_push_pending()
    client.get(_status_url(batch))
    batch.refresh_from_db()
    assert batch.push_status == Batch.PushStatus.PENDING


# --- template hook ---------------------------------------------------------


def test_pending_page_exposes_poll_hook_and_close_script(client):
    batch = Batch.objects.create()
    batch.mark_push_pending()
    content = _review_page(client, batch).content.decode()
    assert 'data-push-status="pending"' in content
    assert f'data-push-status-url="{_status_url(batch)}"' in content
    assert "window.close" in content
    assert "POLL_INTERVAL_MS = 1000" in content
    assert "CLOSE_DELAY_MS = 3000" in content


def test_done_page_exposes_close_hook(client):
    batch = Batch.objects.create()
    batch.record_push_done(deck_name="My Deck", pushed=2, skipped=0, failed=0)
    content = _review_page(client, batch).content.decode()
    assert 'data-push-status="done"' in content
    assert f'data-push-status-url="{_status_url(batch)}"' in content
    assert "window.close" in content


def test_failed_page_never_closes(client):
    batch = Batch.objects.create()
    batch.record_push_failed()
    content = _review_page(client, batch).content.decode()
    assert 'data-push-status="failed"' in content
    # The script's terminal-state guard renders the failure banner and stops
    # polling instead of scheduling a close.
    assert 'nextStatus === "unreachable" || nextStatus === "failed"' in content


def test_blank_status_page_has_no_poll_hook(client):
    batch = Batch.objects.create()
    content = _review_page(client, batch).content.decode()
    assert "id=\"push-outcome\"" not in content
    # The script exits early without #push-outcome, so no polling and no
    # close attempt occurs; the banner's status URL is never rendered.
    assert "data-push-status-url=\"" not in content
