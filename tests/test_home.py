import pytest
from django.urls import reverse

from submissions.models import Batch, SubmittedURL

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
    SubmittedURL.objects.create(
        url="https://persisted.example.com", batch=Batch.objects.create()
    )
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
