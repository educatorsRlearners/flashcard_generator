/*
 * Live-updating LLM usage dashboard (issue #89).
 *
 * Plain vanilla JS: fetch + setInterval, no library/dependency. Polls the
 * same `llm_usage` view every 15s with `X-Requested-With: XMLHttpRequest`
 * (the JSON branch added for this issue) and patches the existing
 * `data-role` hooks in place - no full page navigation, no scroll jump.
 *
 * Pure enhancement: if this file fails to load/run, the page already
 * shows the full, correct server-rendered data from the initial request
 * (issue #88); nothing here is required for correct initial data.
 */
(function () {
    "use strict";

    var POLL_INTERVAL_MS = 15000;

    var app = document.getElementById("llm-usage-app");
    if (!app) {
        return;
    }

    var baseUrl = app.getAttribute("data-poll-url");
    var toggle = app.querySelector('[data-role="live-toggle"]');
    var status = app.querySelector('[data-role="live-status"]');

    var live = true;
    var timerId = null;

    function currentWindow() {
        // Read the currently-active window straight from the DOM rather
        // than caching it, so a window switch (full page load) always
        // picks it up correctly on the next script evaluation.
        var active = app.querySelector('[data-role="window-link"].llm-usage__window--active');
        return (active && active.getAttribute("data-window")) || app.getAttribute("data-window");
    }

    function setText(role, text) {
        var el = app.querySelector('[data-role="' + role + '"]');
        if (el) {
            el.textContent = text;
        }
    }

    function buildRows(rows, cellsFor) {
        var tbody = document.createElement("tbody");
        rows.forEach(function (row) {
            var tr = document.createElement("tr");
            cellsFor(row).forEach(function (cell) {
                var td = document.createElement("td");
                td.setAttribute("data-role", cell.role);
                td.textContent = cell.text;
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        return tbody;
    }

    function renderSection(sectionRole, rows, tableRole, emptyRole, emptyMessage, headHtml, cellsFor) {
        var section = app.querySelector('[data-role="' + sectionRole + '"]');
        if (!section) {
            return;
        }
        if (!rows.length) {
            section.innerHTML =
                '<p class="empty-state" data-role="' + emptyRole + '">' + emptyMessage + "</p>";
            return;
        }
        var existingTable = section.querySelector('[data-role="' + tableRole + '"]');
        var tbody = buildRows(rows, cellsFor);
        if (existingTable) {
            var oldTbody = existingTable.querySelector("tbody");
            if (oldTbody) {
                existingTable.replaceChild(tbody, oldTbody);
            } else {
                existingTable.appendChild(tbody);
            }
        } else {
            section.innerHTML =
                '<table class="llm-usage__table" data-role="' +
                tableRole +
                '"><thead>' +
                headHtml +
                "</thead></table>";
            section.querySelector('[data-role="' + tableRole + '"]').appendChild(tbody);
        }
    }

    function applyData(data) {
        setText("total-calls", data.total_calls);
        setText("total-cost", "$" + data.total_cost);
        setText("failed-calls", data.failed_calls);
        setText("failure-rate", data.failure_rate + "%");
        setText("avg-latency", data.avg_latency_ms + " ms");

        renderSection(
            "by-model-section",
            data.by_model,
            "by-model-table",
            "by-model-empty",
            "No calls in this window.",
            "<tr><th scope=\"col\">Model</th><th scope=\"col\">Calls</th>" +
                '<th scope="col">Total cost</th><th scope="col">Failed</th>' +
                '<th scope="col">Avg latency (ms)</th></tr>',
            function (row) {
                return [
                    { role: "model", text: row.model_display },
                    { role: "call-count", text: row.call_count },
                    { role: "total-cost", text: "$" + row.total_cost },
                    { role: "failed-count", text: row.failed_count },
                    { role: "avg-latency", text: row.avg_latency_ms },
                ];
            }
        );

        renderSection(
            "by-provider-section",
            data.by_provider,
            "by-provider-table",
            "by-provider-empty",
            "No calls in this window.",
            '<tr><th scope="col">Provider</th><th scope="col">Calls</th>' +
                '<th scope="col">Total cost</th><th scope="col">Failed</th>' +
                '<th scope="col">Avg latency (ms)</th>' +
                '<th scope="col">JSON-retried</th><th scope="col">JSON-retry rate</th></tr>',
            function (row) {
                return [
                    { role: "provider", text: row.provider_display },
                    { role: "call-count", text: row.call_count },
                    { role: "total-cost", text: "$" + row.total_cost },
                    { role: "failed-count", text: row.failed_count },
                    { role: "avg-latency", text: row.avg_latency_ms },
                    { role: "json-retried-count", text: row.json_retried_count },
                    { role: "json-retry-rate", text: row.json_retry_rate + "%" },
                ];
            }
        );

        renderSection(
            "by-error-class-section",
            data.by_error_class,
            "by-error-class-table",
            "by-error-class-empty",
            "No failed calls in this window.",
            '<tr><th scope="col">Error class</th><th scope="col">Count</th></tr>',
            function (row) {
                return [
                    { role: "error-class", text: row.error_class_display },
                    { role: "error-count", text: row.count },
                ];
            }
        );

        renderSection(
            "trend-section",
            data.trend,
            "trend-table",
            "trend-empty",
            "No calls in this window.",
            '<tr><th scope="col">Bucket (' +
                data.trend_bucket_label +
                ')</th><th scope="col">Calls</th>' +
                '<th scope="col">Total cost</th><th scope="col">Avg latency (ms)</th></tr>',
            function (row) {
                return [
                    { role: "bucket", text: row.bucket },
                    { role: "call-count", text: row.call_count },
                    { role: "total-cost", text: "$" + row.total_cost },
                    { role: "avg-latency", text: row.avg_latency_ms },
                ];
            }
        );
    }

    function poll() {
        var url = baseUrl + "?window=" + encodeURIComponent(currentWindow());
        fetch(url, {
            headers: { "X-Requested-With": "XMLHttpRequest" },
            credentials: "same-origin",
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("bad status " + response.status);
                }
                var contentType = response.headers.get("Content-Type") || "";
                if (contentType.indexOf("application/json") === -1) {
                    throw new Error("non-JSON response");
                }
                return response.json();
            })
            .then(function (data) {
                applyData(data);
            })
            .catch(function () {
                // Fetch failure / non-2xx / non-JSON: keep showing the
                // last-known-good numbers and silently retry on the next
                // tick. No user-visible error, no crash-loop.
            });
    }

    function startPolling() {
        if (timerId !== null) {
            return;
        }
        timerId = window.setInterval(poll, POLL_INTERVAL_MS);
    }

    function stopPolling() {
        if (timerId === null) {
            return;
        }
        window.clearInterval(timerId);
        timerId = null;
    }

    function setLive(nextLive) {
        live = nextLive;
        if (toggle) {
            toggle.textContent = "Live: " + (live ? "On" : "Off");
            toggle.setAttribute("aria-pressed", live ? "true" : "false");
        }
        if (live) {
            if (document.visibilityState === "visible") {
                poll();
                startPolling();
            }
        } else {
            stopPolling();
        }
    }

    if (toggle) {
        toggle.addEventListener("click", function () {
            setLive(!live);
        });
    }

    document.addEventListener("visibilitychange", function () {
        if (!live) {
            return;
        }
        if (document.visibilityState === "visible") {
            // Refresh immediately so the newly-visible tab isn't stale,
            // then resume the normal interval.
            poll();
            startPolling();
        } else {
            stopPolling();
        }
    });

    if (status) {
        status.textContent = "";
    }

    if (document.visibilityState === "visible") {
        startPolling();
    }
})();
