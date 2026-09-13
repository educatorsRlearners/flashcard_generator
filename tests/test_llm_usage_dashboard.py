"""Tests for the LLM usage dashboard (issue #88).

``GET /llm-usage/`` aggregates ``LLMCall`` rows (issue #28) over a
selectable recent time window. ``created_at`` is ``auto_now_add`` so rows
are created normally and then backdated with a direct ``.update()`` (never
by round-tripping through ``save()``, which would re-stamp it).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from submissions.models import LLMCall

pytestmark = pytest.mark.django_db


def _make_call(*, model, cost, latency_ms, status="ok", error_class="", age):
    """Create an ``LLMCall`` and backdate its ``created_at`` by *age*."""
    call = LLMCall.objects.create(
        model=model,
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=latency_ms,
        estimated_cost_usd=Decimal(str(cost)),
        status=status,
        error_class=error_class,
    )
    LLMCall.objects.filter(pk=call.pk).update(created_at=timezone.now() - age)
    return call


def test_llm_usage_defaults_to_7d_window():
    client = Client()
    response = client.get("/llm-usage/")
    assert response.status_code == 200
    assert response.templates[0].name == "submissions/llm_usage.html"
    assert b"7d" in response.content


def test_llm_usage_unrecognized_window_falls_back_to_7d():
    client = Client()
    response = client.get("/llm-usage/", {"window": "bogus"})
    assert response.status_code == 200
    assert response.context["window"] == "7d"


def test_llm_usage_aggregates_window(client=Client()):
    _make_call(
        model="claude-a", cost="1.00", latency_ms=100, status="ok", age=timedelta(days=1)
    )
    _make_call(
        model="claude-a",
        cost="2.00",
        latency_ms=200,
        status="failed",
        error_class="BadResponseError",
        age=timedelta(days=1),
    )
    _make_call(
        model="claude-b",
        cost="0.50",
        latency_ms=300,
        status="failed",
        error_class="RateLimitError",
        age=timedelta(days=1),
    )
    _make_call(model="", cost="0.25", latency_ms=400, status="ok", age=timedelta(days=1))
    # Outside the 7d window entirely - must not affect any total.
    _make_call(model="claude-a", cost="99.00", latency_ms=9999, status="ok", age=timedelta(days=10))

    response = client.get("/llm-usage/", {"window": "7d"})
    assert response.status_code == 200
    ctx = response.context

    assert ctx["total_calls"] == 4
    assert ctx["total_cost"] == Decimal("3.75")
    assert ctx["failed_calls"] == 2
    assert ctx["failure_rate"] == 50.0
    assert ctx["avg_latency_ms"] == 250.0

    by_model = {row["model"]: row for row in ctx["by_model"]}
    assert by_model["claude-a"]["call_count"] == 2
    assert by_model["claude-a"]["total_cost"] == Decimal("3.00")
    assert by_model["claude-a"]["failed_count"] == 1
    assert by_model["claude-a"]["avg_latency_ms"] == 150.0
    assert by_model["claude-b"]["call_count"] == 1
    assert by_model["claude-b"]["total_cost"] == Decimal("0.50")
    assert by_model[""]["model_display"] == "(unknown model)"
    assert by_model[""]["total_cost"] == Decimal("0.25")

    # Sorted descending by total cost: claude-a, claude-b, (unknown model).
    ordered_models = [row["model_display"] for row in ctx["by_model"]]
    assert ordered_models == ["claude-a", "claude-b", "(unknown model)"]

    by_error = {row["error_class"]: row["count"] for row in ctx["by_error_class"]}
    assert by_error == {"BadResponseError": 1, "RateLimitError": 1}

    content = response.content.decode()
    assert "$3.75" in content
    assert "50.0%" in content
    assert "claude-a" in content
    assert "(unknown model)" in content
    assert "BadResponseError" in content
    assert "RateLimitError" in content
    # The excluded, far-older row's distinctive cost must not leak in.
    assert "99.00" not in content


def test_llm_usage_excludes_call_just_outside_window_boundary():
    client = Client()
    # 24h window: a call just past the boundary is excluded, one just
    # inside it is included.
    _make_call(
        model="claude-a",
        cost="5.00",
        latency_ms=100,
        age=timedelta(hours=24, seconds=5),
    )
    _make_call(
        model="claude-a",
        cost="7.00",
        latency_ms=200,
        age=timedelta(hours=23, minutes=59),
    )

    response = client.get("/llm-usage/", {"window": "24h"})
    assert response.status_code == 200
    assert response.context["total_calls"] == 1
    assert response.context["total_cost"] == Decimal("7.00")
    content = response.content.decode()
    assert "5.00" not in content


def test_llm_usage_zero_calls_in_window():
    client = Client()
    response = client.get("/llm-usage/", {"window": "30d"})
    assert response.status_code == 200
    ctx = response.context
    assert ctx["total_calls"] == 0
    assert ctx["total_cost"] == Decimal("0")
    assert ctx["failed_calls"] == 0
    assert ctx["failure_rate"] == 0.0
    content = response.content.decode()
    assert "$0.00" in content
    assert "0.0%" in content


def test_llm_usage_all_calls_failed():
    client = Client()
    _make_call(
        model="claude-a",
        cost="1.50",
        latency_ms=100,
        status="failed",
        error_class="AuthError",
        age=timedelta(hours=1),
    )
    _make_call(
        model="claude-a",
        cost="2.50",
        latency_ms=300,
        status="failed",
        error_class="AuthError",
        age=timedelta(hours=2),
    )

    response = client.get("/llm-usage/", {"window": "24h"})
    assert response.status_code == 200
    ctx = response.context
    assert ctx["total_calls"] == 2
    assert ctx["failed_calls"] == 2
    assert ctx["failure_rate"] == 100.0
    # Cost/latency from failed calls are not excluded.
    assert ctx["total_cost"] == Decimal("4.00")
    assert ctx["avg_latency_ms"] == 200.0
    content = response.content.decode()
    assert "100.0%" in content


def test_llm_usage_trend_buckets_present_for_each_window():
    client = Client()
    _make_call(model="claude-a", cost="1.00", latency_ms=100, age=timedelta(hours=2))

    response_24h = client.get("/llm-usage/", {"window": "24h"})
    assert len(response_24h.context["trend"]) == 1

    response_7d = client.get("/llm-usage/", {"window": "7d"})
    assert len(response_7d.context["trend"]) == 1

    response_30d = client.get("/llm-usage/", {"window": "30d"})
    assert len(response_30d.context["trend"]) == 1
