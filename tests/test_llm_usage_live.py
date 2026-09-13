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


# --- Issue #97: change-aware setText for the aria-live totals box. ---
#
# The five `.review-tally` spans are written only via `setText` in
# `submissions/static/submissions/llm_usage_live.js`. These tests drive
# the real `setText` source (extracted verbatim from that file) under
# Node with a minimal fake DOM that counts `textContent` writes, so an
# unchanged poll triggers no DOM write (no screen-reader re-announce).

import json
import pathlib
import re
import subprocess

_LIVE_JS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "submissions"
    / "static"
    / "submissions"
    / "llm_usage_live.js"
)

_SETTEXT_RE = re.compile(r"function setText\(role, text\) \{[\s\S]*?\n    \}")

_FAKE_DOM_PREAMBLE = """
function makeEl(initial) {
    var _t = initial;
    var writes = 0;
    var el = {};
    Object.defineProperty(el, "textContent", {
        get: function () { return _t; },
        set: function (v) { writes += 1; _t = v; },
        configurable: true,
        enumerable: true
    });
    el._writes = function () { return writes; };
    el._value = function () { return _t; };
    return el;
}
"""


def _load_settext_source():
    src = _LIVE_JS_PATH.read_text()
    match = _SETTEXT_RE.search(src)
    assert match, "setText helper not found in llm_usage_live.js"
    return match.group(0)


def _run_settext_probe(setup_js, calls_js, report_js):
    """Run the real `setText` from the shipped JS under Node.

    The fake DOM exposes `elements` (name -> fake el with a
    counting `textContent` setter) and `app.querySelector`, so the
    probe can assert write/no-write per span.
    """
    settext_src = _load_settext_source()
    script = (
        _FAKE_DOM_PREAMBLE
        + setup_js
        + "\nvar app = { querySelector: function (sel) {"
        + " var m = /\\[data-role=\"([^\"]+)\"\\]/.exec(sel);"
        + " if (!m) { return null; }"
        + " return elements[m[1]] || null; } };"
        + "\n"
        + settext_src
        + "\n"
        + calls_js
        + "\n"
        + report_js
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, f"node probe failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_settext_source_is_change_aware_with_string_coercion():
    """Guard the comparison itself: String() before ===, early return."""
    src = _load_settext_source()
    assert "String(" in src
    assert "===" in src
    # Missing target stays a silent no-op.
    assert "if (!el)" in src


def test_settext_skips_write_when_value_unchanged():
    """Unchanged poll: no DOM write, so aria-live does not re-announce."""
    result = _run_settext_probe(
        'var elements = { "total-calls": makeEl("5") };',
        'setText("total-calls", "5");',
        'console.log(JSON.stringify({writes: elements["total-calls"]._writes(),'
        ' value: elements["total-calls"]._value()}));',
    )
    assert result == {"writes": 0, "value": "5"}


def test_settext_writes_when_value_changed():
    result = _run_settext_probe(
        'var elements = { "total-calls": makeEl("5") };',
        'setText("total-calls", "6");',
        'console.log(JSON.stringify({writes: elements["total-calls"]._writes(),'
        ' value: elements["total-calls"]._value()}));',
    )
    assert result == {"writes": 1, "value": "6"}


def test_settext_partial_change_only_changed_span_written():
    """Only the differing span is written; the other four are untouched."""
    result = _run_settext_probe(
        "var elements = {"
        ' "total-calls": makeEl("5"),'
        ' "total-cost": makeEl("$0.00"),'
        ' "failed-calls": makeEl("0"),'
        ' "failure-rate": makeEl("0.0%"),'
        ' "avg-latency": makeEl("0 ms") };',
        'setText("total-calls", "6");'
        ' setText("total-cost", "$0.00");'
        ' setText("failed-calls", "0");'
        ' setText("failure-rate", "0.0%");'
        ' setText("avg-latency", "0 ms");',
        "console.log(JSON.stringify({"
        ' writes: [elements["total-calls"]._writes(),'
        ' elements["total-cost"]._writes(),'
        ' elements["failed-calls"]._writes(),'
        ' elements["failure-rate"]._writes(),'
        ' elements["avg-latency"]._writes()],'
        ' value: elements["total-calls"]._value()}));',
    )
    assert result["writes"] == [1, 0, 0, 0, 0]
    assert result["value"] == "6"


def test_settext_numeric_count_coerces_before_compare():
    """JSON numbers (`5`) compare equal to displayed text (`"5"`)."""
    result = _run_settext_probe(
        'var elements = { "total-calls": makeEl("5"),'
        ' "failed-calls": makeEl("0") };',
        "setText(\"total-calls\", 5); setText(\"failed-calls\", 1);",
        "console.log(JSON.stringify({"
        ' callsWrites: elements["total-calls"]._writes(),'
        ' failedWrites: elements["failed-calls"]._writes(),'
        ' failedValue: elements["failed-calls"]._value()}));',
    )
    assert result["callsWrites"] == 0
    assert result["failedWrites"] == 1
    assert result["failedValue"] == "1"


def test_settext_missing_target_is_silent_noop():
    """A missing data-role target: no exception, other spans still update."""
    result = _run_settext_probe(
        'var elements = { "total-calls": makeEl("5") };',
        'setText("no-such-role", "x"); setText("total-calls", "6");',
        'console.log(JSON.stringify({ok: true,'
        ' writes: elements["total-calls"]._writes(),'
        ' value: elements["total-calls"]._value()}));',
    )
    assert result == {"ok": True, "writes": 1, "value": "6"}


def test_review_tally_keeps_aria_live_polite():
    """#97 changes writes, not markup: the totals box stays aria-live."""
    client = Client()
    content = client.get("/llm-usage/").content.decode()
    assert '<p class="review-tally" aria-live="polite">' in content
