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
    # UX fix (#155 follow-up): pending banner warns the tab will close.
    assert "close automatically" in data["message"].lower()


def test_done_status_reports_pushed_count(client):
    batch = Batch.objects.create()
    batch.record_push_done(deck_name="My Deck", pushed=3, skipped=0, failed=0)
    resp = client.get(_status_url(batch))
    assert resp.status_code == 200
    data = resp.json()
    assert data["push_status"] == "done"
    assert "3 card(s) pushed to deck 'My Deck'" in data["message"]
    # UX fix (#155 follow-up): done banner warns the tab will close.
    assert "close automatically" in data["message"].lower()


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
    # UX fix (#155 follow-up): live region + in-UI auto-close warning.
    assert 'id="push-outcome"' in content
    assert 'aria-live="polite"' in content
    assert "close automatically" in content.lower()


def test_done_page_exposes_close_hook(client):
    batch = Batch.objects.create()
    batch.record_push_done(deck_name="My Deck", pushed=2, skipped=0, failed=0)
    content = _review_page(client, batch).content.decode()
    assert 'data-push-status="done"' in content
    assert f'data-push-status-url="{_status_url(batch)}"' in content
    assert "window.close" in content
    # UX fix (#155 follow-up): live region + in-UI auto-close warning.
    assert 'aria-live="polite"' in content
    assert "close automatically" in content.lower()


def test_failure_banner_never_warns_about_autoclose(client):
    """Unreachable/failed never close, so they must not promise auto-close."""
    for record in (Batch.record_push_unreachable, Batch.record_push_failed):
        batch = Batch.objects.create()
        record(batch)
        content = _review_page(client, batch).content.decode()
        assert 'aria-live="polite"' in content
        assert "close automatically" not in content.lower()


def test_failed_page_never_closes(client):
    batch = Batch.objects.create()
    batch.record_push_failed()
    content = _review_page(client, batch).content.decode()
    assert 'data-push-status="failed"' in content
    # (#161) unreachable/failed no longer stop polling -- they keep polling
    # (slowly) so a tab left open notices when the periodic retry (#58)
    # later succeeds. Only reaching "done" schedules a close.
    assert 'nextStatus === "unreachable" || nextStatus === "failed"' in content
    assert "startPolling(SLOW_POLL_INTERVAL_MS)" in content


def test_unreachable_page_exposes_slow_poll_hook(client):
    batch = Batch.objects.create()
    batch.record_push_unreachable()
    content = _review_page(client, batch).content.decode()
    assert 'data-push-status="unreachable"' in content
    assert f'data-push-status-url="{_status_url(batch)}"' in content
    assert "SLOW_POLL_INTERVAL_MS = 30000" in content


def test_failed_page_exposes_slow_poll_hook(client):
    batch = Batch.objects.create()
    batch.record_push_failed()
    content = _review_page(client, batch).content.decode()
    assert 'data-push-status="failed"' in content
    assert f'data-push-status-url="{_status_url(batch)}"' in content
    assert "SLOW_POLL_INTERVAL_MS = 30000" in content


def test_slow_poll_interval_distinct_from_fast_interval(client):
    batch = Batch.objects.create()
    batch.record_push_failed()
    content = _review_page(client, batch).content.decode()
    assert "POLL_INTERVAL_MS = 1000" in content
    assert "SLOW_POLL_INTERVAL_MS = 30000" in content


def test_failure_to_done_transition_updates_banner_and_schedules_close(client):
    """A slow poll observing done must render the done banner and close."""
    batch = Batch.objects.create()
    batch.record_push_failed()
    content = _review_page(client, batch).content.decode()
    # onStatus renders "done" and calls scheduleClose() regardless of which
    # status the tab was previously showing -- there is no branch that
    # requires having started from "pending" to reach the done/close path.
    assert 'nextStatus === "done"' in content
    assert 'render("done", message)' in content
    assert "scheduleClose();" in content


def test_repeated_failure_status_skips_render_to_avoid_flicker(client):
    """Only an actual status change re-renders; repeats must not flicker."""
    batch = Batch.objects.create()
    batch.record_push_failed()
    content = _review_page(client, batch).content.decode()
    assert "var currentStatus = el.getAttribute" in content
    assert "if (nextStatus !== currentStatus) { render(nextStatus, message); }" in content


def test_blank_status_page_has_no_poll_hook(client):
    batch = Batch.objects.create()
    content = _review_page(client, batch).content.decode()
    assert "id=\"push-outcome\"" not in content
    # The script exits early without #push-outcome, so no polling and no
    # close attempt occurs; the banner's status URL is never rendered.
    assert "data-push-status-url=\"" not in content
