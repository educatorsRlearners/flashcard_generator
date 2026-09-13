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


# --- Issue #123: change-aware renderSection tables. ---
#
# Drive the real `renderSection`/`buildRows` source (extracted verbatim
# from `llm_usage_live.js`) under Node with a minimal fake DOM. The fake
# implements just enough of the DOM API the section code uses
# (`createElement`, `querySelector(All)`, `appendChild`, `replaceChild`,
# `innerHTML` for the two HTML shapes `renderSection` writes, plus
# `textContent` / `data-role`), counting `innerHTML` writes and
# `replaceChild` calls so unchanged polls assert zero mutations and
# changed polls assert replacement with correctly formatted cells.


def _load_section_source():
    src = _LIVE_JS_PATH.read_text()
    start = src.find("function buildRows")
    end = src.find("function applyData")
    assert start != -1 and end != -1 and start < end, "section helpers not found"
    return src[start:end]


_FAKE_SECTION_DOM = """
function FakeEl(tag) {
    this.tagName = (tag || "div").toLowerCase();
    this.attrs = {};
    this.children = [];
    this._text = "";
    this.parent = null;
    this._innerWrites = 0;
    this._replaceCalls = 0;
}
FakeEl.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
FakeEl.prototype.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
};
Object.defineProperty(FakeEl.prototype, "textContent", {
    get: function () {
        if (this.children.length) {
            return this.children.map(function (c) { return c.textContent; }).join("");
        }
        return this._text;
    },
    set: function (v) { this._text = String(v); this.children = []; },
    configurable: true
});
Object.defineProperty(FakeEl.prototype, "innerHTML", {
    get: function () { return this._html || ""; },
    set: function (html) {
        this._innerWrites += 1;
        this._html = String(html);
        this.children = [];
        this._text = "";
        var h = String(html);
        if (h.indexOf("<table") !== -1) {
            var roleM = /data-role="([^"]+)"/.exec(h);
            var headM = /<thead>([\\s\\S]*?)<\\/thead>/.exec(h);
            var wrap = new FakeEl("div");
            var table = new FakeEl("table");
            if (roleM) { table.setAttribute("data-role", roleM[1]); }
            var thead = new FakeEl("thead");
            var headInner = headM ? headM[1] : "";
            thead._text = headInner.replace(/<[^>]*>/g, " ");
            table.children.push(thead); thead.parent = table;
            wrap.children.push(table); table.parent = wrap;
            this.children.push(wrap); wrap.parent = this;
        } else if (h.indexOf("empty-state") !== -1) {
            var eM = /data-role="([^"]+)"/.exec(h);
            var tM = />([^<>]*)<\\/p>\\s*$/.exec(h);
            var p = new FakeEl("p");
            if (eM) { p.setAttribute("data-role", eM[1]); }
            p._text = tM ? tM[1] : "";
            this.children.push(p); p.parent = this;
        }
    },
    configurable: true
});
FakeEl.prototype.appendChild = function (child) {
    this.children.push(child); child.parent = this; return child;
};
FakeEl.prototype.replaceChild = function (next, old) {
    this._replaceCalls += 1;
    var i = this.children.indexOf(old);
    if (i === -1) { throw new Error("replaceChild: old not found"); }
    this.children[i] = next; next.parent = this; old.parent = null;
    return old;
};
function _matches(el, sel) {
    var m = /^\\[data-role="([^"]+)"\\]$/.exec(sel);
    if (m) { return el.getAttribute("data-role") === m[1]; }
    return el.tagName === sel.toLowerCase();
}
FakeEl.prototype.querySelector = function (sel) {
    var stack = this.children.slice();
    while (stack.length) {
        var el = stack.shift();
        if (_matches(el, sel)) { return el; }
        stack = el.children.concat(stack);
    }
    return null;
};
FakeEl.prototype.querySelectorAll = function (sel) {
    var out = [];
    (function walk(el) {
        el.children.forEach(function (c) {
            if (_matches(c, sel)) { out.push(c); }
            walk(c);
        });
    })(this);
    return out;
};
"""


def _run_section_probe(probe_js):
    section_src = _load_section_source()
    script = (
        _FAKE_SECTION_DOM
        + "\nvar document = { createElement: function (t) { return new FakeEl(t); } };\n"
        + section_src
        + "\n"
        + probe_js
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node probe failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


_BY_MODEL_CELLS = """
function byModelCells(row) {
    return [
        { role: "model", text: row.model_display },
        { role: "call-count", text: row.call_count },
        { role: "total-cost", text: "$" + row.total_cost },
        { role: "failed-count", text: row.failed_count },
        { role: "avg-latency", text: row.avg_latency_ms }
    ];
}
var HEAD = "<tr><th>Model</th></tr>";
"""

_SETUP_SECTION = """
var section = new FakeEl("div");
section.setAttribute("data-role", "by-model-section");
var app = { querySelector: function (sel) {
    var m = /\\[data-role="([^"]+)"\\]/.exec(sel);
    if (m && section.getAttribute("data-role") === m[1]) { return section; }
    return section.querySelector(sel);
} };
"""


def test_rendersection_source_compares_formatted_strings():
    src = _load_section_source()
    assert "String(" in src
    assert "querySelector" in src
    assert "replaceChild" in src


def test_rendersection_unchanged_keeps_tbody_identity():
    result = _run_section_probe(
        _BY_MODEL_CELLS
        + _SETUP_SECTION
        + """
var rows = [{model_display: "m", call_count: 5, total_cost: "1.00", failed_count: 1, avg_latency_ms: 10}];
renderSection("by-model-section", rows, "by-model-table", "by-model-empty",
    "No calls in this window.", HEAD, byModelCells);
var table = section.querySelector('[data-role="by-model-table"]');
var tbodyBefore = table.querySelector("tbody");
var writesBefore = section._innerWrites;
var replacesBefore = table._replaceCalls;
renderSection("by-model-section", [{model_display: "m", call_count: 5, total_cost: "1.00", failed_count: 1, avg_latency_ms: 10}],
    "by-model-table", "by-model-empty", "No calls in this window.", HEAD, byModelCells);
var tbodyAfter = table.querySelector("tbody");
console.log(JSON.stringify({
    same: tbodyBefore === tbodyAfter,
    innerWrites: section._innerWrites - writesBefore,
    replaces: table._replaceCalls - replacesBefore
}));
"""
    )
    assert result == {"same": True, "innerWrites": 0, "replaces": 0}


def test_rendersection_changed_replaces_with_formatting():
    result = _run_section_probe(
        _BY_MODEL_CELLS
        + _SETUP_SECTION
        + """
var rows = [{model_display: "m", call_count: 5, total_cost: "1.00", failed_count: 1, avg_latency_ms: 10}];
renderSection("by-model-section", rows, "by-model-table", "by-model-empty",
    "No calls in this window.", HEAD, byModelCells);
var table = section.querySelector('[data-role="by-model-table"]');
var tbodyBefore = table.querySelector("tbody");
renderSection("by-model-section", [{model_display: "m", call_count: 6, total_cost: "1.00", failed_count: 1, avg_latency_ms: 10}],
    "by-model-table", "by-model-empty", "No calls in this window.", HEAD, byModelCells);
var tbodyAfter = table.querySelector("tbody");
var cells = tbodyAfter.querySelectorAll("td").map(function (td) { return [td.getAttribute("data-role"), td.textContent]; });
console.log(JSON.stringify({
    replaced: tbodyBefore !== tbodyAfter,
    replaces: table._replaceCalls,
    cells: cells
}));
"""
    )
    assert result["replaced"] is True
    assert result["replaces"] == 1
    assert result["cells"] == [
        ["model", "m"],
        ["call-count", "6"],
        ["total-cost", "$1.00"],
        ["failed-count", "1"],
        ["avg-latency", "10"],
    ]


def test_rendersection_numeric_type_only_is_equal():
    result = _run_section_probe(
        _BY_MODEL_CELLS
        + _SETUP_SECTION
        + """
renderSection("by-model-section",
    [{model_display: "m", call_count: 5, total_cost: "1.00", failed_count: 0, avg_latency_ms: 10}],
    "by-model-table", "by-model-empty", "No calls in this window.", HEAD, byModelCells);
var table = section.querySelector('[data-role="by-model-table"]');
var tbodyBefore = table.querySelector("tbody");
renderSection("by-model-section",
    [{model_display: "m", call_count: "5", total_cost: "1.00", failed_count: "0", avg_latency_ms: "10"}],
    "by-model-table", "by-model-empty", "No calls in this window.", HEAD, byModelCells);
console.log(JSON.stringify({ same: tbodyBefore === table.querySelector("tbody"), replaces: table._replaceCalls }));
"""
    )
    assert result == {"same": True, "replaces": 0}


def test_rendersection_row_order_counts_as_changed():
    result = _run_section_probe(
        _BY_MODEL_CELLS
        + _SETUP_SECTION
        + """
function mk(n) { return {model_display: n, call_count: 1, total_cost: "0.00", failed_count: 0, avg_latency_ms: 1}; }
renderSection("by-model-section", [mk("a"), mk("b")],
    "by-model-table", "by-model-empty", "No calls in this window.", HEAD, byModelCells);
var table = section.querySelector('[data-role="by-model-table"]');
var tbodyBefore = table.querySelector("tbody");
renderSection("by-model-section", [mk("b"), mk("a")],
    "by-model-table", "by-model-empty", "No calls in this window.", HEAD, byModelCells);
var first = table.querySelector("tbody").querySelectorAll("tr")[0].querySelectorAll("td")[0].textContent;
console.log(JSON.stringify({ replaced: tbodyBefore !== table.querySelector("tbody"), first: first }));
"""
    )
    assert result == {"replaced": True, "first": "b"}


def test_rendersection_empty_transitions():
    result = _run_section_probe(
        _BY_MODEL_CELLS
        + _SETUP_SECTION
        + """
var out = {};
renderSection("by-model-section", [], "by-model-table", "by-model-empty",
    "No calls in this window.", HEAD, byModelCells);
var emptyBefore = section.querySelector('[data-role="by-model-empty"]');
var w0 = section._innerWrites;
renderSection("by-model-section", [], "by-model-table", "by-model-empty",
    "No calls in this window.", HEAD, byModelCells);
out.emptyNoWrite = (section._innerWrites - w0) === 0;
out.emptySame = emptyBefore === section.querySelector('[data-role="by-model-empty"]');
renderSection("by-model-section",
    [{model_display: "m", call_count: 1, total_cost: "0.00", failed_count: 0, avg_latency_ms: 1}],
    "by-model-table", "by-model-empty", "No calls in this window.", HEAD, byModelCells);
out.hasTable = !!section.querySelector('[data-role="by-model-table"]');
out.emptyGone = !section.querySelector('[data-role="by-model-empty"]');
renderSection("by-model-section", [], "by-model-table", "by-model-empty",
    "No calls in this window.", HEAD, byModelCells);
out.backToEmpty = !!section.querySelector('[data-role="by-model-empty"]');
out.tableGone = !section.querySelector('[data-role="by-model-table"]');
out.msg = section.querySelector('[data-role="by-model-empty"]').textContent;
console.log(JSON.stringify(out));
"""
    )
    assert result == {
        "emptyNoWrite": True,
        "emptySame": True,
        "hasTable": True,
        "emptyGone": True,
        "backToEmpty": True,
        "tableGone": True,
        "msg": "No calls in this window.",
    }


def test_rendersection_trend_header_change_rebuilds():
    result = _run_section_probe(
        """
function trendCells(row) {
    return [
        { role: "bucket", text: row.bucket },
        { role: "call-count", text: row.call_count },
        { role: "total-cost", text: "$" + row.total_cost },
        { role: "avg-latency", text: row.avg_latency_ms }
    ];
}
var section = new FakeEl("div");
section.setAttribute("data-role", "trend-section");
var app = { querySelector: function (sel) {
    var m = /\\[data-role="([^"]+)"\\]/.exec(sel);
    if (m && section.getAttribute("data-role") === m[1]) { return section; }
    return section.querySelector(sel);
} };
var rows = [{bucket: "b", call_count: 1, total_cost: "0.00", avg_latency_ms: 1}];
var headHour = "<tr><th>Bucket (hour)</th></tr>";
var headDay = "<tr><th>Bucket (day)</th></tr>";
renderSection("trend-section", rows, "trend-table", "trend-empty",
    "No calls in this window.", headHour, trendCells);
var tableBefore = section.querySelector('[data-role="trend-table"]');
var tbodyBefore = tableBefore.querySelector("tbody");
var w0 = section._innerWrites;
renderSection("trend-section", rows, "trend-table", "trend-empty",
    "No calls in this window.", headHour, trendCells);
var sameHeadNoWrite = (section._innerWrites - w0) === 0 && tbodyBefore === section.querySelector('[data-role="trend-table"]').querySelector("tbody");
renderSection("trend-section", rows, "trend-table", "trend-empty",
    "No calls in this window.", headDay, trendCells);
var tableAfter = section.querySelector('[data-role="trend-table"]');
console.log(JSON.stringify({
    sameHeadNoWrite: sameHeadNoWrite,
    rebuilt: tableBefore !== tableAfter,
    headHasDay: tableAfter.querySelector("thead").textContent.indexOf("day") !== -1
}));
"""
    )
    assert result == {"sameHeadNoWrite": True, "rebuilt": True, "headHasDay": True}


def test_rendersection_missing_section_is_silent_noop():
    result = _run_section_probe(
        _BY_MODEL_CELLS
        + """
var app = { querySelector: function (sel) { return null; } };
renderSection("by-model-section", [{model_display: "m", call_count: 1, total_cost: "0.00", failed_count: 0, avg_latency_ms: 1}],
    "by-model-table", "by-model-empty", "No calls in this window.", HEAD, byModelCells);
console.log(JSON.stringify({ ok: true }));
"""
    )
    assert result == {"ok": True}


def test_llm_usage_tables_have_no_aria_live():
    """#123 adds no aria-live to the four table sections (markup unchanged)."""
    client = Client()
    content = client.get("/llm-usage/").content.decode()
    for role in ("by-model-section", "by-provider-section", "by-error-class-section", "trend-section"):
        idx = content.find(f'data-role="{role}"')
        assert idx != -1
        snippet = content[max(0, idx - 200):idx]
        assert "aria-live" not in snippet

