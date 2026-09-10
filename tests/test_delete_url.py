import pytest
from django.urls import reverse

from submissions.models import Batch, BatchRequest, SubmittedURL

pytestmark = pytest.mark.django_db


def _make_url(url, batch, **kwargs):
    submitted = SubmittedURL.objects.create(url=url, batch=batch, **kwargs)
    BatchRequest.objects.create(batch=batch, submitted_url=submitted)
    return submitted


def _delete_url(batch, submitted):
    return reverse("submissions:delete_url", args=[batch.pk, submitted.pk])


def test_post_deletes_row_and_redirects_to_batch_detail(client):
    batch = Batch.objects.create()
    a = _make_url("https://example.com/a", batch)
    _make_url("https://example.com/b", batch)

    resp = client.post(_delete_url(batch, a))

    assert resp.status_code == 302
    assert resp.url == reverse("submissions:batch_detail", args=[batch.pk])
    assert not SubmittedURL.objects.filter(pk=a.pk).exists()
    assert not BatchRequest.objects.filter(submitted_url=a).exists()
    assert Batch.objects.filter(pk=batch.pk).exists()

    # does not reappear after refresh
    content = client.get(
        reverse("submissions:batch_detail", args=[batch.pk])
    ).content.decode()
    url_list = content.split('<ul class="url-list">')[1]
    assert "https://example.com/a" not in url_list
    assert "https://example.com/b" in url_list


def test_flash_message_names_deleted_url(client):
    batch = Batch.objects.create()
    a = _make_url("https://example.com/gone", batch)
    _make_url("https://example.com/stay", batch)

    resp = client.post(_delete_url(batch, a), follow=True)
    assert "Deleted https://example.com/gone." in resp.content.decode()


def test_get_does_not_delete(client):
    batch = Batch.objects.create()
    a = _make_url("https://example.com/a", batch)

    resp = client.get(_delete_url(batch, a))

    assert resp.status_code in (302, 405)
    assert SubmittedURL.objects.filter(pk=a.pk).exists()
    assert BatchRequest.objects.filter(submitted_url=a).exists()


def test_deleting_last_url_removes_empty_batch(client):
    batch = Batch.objects.create()
    a = _make_url("https://example.com/only", batch)

    resp = client.post(_delete_url(batch, a))

    assert not Batch.objects.filter(pk=batch.pk).exists()
    assert not SubmittedURL.objects.filter(pk=a.pk).exists()
    assert resp.url == reverse("submissions:home")

    # home page no longer lists the batch
    content = client.get(reverse("submissions:home")).content.decode()
    assert f"Batch {batch.pk}" not in content


def test_delete_unknown_id_404s(client):
    batch = Batch.objects.create()
    _make_url("https://example.com/a", batch)

    url = reverse("submissions:delete_url", args=[batch.pk, 999999])
    assert client.post(url).status_code == 404


def test_delete_pending_url_updates_batch_counts(client):
    batch = Batch.objects.create()
    a = _make_url("https://example.com/a", batch)
    _make_url("https://example.com/b", batch, status=SubmittedURL.Status.OK)

    assert batch.url_count == 2
    assert batch.overall_status == "pending"

    client.post(_delete_url(batch, a))

    batch.refresh_from_db()
    assert batch.url_count == 1
    assert batch.overall_status == "complete"
    assert batch.status_summary == "0 pending, 1 ok, 0 failed"


def test_delete_ok_and_failed_urls_succeed(client):
    batch = Batch.objects.create()
    ok = _make_url("https://example.com/ok", batch, status=SubmittedURL.Status.OK)
    failed = _make_url(
        "https://example.com/f", batch, status=SubmittedURL.Status.FAILED
    )
    _make_url("https://example.com/keep", batch)

    assert client.post(_delete_url(batch, ok)).status_code == 302
    assert client.post(_delete_url(batch, failed)).status_code == 302
    assert not SubmittedURL.objects.filter(pk__in=[ok.pk, failed.pk]).exists()


def test_delete_from_batch_a_does_not_affect_batch_b(client):
    batch_a = Batch.objects.create()
    batch_b = Batch.objects.create()
    a = _make_url("https://example.com/a", batch_a)
    b = _make_url("https://example.com/b", batch_b)

    client.post(_delete_url(batch_a, a))

    assert Batch.objects.filter(pk=batch_b.pk).exists()
    assert SubmittedURL.objects.filter(pk=b.pk).exists()
    assert BatchRequest.objects.filter(batch=batch_b, submitted_url=b).exists()


def test_resubmitting_a_deleted_url_creates_a_new_row(client):
    home = reverse("submissions:home")
    client.post(home, {"urls": "https://example.com/redo"}, follow=True)
    batch = Batch.objects.get()
    original = SubmittedURL.objects.get(url="https://example.com/redo")

    client.post(_delete_url(batch, original))
    assert not SubmittedURL.objects.filter(url="https://example.com/redo").exists()

    client.post(home, {"urls": "https://example.com/redo"}, follow=True)
    recreated = SubmittedURL.objects.get(url="https://example.com/redo")
    assert recreated.pk != original.pk


def test_delete_from_multi_batch_only_removes_that_request(client):
    home = reverse("submissions:home")
    client.post(home, {"urls": "https://example.com/shared"}, follow=True)
    batch1 = Batch.objects.get()
    client.post(home, {"urls": "https://example.com/shared"}, follow=True)
    batch2 = Batch.objects.exclude(pk=batch1.pk).get()

    shared = SubmittedURL.objects.get(url="https://example.com/shared")
    assert shared.requests.count() == 2

    # delete from batch2 (the non-originating batch)
    resp = client.post(_delete_url(batch2, shared))

    shared.refresh_from_db()
    assert shared.requests.count() == 1
    assert shared.requests.get().batch_id == batch1.pk
    # batch2 had only this request -> now empty -> deleted
    assert not Batch.objects.filter(pk=batch2.pk).exists()
    assert resp.url == reverse("submissions:home")
    # SubmittedURL survives because it still belongs to batch1
    assert SubmittedURL.objects.filter(pk=shared.pk).exists()


def test_delete_multi_batch_url_from_origin_batch(client):
    home = reverse("submissions:home")
    client.post(home, {"urls": "https://example.com/shared"}, follow=True)
    batch1 = Batch.objects.get()
    client.post(home, {"urls": "https://example.com/shared"}, follow=True)
    batch2 = Batch.objects.exclude(pk=batch1.pk).get()

    shared = SubmittedURL.objects.get(url="https://example.com/shared")
    assert shared.batch_id == batch1.pk  # originated in batch1

    # delete from batch1, the ORIGIN batch
    resp = client.post(_delete_url(batch1, shared))

    shared.refresh_from_db()
    # request for batch1 gone, batch2's untouched
    assert shared.requests.count() == 1
    assert shared.requests.get().batch_id == batch2.pk
    # origin marker repointed to a batch that still contains the URL
    assert shared.batch_id == batch2.pk
    # batch1 is now empty -> deleted; batch2 unaffected
    assert not Batch.objects.filter(pk=batch1.pk).exists()
    assert Batch.objects.filter(pk=batch2.pk).exists()
    assert resp.url == reverse("submissions:home")

    # row does not reappear on batch1 (404 now) and batch2 still shows it
    assert client.get(
        reverse("submissions:batch_detail", args=[batch1.pk])
    ).status_code == 404
    detail2 = client.get(
        reverse("submissions:batch_detail", args=[batch2.pk])
    ).content.decode()
    assert "https://example.com/shared" in detail2


def test_url_count_matches_rendered_rows_after_delete(client):
    batch = Batch.objects.create()
    a = _make_url("https://example.com/a", batch)
    _make_url("https://example.com/b", batch)
    _make_url("https://example.com/c", batch)

    client.post(_delete_url(batch, a))

    batch.refresh_from_db()
    detail = client.get(
        reverse("submissions:batch_detail", args=[batch.pk])
    ).content.decode()
    url_list = detail.split('<ul class="url-list">')[1].split("</ul>")[0]
    rendered_rows = url_list.count("<li")
    assert rendered_rows == batch.url_count == 2


def test_delete_control_rendered_on_both_pages(client):
    home = reverse("submissions:home")
    client.post(home, {"urls": "https://example.com/x"}, follow=True)
    batch = Batch.objects.get()
    submitted = SubmittedURL.objects.get()
    action = _delete_url(batch, submitted)

    home_content = client.get(home).content.decode()
    assert action in home_content
    assert "confirm(" in home_content
    assert "csrfmiddlewaretoken" in home_content

    detail_content = client.get(
        reverse("submissions:batch_detail", args=[batch.pk])
    ).content.decode()
    assert action in detail_content
    assert "confirm(" in detail_content
