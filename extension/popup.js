// extension/popup.js
//
// Orchestrates the one-button flow (#39): connect to the native host to get
// a bearer token (spawning the backend if needed), extract the active
// tab's content, submit it, poll until card generation finishes, then open
// the review tab. No confirmation dialog or modal anywhere - the only
// manual step in the whole system is the per-card accept/reject already
// in the review UI.
//
// No background service worker: this popup performs connectNative,
// chrome.scripting, fetch, and chrome.tabs calls directly and is expected
// to finish well under a minute with the popup open throughout. Closing the
// popup mid-flow aborts whatever is in flight (a known, accepted v1
// limitation - see #39's "Out of scope"); reopening and clicking again
// starts a fresh submission safely.

(function () {
    var NATIVE_HOST_NAME = "com.flashcard_generator.native_host";
    var BACKEND_ORIGIN = "http://127.0.0.1:8000";
    // How often to re-poll GET .../status/ while waiting for generation to
    // finish. Not prescribed by #39; 1s is a reasonable default.
    var POLL_INTERVAL_MS = 1000;

    var button = document.getElementById("generate");
    var statusEl = document.getElementById("status");

    function setStatus(text, isError) {
        statusEl.textContent = text;
        statusEl.classList.toggle("status--error", !!isError);
    }

    function showError(message) {
        setStatus(message, true);
        button.disabled = false;
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    // Exactly one request/response over connectNative (see host.py) -
    // content is ignored by v1, so any message triggers it.
    function connectNativeHost() {
        return new Promise(function (resolve, reject) {
            var port;
            try {
                port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
            } catch (err) {
                reject(err);
                return;
            }
            var settled = false;
            port.onMessage.addListener(function (reply) {
                settled = true;
                resolve(reply);
                try { port.disconnect(); } catch (err) { /* already gone */ }
            });
            port.onDisconnect.addListener(function () {
                if (settled) { return; }
                var lastError = chrome.runtime.lastError;
                reject(new Error(lastError ? lastError.message : "native host disconnected"));
            });
            port.postMessage({});
        });
    }

    // Injects Readability.js + content_extract.js, then invokes
    // extractPageContent() in the page. Two separate executeScript calls
    // because the isolated-world execution context persists across calls
    // to the same frame (see #39's Context section).
    function extractTabContent(tabId) {
        return chrome.scripting
            .executeScript({
                target: { tabId: tabId },
                files: ["lib/Readability.js", "content_extract.js"],
            })
            .then(function () {
                return chrome.scripting.executeScript({
                    target: { tabId: tabId },
                    func: function () { return extractPageContent(); },
                });
            })
            .then(function (results) {
                var content = results && results[0] && results[0].result;
                if (!content || typeof content.text !== "string" || !content.text) {
                    throw new Error("no extractable content");
                }
                return content;
            });
    }

    // Reads {"error": "..."} out of a non-2xx response body, falling back
    // to the HTTP status if the body isn't JSON or has no `error` field.
    function errorFromResponse(response) {
        return response
            .json()
            .then(function (body) {
                return body && typeof body.error === "string" ? body.error : String(response.status);
            })
            .catch(function () { return String(response.status); });
    }

    // A fetch() call that rejects (network refused, DNS failure, CORS
    // preflight rejected, etc.) is distinguished from a non-2xx HTTP
    // response: the former means "could not reach the backend at all",
    // the latter carries a real error body from the server.
    function fetchOrNetworkError(url, options) {
        return fetch(url, options).catch(function () {
            throw { kind: "network" };
        });
    }

    function pollStatus(submittedUrlId, token) {
        // NOTE (CORS gotcha): both endpoints below only answer this
        // extension's origin when the backend's EXTENSION_ID setting is
        // set to this extension's actual loaded ID. If it isn't, every
        // fetch here fails its CORS preflight with an opaque network
        // error indistinguishable from the backend being unreachable -
        // see README.md's "Browser extension setup" section.
        return fetchOrNetworkError(
            BACKEND_ORIGIN + "/api/extension/submit/" + submittedUrlId + "/status/",
            { headers: { Authorization: "Bearer " + token } }
        ).then(function (response) {
            if (!response.ok) {
                return errorFromResponse(response).then(function (message) {
                    throw { kind: "http", message: message };
                });
            }
            return response.json();
        }).then(function (data) {
            if (!data.terminal) {
                return sleep(POLL_INTERVAL_MS).then(function () {
                    return pollStatus(submittedUrlId, token);
                });
            }
            if (data.review_url) {
                setStatus("Done — review tab opened.");
                chrome.tabs.create({ url: data.review_url });
                return; // leave the button disabled - nothing left to retry
            }
            throw { kind: "http", message: data.generation_error || "card generation failed" };
        });
    }

    function submitContent(content, tabUrl, token) {
        // See the CORS note in pollStatus() above - it applies to this
        // fetch too.
        return fetchOrNetworkError(BACKEND_ORIGIN + "/api/extension/submit/", {
            method: "POST",
            headers: {
                Authorization: "Bearer " + token,
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ url: tabUrl, title: content.title || "", text: content.text }),
        }).then(function (response) {
            if (!response.ok) {
                return errorFromResponse(response).then(function (message) {
                    throw { kind: "http", message: message };
                });
            }
            return response.json();
        }).then(function (data) {
            return pollStatus(data.submitted_url_id, token);
        });
    }

    function runFlow() {
        button.disabled = true;
        setStatus("Connecting to backend…");

        return connectNativeHost().catch(function (err) {
            throw { kind: "native-connect", message: err && err.message };
        }).then(function (reply) {
            if (!reply.ok) {
                throw { kind: "native-error", message: reply.detail };
            }
            var token = reply.token;

            setStatus("Reading page…");
            return chrome.tabs.query({ active: true, currentWindow: true }).then(function (tabs) {
                var tab = tabs[0];
                return extractTabContent(tab.id).catch(function () {
                    throw { kind: "extract" };
                }).then(function (content) {
                    setStatus("Generating cards…");
                    return submitContent(content, tab.url, token);
                });
            });
        }).catch(function (err) {
            if (err && err.kind === "native-connect") {
                showError("Error: could not reach native host — is the extension registered? See README.md.");
            } else if (err && err.kind === "native-error") {
                showError("Error: " + err.message);
            } else if (err && err.kind === "extract") {
                showError(
                    "Error: can't read this page (chrome:// and extension pages " +
                    "aren't supported — open a regular webpage and try again)."
                );
            } else if (err && err.kind === "http") {
                showError("Error: " + err.message);
            } else if (err && err.kind === "network") {
                showError(
                    "Error: could not reach the backend. Is it running? " +
                    "(See README.md if this is the first run — the EXTENSION_ID " +
                    "setting may not match this extension's ID.)"
                );
            } else {
                // Unexpected error (a bug, not one of the flow's documented
                // failure modes) - still surface something rather than
                // leaving the button stuck disabled.
                showError("Error: " + (err && err.message ? err.message : "something went wrong."));
            }
        });
    }

    button.addEventListener("click", function () {
        runFlow();
    });
}());
