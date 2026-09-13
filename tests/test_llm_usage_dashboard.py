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


def test_llm_usage_by_provider_json_retry_rate(client=Client()):
    _make_call(
        model="claude-a",
        provider="anthropic",
        cost="1.00",
        latency_ms=100,
        status="ok",
        json_retried=True,
        age=timedelta(hours=1),
    )
    _make_call(
        model="claude-a",
        provider="anthropic",
        cost="2.00",
        latency_ms=200,
        status="ok",
        json_retried=False,
        age=timedelta(hours=1),
    )
    _make_call(
        model="claude-a",
        provider="anthropic",
        cost="3.00",
        latency_ms=300,
        status="failed",
        error_class="RateLimitError",
        json_retried=False,
        age=timedelta(hours=1),
    )
    # Every call for this provider fails - failed_count should equal
    # call_count, and cost/latency must still be counted (recorded
    # regardless of status, same as by_model).
    _make_call(
        model="gpt-4o",
        provider="openai-compatible",
        cost="4.00",
        latency_ms=400,
        status="failed",
        error_class="AuthError",
        json_retried=True,
        age=timedelta(hours=1),
    )
    # Blank provider folds into "(unknown provider)".
    _make_call(
        model="",
        provider="",
        cost="5.00",
        latency_ms=500,
        status="ok",
        json_retried=False,
        age=timedelta(hours=1),
    )

    response = client.get("/llm-usage/", {"window": "24h"})
    assert response.status_code == 200
    ctx = response.context

    by_provider = {row["provider_display"]: row for row in ctx["by_provider"]}
    assert by_provider["anthropic"]["call_count"] == 3
    assert by_provider["anthropic"]["json_retried_count"] == 1
    assert by_provider["anthropic"]["json_retry_rate"] == pytest.approx(33.3)
    assert by_provider["anthropic"]["total_cost"] == Decimal("6.00")
    assert by_provider["anthropic"]["failed_count"] == 1
    assert by_provider["anthropic"]["avg_latency_ms"] == 200.0

    assert by_provider["openai-compatible"]["call_count"] == 1
    assert by_provider["openai-compatible"]["json_retried_count"] == 1
    assert by_provider["openai-compatible"]["json_retry_rate"] == 100.0
    assert by_provider["openai-compatible"]["total_cost"] == Decimal("4.00")
    assert by_provider["openai-compatible"]["failed_count"] == 1
    assert by_provider["openai-compatible"]["avg_latency_ms"] == 400.0

    assert by_provider["(unknown provider)"]["call_count"] == 1
    assert by_provider["(unknown provider)"]["json_retried_count"] == 0
    assert by_provider["(unknown provider)"]["json_retry_rate"] == 0.0
    assert by_provider["(unknown provider)"]["total_cost"] == Decimal("5.00")
    assert by_provider["(unknown provider)"]["failed_count"] == 0
    assert by_provider["(unknown provider)"]["avg_latency_ms"] == 500.0

    content = response.content.decode()
    assert "By provider + model" in content
    assert "anthropic" in content
    assert "33.3%" in content
    assert "$6.00" in content
    assert "$4.00" in content
    assert "$5.00" in content


def test_llm_usage_by_provider_groups_by_provider_and_model():
    """#103: two different models under the same provider get separate
    rows, each with independently correct call_count/json_retried_count/
    json_retry_rate (rather than being averaged together per-provider)."""
    _make_call(
        model="claude-a",
        provider="anthropic",
        cost="1.00",
        latency_ms=100,
        status="ok",
        json_retried=True,
        age=timedelta(hours=1),
    )
    _make_call(
        model="claude-a",
        provider="anthropic",
        cost="1.00",
        latency_ms=100,
        status="ok",
        json_retried=True,
        age=timedelta(hours=1),
    )
    _make_call(
        model="claude-b",
        provider="anthropic",
        cost="2.00",
        latency_ms=200,
        status="ok",
        json_retried=False,
        age=timedelta(hours=1),
    )

    client = Client()
    response = client.get("/llm-usage/", {"window": "24h"})
    ctx = response.context

    rows = {(r["provider_display"], r["model_display"]): r for r in ctx["by_provider"]}
    assert len(ctx["by_provider"]) == 2

    claude_a = rows[("anthropic", "claude-a")]
    assert claude_a["call_count"] == 2
    assert claude_a["json_retried_count"] == 2
    assert claude_a["json_retry_rate"] == 100.0

    claude_b = rows[("anthropic", "claude-b")]
    assert claude_b["call_count"] == 1
    assert claude_b["json_retried_count"] == 0
    assert claude_b["json_retry_rate"] == 0.0

    content = response.content.decode()
    assert "claude-a" in content
    assert "claude-b" in content


def test_llm_usage_by_provider_folds_blank_provider_and_model_independently():
    """A blank provider with a real model, and a real provider with a
    blank model, fold independently rather than collapsing into a single
    combined fallback string."""
    _make_call(
        model="claude-a",
        provider="",
        cost="1.00",
        latency_ms=100,
        status="ok",
        age=timedelta(hours=1),
    )
    _make_call(
        model="",
        provider="anthropic",
        cost="1.00",
        latency_ms=100,
        status="ok",
        age=timedelta(hours=1),
    )

    client = Client()
    response = client.get("/llm-usage/", {"window": "24h"})
    ctx = response.context

    rows = {(r["provider_display"], r["model_display"]): r for r in ctx["by_provider"]}
    assert ("(unknown provider)", "claude-a") in rows
    assert ("anthropic", "(unknown model)") in rows


def test_llm_usage_by_provider_ordered_by_call_count_descending():
    _make_call(
        model="claude-a",
        provider="anthropic",
        cost="1.00",
        latency_ms=100,
        status="ok",
        age=timedelta(hours=1),
    )
    for _ in range(3):
        _make_call(
            model="gpt-4o",
            provider="openai-compatible",
            cost="1.00",
            latency_ms=100,
            status="ok",
            age=timedelta(hours=1),
        )

    client = Client()
    response = client.get("/llm-usage/", {"window": "24h"})
    ctx = response.context

    ordered = [(r["provider_display"], r["model_display"]) for r in ctx["by_provider"]]
    assert ordered[0] == ("openai-compatible", "gpt-4o")
    assert ordered[1] == ("anthropic", "claude-a")


def test_llm_usage_by_provider_empty_state():
    client = Client()
    response = client.get("/llm-usage/", {"window": "30d"})
    assert response.status_code == 200
    assert response.context["by_provider"] == []
    content = response.content.decode()
    assert 'data-role="by-provider-empty"' in content
    assert "By provider + model" in content


def test_llm_usage_trend_buckets_present_for_each_window():
    client = Client()
    _make_call(model="claude-a", cost="1.00", latency_ms=100, age=timedelta(hours=2))

    response_24h = client.get("/llm-usage/", {"window": "24h"})
    assert len(response_24h.context["trend"]) == 1

    response_7d = client.get("/llm-usage/", {"window": "7d"})
    assert len(response_7d.context["trend"]) == 1

    response_30d = client.get("/llm-usage/", {"window": "30d"})
    assert len(response_30d.context["trend"]) == 1
