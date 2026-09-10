import pytest
from django.urls import reverse

from submissions.models import Batch, BatchRequest, SubmittedURL

pytestmark = pytest.mark.django_db


@pytest.fixture
def url():
    return reverse("submissions:home")


def test_get_shows_form_with_textarea_and_submit(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "<textarea" in content
    assert 'type="submit"' in content


def test_successful_single_submission(client, url):
    resp = client.post(url, {"urls": "https://example.com/a"}, follow=True)
    assert resp.status_code == 200
    assert SubmittedURL.objects.count() == 1
    assert SubmittedURL.objects.first().url == "https://example.com/a"
    assert "https://example.com/a" in resp.content.decode()


def test_multi_line_submission_saved_and_listed(client, url):
    body = "https://example.com/1\nhttps://example.com/2\nhttp://example.org/3"
    resp = client.post(url, {"urls": body}, follow=True)
    assert SubmittedURL.objects.count() == 3
    content = resp.content.decode()
    for u in ["https://example.com/1", "https://example.com/2", "http://example.org/3"]:
        assert u in content


def test_listing_back_persists(client, url):
    batch = Batch.objects.create()
    submitted = SubmittedURL.objects.create(
        url="https://persisted.example.com", batch=batch
    )
    BatchRequest.objects.create(batch=batch, submitted_url=submitted)
    resp = client.get(url)
    assert "https://persisted.example.com" in resp.content.decode()


def test_empty_submission_saves_nothing_and_shows_message(client, url):
    resp = client.post(url, {"urls": ""})
    assert resp.status_code == 200
    assert SubmittedURL.objects.count() == 0
    assert "Please enter at least one URL." in resp.content.decode()


def test_whitespace_only_submission_treated_as_empty(client, url):
    resp = client.post(url, {"urls": "   \n  \n\t"})
    assert resp.status_code == 200
    assert SubmittedURL.objects.count() == 0
    assert "Please enter at least one URL." in resp.content.decode()


def test_surrounding_whitespace_trimmed(client, url):
    client.post(url, {"urls": "   https://example.com/trim   "}, follow=True)
    assert SubmittedURL.objects.count() == 1
    assert SubmittedURL.objects.first().url == "https://example.com/trim"


@pytest.mark.parametrize("bad", ["not a url", "ftp://x", "http://", "justtext"])
def test_malformed_urls_rejected(client, url, bad):
    resp = client.post(url, {"urls": bad}, follow=True)
    assert SubmittedURL.objects.count() == 0
    assert "invalid" in resp.content.decode().lower()


def test_mixed_valid_and_invalid(client, url):
    body = "https://good.example.com\nnot a url\nhttp://also-good.example.com\nftp://bad"
    resp = client.post(url, {"urls": body}, follow=True)
    saved = set(SubmittedURL.objects.values_list("url", flat=True))
    assert saved == {"https://good.example.com", "http://also-good.example.com"}
    content = resp.content.decode()
    assert "not a url" in content
    assert "ftp://bad" in content


def test_duplicate_already_in_db_not_added_twice(client, url):
    SubmittedURL.objects.create(
        url="https://dup.example.com", batch=Batch.objects.create()
    )
    client.post(url, {"urls": "https://dup.example.com"}, follow=True)
    assert SubmittedURL.objects.filter(url="https://dup.example.com").count() == 1


def test_duplicate_within_same_submission_saved_once(client, url):
    body = "https://same.example.com\nhttps://same.example.com"
    client.post(url, {"urls": body}, follow=True)
    assert SubmittedURL.objects.filter(url="https://same.example.com").count() == 1


def test_long_submission_of_100_urls(client, url):
    body = "\n".join(f"https://example.com/page/{i}" for i in range(100))
    resp = client.post(url, {"urls": body}, follow=True)
    assert resp.status_code == 200
    assert SubmittedURL.objects.count() == 100


# --- Issue #13: visual styling / accessibility ---


def test_page_loads_single_project_css_and_no_style_blocks(client, url):
    content = client.get(url).content.decode()
    assert content.count('<link rel="stylesheet"') == 1
    assert "submissions/app.css" in content
    assert "<style" not in content


def test_viewport_meta_present(client, url):
    content = client.get(url).content.decode()
    assert 'name="viewport"' in content
    assert "width=device-width" in content


def test_skip_link_and_main_landmark(client, url):
    content = client.get(url).content.decode()
    assert 'href="#main"' in content
    assert "Skip to content" in content
    assert '<main id="main"' in content
    # skip link is before <main> in source (first focusable element)
    assert content.index("Skip to content") < content.index("<main")


def test_consistent_field_wording(client, url):
    content = client.get(url).content.decode()
    assert "URLs (one per line)" in content
    assert "One URL per line" not in content


def test_flash_messages_have_live_region(client, url):
    resp = client.post(url, {"urls": "https://example.com/live"}, follow=True)
    content = resp.content.decode()
    assert 'aria-live="polite"' in content or 'role="status"' in content
    assert "Saved 1 URL(s)." in content


def test_empty_state_wording_is_batches(client, url):
    content = client.get(url).content.decode()
    assert "No batches yet" in content
    assert "No URLs submitted yet" not in content


def test_validation_failure_marks_error_for_focus(client, url):
    content = client.post(url, {"urls": ""}).content.decode()
    assert 'id="form-error"' in content
    assert 'aria-describedby="form-error"' in content


def test_batch_detail_uses_shared_base(client, url):
    client.post(url, {"urls": "https://example.com/x"}, follow=True)
    batch = Batch.objects.get()
    content = client.get(
        reverse("submissions:batch_detail", args=[batch.pk])
    ).content.decode()
    assert "submissions/app.css" in content
    assert '<main id="main"' in content
    assert 'name="viewport"' in content
    assert "Skip to content" in content
