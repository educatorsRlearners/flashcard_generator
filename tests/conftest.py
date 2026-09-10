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
