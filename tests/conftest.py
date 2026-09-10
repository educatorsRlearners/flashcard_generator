"""Shared test wiring.

Fetch politeness (issue #17) adds a robots.txt fetch, per-domain sleeps, and a
retry/backoff loop to the extraction path. This autouse fixture neutralises all
three for the whole suite so tests stay offline and instant:

* ``_fetch_robots_text`` -> always ``None`` (robots.txt "allow all", fail open),
* ``_sleep`` -> a no-op (no real waiting for rate limiting / backoff),
* the in-process robots cache and rate-limit clock are reset between tests.

``tests/test_politeness.py`` overrides these with its own fake clock / sleep
spy / canned robots bodies to exercise the real behaviour.
"""

import pytest

from submissions import extraction


@pytest.fixture(autouse=True)
def _offline_politeness(monkeypatch):
    extraction._reset_politeness_state()
    monkeypatch.setattr(extraction, "_fetch_robots_text", lambda robots_url: None)
    monkeypatch.setattr(extraction, "_sleep", lambda seconds: None)
    yield
    extraction._reset_politeness_state()


@pytest.fixture(autouse=True)
def _huey_immediate(monkeypatch):
    """Run Huey tasks inline (issue #8) and neutralise the extraction call.

    Immediate mode means submitting a batch executes its tasks synchronously
    in-process - no external ``run_huey`` consumer. The real extraction path
    is stubbed to a no-op here so the wider suite stays offline and URLs stay
    ``pending`` unless a test opts in; #8's own tests replace
    ``submissions.tasks.run_extraction`` with a fake that sets a terminal
    status.
    """
    from huey.contrib.djhuey import HUEY

    from submissions import tasks

    HUEY.immediate = True
    monkeypatch.setattr(tasks, "run_extraction", lambda submitted_url: None)
    yield
    HUEY.immediate = False


@pytest.fixture(autouse=True)
def _no_card_images(monkeypatch):
    """Neutralise per-card image work (issue #12) for the whole suite.

    Card generation calls ``submissions.images.attach_images`` as a
    best-effort final step; that would re-fetch the source page and call
    Draw Things. Stub it to a no-op so the wider suite stays offline.
    ``tests/test_card_images.py`` exercises the real module with fakes.
    """
    from submissions import images

    monkeypatch.setattr(images, "attach_images", lambda *a, **kw: None)
    yield
