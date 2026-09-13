"""Tests for the LLM usage dashboard's live-refresh JSON endpoint (#89).

The `llm_usage` view (#88) gains a JSON branch, selected the same way the
rest of this view module already picks JSON vs HTML (`X-Requested-With:
XMLHttpRequest`, see `_wants_json`), so the page's inline polling script
can re-fetch the current window's aggregates every 15s without a full
reload. This file only covers that JSON branch and its data fidelity
against the HTML render; the aggregation logic itself is covered by
`tests/test_llm_usage_dashboard.py` (#88).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from submissions.models import LLMCall

pytestmark = pytest.mark.django_db

_JSON_HEADERS = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _make_call(
    *, model, cost, latency_ms, status="ok", error_class="", age, provider="", json_retried=False
):
    """Create an ``LLMCall`` and backdate its ``created_at`` by *age*."""
    call = LLMCall.objects.create(
        model=model,
        provider=provider,
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=latency_ms,
        estimated_cost_usd=Decimal(str(cost)),
        status=status,
        error_class=error_class,
        json_retried=json_retried,
    )
    LLMCall.objects.filter(pk=call.pk).update(created_at=timezone.now() - age)
    return call


def test_json_branch_requires_xrequestedwith_header():
    """A plain GET (no header) still renders the HTML template, unchanged."""
    client = Client()
    response = client.get("/llm-usage/")
    assert response["Content-Type"].startswith("text/html")


def test_json_branch_returns_json_content_type():
    client = Client()
    response = client.get("/llm-usage/", **_JSON_HEADERS)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"


def test_json_payload_matches_current_window_default():
    client = Client()
    response = client.get("/llm-usage/", **_JSON_HEADERS)
    data = response.json()
    assert data["window"] == "7d"
    assert data["total_calls"] == 0
    assert data["total_cost"] == "0.00"
    assert data["failed_calls"] == 0
    assert data["failure_rate"] == "0.0"
    assert data["by_model"] == []
    assert data["by_provider"] == []
    assert data["by_error_class"] == []
    assert data["trend"] == []


def test_json_payload_reflects_new_calls_and_matches_html_render():
    client = Client()
    _make_call(
        model="claude-a",
        provider="anthropic",
        cost="1.00",
        latency_ms=100,
        json_retried=True,
        age=timedelta(hours=1),
    )
    _make_call(
        model="claude-b",
        provider="anthropic",
        cost="2.50",
        latency_ms=300,
        status="failed",
        error_class="RateLimitError",
        age=timedelta(hours=2),
    )

    html_response = client.get("/llm-usage/", {"window": "7d"})
    json_response = client.get("/llm-usage/", {"window": "7d"}, **_JSON_HEADERS)

    data = json_response.json()
    assert data["total_calls"] == 2
    assert data["total_cost"] == "3.50"
    assert data["failed_calls"] == 1
    assert data["failure_rate"] == "50.0"

    by_model = {row["model_display"]: row for row in data["by_model"]}
    assert by_model["claude-a"]["call_count"] == 1
    assert by_model["claude-a"]["total_cost"] == "1.00"
    assert by_model["claude-b"]["failed_count"] == 1

    by_error = {row["error_class_display"]: row["count"] for row in data["by_error_class"]}
    assert by_error == {"RateLimitError": 1}

    # Grouped by provider+model (#103): the two "anthropic" calls use
    # different models, so they land in separate rows.
    by_provider = {
        (row["provider_display"], row["model_display"]): row for row in data["by_provider"]
    }
    assert by_provider[("anthropic", "claude-a")]["call_count"] == 1
    assert by_provider[("anthropic", "claude-a")]["json_retried_count"] == 1
    assert by_provider[("anthropic", "claude-a")]["json_retry_rate"] == "100.0"
    assert by_provider[("anthropic", "claude-b")]["call_count"] == 1
    assert by_provider[("anthropic", "claude-b")]["json_retried_count"] == 0
    assert by_provider[("anthropic", "claude-b")]["json_retry_rate"] == "0.0"

    assert len(data["trend"]) >= 1

    # Formatting must match exactly what the HTML template rendered for
    # the same window (byte-identical numbers, no polling "jump").
    html = html_response.content.decode()
    assert f"${data['total_cost']}" in html
    assert f"{data['failure_rate']}%" in html
    assert f"{by_provider[('anthropic', 'claude-a')]['json_retry_rate']}%" in html


def test_json_by_provider_model_display_matches_html():
    """#103: the JSON payload's `by_provider` rows carry `model_display`,
    formatted consistently with the HTML render, so live-refresh output
    stays byte-identical to a full reload for the new Model column."""
    client = Client()
    _make_call(
        model="",
        provider="openai-compatible",
        cost="1.00",
        latency_ms=100,
        json_retried=True,
        age=timedelta(hours=1),
    )

    html_response = client.get("/llm-usage/", {"window": "7d"})
    json_response = client.get("/llm-usage/", {"window": "7d"}, **_JSON_HEADERS)
    data = json_response.json()

    row = data["by_provider"][0]
    assert row["provider_display"] == "openai-compatible"
    assert row["model_display"] == "(unknown model)"

    html = html_response.content.decode()
    assert "(unknown model)" in html


def test_json_payload_respects_window_query_param():
    client = Client()
    # Outside 24h but inside 7d.
    _make_call(model="claude-a", cost="9.00", latency_ms=100, age=timedelta(hours=30))

    response_24h = client.get("/llm-usage/", {"window": "24h"}, **_JSON_HEADERS)
    response_7d = client.get("/llm-usage/", {"window": "7d"}, **_JSON_HEADERS)

    assert response_24h.json()["window"] == "24h"
    assert response_24h.json()["total_calls"] == 0
    assert response_7d.json()["window"] == "7d"
    assert response_7d.json()["total_calls"] == 1


def test_json_payload_unrecognized_window_falls_back_to_default():
    client = Client()
    response = client.get("/llm-usage/", {"window": "bogus"}, **_JSON_HEADERS)
    assert response.json()["window"] == "7d"


def test_html_page_includes_live_toggle_and_polling_script():
    """No-JS fallback + toggle markup: present regardless of JS execution."""
    client = Client()
    response = client.get("/llm-usage/")
    content = response.content.decode()
    assert 'data-role="live-toggle"' in content
    assert "llm_usage_live.js" in content
    # The full server-rendered data is present without any script running.
    assert 'data-role="total-calls"' in content


def test_html_page_carries_poll_url_and_active_window_for_script():
    client = Client()
    response = client.get("/llm-usage/", {"window": "30d"})
    content = response.content.decode()
    assert 'data-window="30d"' in content
    assert 'data-poll-url="/llm-usage/"' in content
