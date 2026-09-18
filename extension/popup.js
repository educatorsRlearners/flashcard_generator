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
    // How often to re-poll GET .../status/ while waiting for generation to
    // finish. Not prescribed by #39; 1s is a reasonable default.
    var POLL_INTERVAL_MS = 1000;

    var button = document.getElementById("generate");
    var statusEl = document.getElementById("status");
    var spinnerEl = document.getElementById("spinner");
    var deckSelect = document.getElementById("deck-select");
    var deckNew = document.getElementById("deck-new");
    var deckNote = document.getElementById("deck-note");
    var providerSelect = document.getElementById("provider-select");
    var modelSelect = document.getElementById("model-select");
    var llmBanner = document.getElementById("llm-key-banner");

    // LLM selector state (issues #107/#108/#109). llmConfigCache is the
    // cached GET .../llm-config/ response ({providers, byName, default})
    // reused for every provider change so switching never re-fetches.
    // llmReady means the dropdowns are populated from a live config and
    // submit may include provider/model; flowRunning tracks runFlow()'s
    // hold on the Generate button so the banner gating never re-enables
    // it mid-generation.
    var llmConfigCache = null;
    var llmReady = false;
    var flowRunning = false;
    var llmChangeListenersAttached = false;

    function setStatus(text, isError) {
        statusEl.textContent = text;
        statusEl.classList.toggle("status--error", !!isError);
    }

    // Shows/hides the CSS spinner next to the status text. Called for
    // every in-progress stage (connecting/reading/generating) so a
    // cold-start wait (up to ~30s with unchanged status text) doesn't
    // read as frozen; turned off on every terminal state (done or error).
    function setBusy(isBusy) {
        spinnerEl.hidden = !isBusy;
    }

    function showError(message) {
        setBusy(false);
        setStatus(message, true);
        flowRunning = false;
        button.disabled = false;
        // Re-apply the missing-key gating (#109): if the banner condition
        // holds, the button goes straight back to disabled.
        updateLlmBanner();
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    // Deck picker (issue #76): dropdown of live decks via the backend
    // passthrough plus free-text new-deck input; typed name wins. A cached
    // backend promise so the deck list (loaded on popup open) and submit
    // share one native-host handshake.
    var backendPromise = null;

    function ensureBackend() {
        if (!backendPromise) {
            backendPromise = (async function () {
                var reply = await connectNativeHost();
                if (!reply || !reply.ok) {
                    throw { kind: "native-error", message: (reply && reply.detail) || "native host error" };
                }
                if (typeof reply.base_url !== "string" || !reply.base_url) {
                    throw { kind: "native-error", message: "invalid base_url from native host" };
                }
                return { token: reply.token, baseUrl: reply.base_url };
            }());
            // A failed handshake must not poison later retries (the Generate
            // click reconnects instead of reusing the rejection).
            backendPromise.catch(function () { backendPromise = null; });
        }
        return backendPromise;
    }

    function setDecksUnavailable() {
        // Unreachable deck list => free-text only, submit NOT blocked.
        deckSelect.textContent = "";
        var opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "Deck list unavailable — type a deck name to continue.";
        deckSelect.appendChild(opt);
        deckSelect.disabled = true;
        if (deckNote) {
            deckNote.textContent = "Deck list unavailable — type a deck name to continue.";
        }
    }

    async function loadDecks() {
        if (!deckSelect) { return; }
        var backend;
        try {
            backend = await ensureBackend();
        } catch (err) {
            // Native-host handshake failed: leave the loading placeholder;
            // runFlow() will surface the real error on Generate click.
            return;
        }
        try {
            var response = await fetchOrNetworkError(backend.baseUrl + "/api/extension/decks/", {
                headers: { Authorization: "Bearer " + backend.token },
            });
            if (!response.ok) { throw { kind: "http" }; }
            var data = await response.json();
            deckSelect.textContent = "";
            var decks = (data && data.decks) || [];
            var placeholder = document.createElement("option");
            placeholder.value = "";
            placeholder.textContent = decks.length ? "Select a deck…" : "No decks yet — type a name";
            deckSelect.appendChild(placeholder);
            decks.forEach(function (name) {
                var opt = document.createElement("option");
                opt.value = name;
                opt.textContent = name;
                deckSelect.appendChild(opt);
            });
            deckSelect.disabled = false;
            if (data && data.unavailable) { setDecksUnavailable(); }
        } catch (err) {
            // Any deck-list failure (backend down, Anki unreachable,
            // CORS/HTTP) degrades to free-text only, never blocks submit.
            setDecksUnavailable();
        }
    }

    function chosenDeckName() {
        // Typed free-text wins over the dropdown.
        var typed = deckNew && typeof deckNew.value === "string" ? deckNew.value.trim() : "";
        if (typed) { return typed; }
        var picked = deckSelect && !deckSelect.disabled && typeof deckSelect.value === "string"
            ? deckSelect.value.trim() : "";
        return picked;
    }

    // LLM provider/model selector (issues #107/#108/#109): dropdowns
    // populated from GET .../llm-config/ (#105 shape:
    // {"providers": [{"name", "models", "key_configured"}], "default":
    // {"provider", "model"}}), persisted via chrome.storage.local key
    // `llmSelection` -> {provider, model}, with a missing-key banner that
    // gates the Generate button. Curated model lists are backend-owned -
    // no provider/model names are hardcoded here.
    var LLM_SELECTION_KEY = "llmSelection";

    // Reads the stored {provider, model}; never rejects - an unreadable
    // chrome.storage.local (or a missing `storage` manifest permission,
    // which leaves chrome.storage undefined) degrades silently to null
    // so the popup still opens seeded from the .env default (#108).
    function readStoredLlmSelection() {
        return new Promise(function (resolve) {
            var storage = null;
            try {
                storage = chrome.storage && chrome.storage.local ? chrome.storage.local : null;
            } catch (err) { storage = null; }
            if (!storage) { resolve(null); return; }
            try {
                storage.get(LLM_SELECTION_KEY, function (items) {
                    try {
                        if (chrome.runtime && chrome.runtime.lastError) { resolve(null); return; }
                    } catch (err) { /* ignore */ }
                    var value = items ? items[LLM_SELECTION_KEY] : null;
                    resolve(value === undefined ? null : value);
                });
            } catch (err) {
                resolve(null);
            }
        });
    }

    // Fire-and-forget save-on-change (#108); write failures degrade
    // silently, never surfacing in the popup.
    function writeStoredLlmSelection(selection) {
        try {
            var storage = chrome.storage && chrome.storage.local ? chrome.storage.local : null;
            if (!storage) { return; }
            var items = {};
            items[LLM_SELECTION_KEY] = selection;
            var result = storage.set(items, function () { /* ignore lastError */ });
            // MV3 promise-style storage returns a thenable; swallow async
            // rejections so a denied write never becomes uncaught.
            if (result && typeof result.catch === "function") {
                result.catch(function () { /* persistence silently degrades */ });
            }
        } catch (err) { /* persistence silently degrades */ }
    }

    function llmProviderEntry(name) {
        if (!llmConfigCache || !llmConfigCache.byName) { return undefined; }
        return llmConfigCache.byName[name];
    }

    function clearSelect(selectEl) {
        // textContent = "" removes all options without innerHTML.
        selectEl.textContent = "";
    }

    function addSelectOption(selectEl, value, text) {
        var opt = document.createElement("option");
        opt.value = value;
        opt.textContent = text;
        selectEl.appendChild(opt);
        return opt;
    }

    // .env-default fallback for seeding (#107): unknown default provider
    // falls back to the first provider, unknown default model to the
    // first model of the chosen provider. Never blocks Generate.
    function resolveDefaultLlm(providers, byName, def) {
        var providerName = (def && typeof def.provider === "string" && byName[def.provider])
            ? def.provider : providers[0].name;
        var models = byName[providerName].models || [];
        var modelName = (def && providerName === def.provider && typeof def.model === "string" &&
            models.indexOf(def.model) !== -1) ? def.model : (models[0] || "");
        return { provider: providerName, model: modelName };
    }

    function populateModelSelect(providerName, selectedModel) {
        var entry = llmProviderEntry(providerName);
        var models = (entry && Array.isArray(entry.models)) ? entry.models : [];
        clearSelect(modelSelect);
        if (!models.length) {
            // Provider with an empty model list: disabled, submit omits
            // `model` (#107 edge case).
            addSelectOption(modelSelect, "", "No models available for this provider.");
            modelSelect.disabled = true;
            return;
        }
        models.forEach(function (modelName) {
            addSelectOption(modelSelect, modelName, modelName);
        });
        modelSelect.disabled = false;
        modelSelect.value = models.indexOf(selectedModel) !== -1 ? selectedModel : models[0];
    }

    // Missing-key banner + Generate gating (#109). Presence check only:
    // reads key_configured off the cached config, never re-fetches.
    // Fail-open: unknown key state (no cache, absent DOM, unknown
    // provider) shows no banner and leaves Generate enabled.
    function updateLlmBanner() {
        if (!llmBanner || !providerSelect || !button) { return; }
        var entry = llmProviderEntry(providerSelect.value);
        if (!entry || entry.key_configured) {
            llmBanner.textContent = "";
            llmBanner.hidden = true;
            if (!flowRunning) { button.disabled = false; }
            return;
        }
        var displayName = providerSelect.value;
        try {
            var selected = providerSelect.selectedOptions && providerSelect.selectedOptions[0];
            if (selected && selected.textContent) { displayName = selected.textContent; }
        } catch (err) { /* fall back to the raw provider id */ }
        llmBanner.textContent =
            "No API key configured for " + displayName +
            " — add it to your backend `.env` and restart.";
        llmBanner.hidden = false;
        button.disabled = true;
    }

    function onProviderChange() {
        var name = providerSelect.value;
        var entry = llmProviderEntry(name);
        if (!entry) { return; } // degraded/unknown state: leave storage alone
        var models = Array.isArray(entry.models) ? entry.models : [];
        // Reset to the provider's first model, or its .env-default model
        // when it belongs to the newly selected provider (#107).
        var def = llmConfigCache.default || {};
        var nextModel = (name === def.provider && typeof def.model === "string" &&
            models.indexOf(def.model) !== -1) ? def.model : (models[0] || "");
        populateModelSelect(name, nextModel);
        writeStoredLlmSelection({ provider: name, model: modelSelect.disabled ? "" : modelSelect.value });
        updateLlmBanner();
    }

    function onModelChange() {
        if (!llmProviderEntry(providerSelect.value)) { return; }
        writeStoredLlmSelection({ provider: providerSelect.value, model: modelSelect.value });
    }

    // Applies a usable config response: seeds from stored-beats-default
    // (#108), renders both dropdowns, persists first-use/stale seeds,
    // and syncs the banner (#109).
    function applyLlmConfig(data, stored) {
        var providers = (data.providers || []).filter(function (p) {
            return p && typeof p.name === "string" && p.name && Array.isArray(p.models);
        });
        if (!providers.length) { degradeLlmConfig(); return; }
        var byName = {};
        providers.forEach(function (p) { byName[p.name] = p; });
        var def = (data.default && typeof data.default === "object") ? data.default : {};
        llmConfigCache = { providers: providers, byName: byName, default: def };

        var seed;
        var needsWrite = false;
        var storedUsable = stored && typeof stored.provider === "string" &&
            typeof stored.model === "string" && !!byName[stored.provider] &&
            ((byName[stored.provider].models || []).indexOf(stored.model) !== -1 ||
                ((byName[stored.provider].models || []).length === 0 && stored.model === ""));
        if (storedUsable) {
            seed = { provider: stored.provider, model: stored.model };
        } else {
            if (stored && typeof stored.provider === "string" && typeof stored.model === "string" &&
                stored.provider && byName[stored.provider]) {
                // Valid provider, stale model: keep the provider, fall
                // back to its first curated model (#108).
                var keptModels = byName[stored.provider].models || [];
                seed = { provider: stored.provider, model: keptModels[0] || "" };
            } else {
                // First-ever use (missing/corrupt) or stale provider:
                // seed from the .env default (#107/#108).
                seed = resolveDefaultLlm(providers, byName, def);
            }
            needsWrite = true;
        }

        clearSelect(providerSelect);
        providers.forEach(function (p) {
            // Option text is the backend id verbatim - the banner's
            // display name is read back from the selected option (#109).
            addSelectOption(providerSelect, p.name, p.name);
        });
        providerSelect.disabled = false;
        providerSelect.value = seed.provider;
        populateModelSelect(seed.provider, seed.model);

        if (!llmChangeListenersAttached) {
            providerSelect.addEventListener("change", onProviderChange);
            modelSelect.addEventListener("change", onModelChange);
            llmChangeListenersAttached = true;
        }

        llmReady = true;
        if (needsWrite) {
            writeStoredLlmSelection({
                provider: providerSelect.value,
                model: modelSelect.disabled ? "" : modelSelect.value,
            });
        }
        updateLlmBanner();
    }

    // Graceful degrade (#107): single disabled options, Generate stays
    // enabled, submit omits provider/model so the backend falls back to
    // .env-implicit behavior (#106). Mirrors the deck picker's
    // "unavailable, don't block submit" philosophy.
    function degradeLlmConfig() {
        llmConfigCache = null;
        llmReady = false;
        if (providerSelect) {
            clearSelect(providerSelect);
            addSelectOption(providerSelect, "", "Provider list unavailable — using backend default.");
            providerSelect.disabled = true;
        }
        if (modelSelect) {
            clearSelect(modelSelect);
            addSelectOption(modelSelect, "", "Model list unavailable — using backend default.");
            modelSelect.disabled = true;
        }
        if (llmBanner) {
            llmBanner.textContent = "";
            llmBanner.hidden = true;
        }
        if (!flowRunning) { button.disabled = false; }
    }

    // Mirrors loadDecks(): reuses the shared ensureBackend() handshake +
    // fetchOrNetworkError(), GETs the #105 config endpoint, and applies
    // the stored-beats-default decision once config + storage both
    // resolve (read concurrently on popup open).
    async function loadLlmConfig() {
        // Defensive (#109): absent dropdown DOM no-ops without throwing
        // and leaves Generate enabled.
        if (!providerSelect || !modelSelect) { return; }
        // Kicked off before the native-host handshake is awaited below, so
        // the stored-selection read runs concurrently with it (and with the
        // config fetch that follows) rather than being serialized after.
        var storedPromise = readStoredLlmSelection();
        try {
            var backend = await ensureBackend();
            var response = await fetchOrNetworkError(backend.baseUrl + "/api/extension/llm-config/", {
                headers: { Authorization: "Bearer " + backend.token },
            });
            if (!response.ok) { throw { kind: "http" }; }
            var data = await response.json();
            var stored = await storedPromise;
            if (!data || !Array.isArray(data.providers)) {
                degradeLlmConfig();
                return;
            }
            applyLlmConfig(data, stored);
        } catch (err) {
            // Network error, non-2xx, unusable data, or native-host
            // handshake failure: degrade, never block submit (fail-open).
            degradeLlmConfig();
        }
    }

    // Currently selected provider/model for the submit body (#106 field
    // names). Empty while degraded so the backend uses .env behavior.
    function chosenLlm() {
        if (!llmReady || !providerSelect || !modelSelect) { return {}; }
        if (providerSelect.disabled || !providerSelect.value) { return {}; }
        var out = { provider: providerSelect.value };
        if (!modelSelect.disabled && modelSelect.value) { out.model = modelSelect.value; }
        return out;
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
    async function extractTabContent(tabId) {
        await chrome.scripting.executeScript({
            target: { tabId: tabId },
            files: ["lib/Readability.js", "content_extract.js"],
        });
        var results = await chrome.scripting.executeScript({
            target: { tabId: tabId },
            func: function () { return extractPageContent(); },
        });
        var content = results && results[0] && results[0].result;
        if (!content || typeof content.text !== "string" || !content.text) {
            throw new Error("no extractable content");
        }
        return content;
    }

    // Reads {"error": "..."} out of a non-2xx response body, falling back
    // to the HTTP status if the body isn't JSON or has no `error` field.
    async function errorFromResponse(response) {
        try {
            var body = await response.json();
            return body && typeof body.error === "string" ? body.error : String(response.status);
        } catch (err) {
            return String(response.status);
        }
    }

    // A fetch() call that rejects (network refused, DNS failure, CORS
    // preflight rejected, etc.) is distinguished from a non-2xx HTTP
    // response: the former means "could not reach the backend at all",
    // the latter carries a real error body from the server.
    async function fetchOrNetworkError(url, options) {
        try {
            return await fetch(url, options);
        } catch (err) {
            throw { kind: "network" };
        }
    }

    async function pollStatus(submittedUrlId, token, baseUrl) {
        // NOTE (CORS gotcha): both endpoints below only answer this
        // extension's origin when the backend's EXTENSION_ID setting is
        // set to this extension's actual loaded ID. If it isn't, every
        // fetch here fails its CORS preflight with an opaque network
        // error indistinguishable from the backend being unreachable -
        // see README.md's "Browser extension setup" section.
        var response = await fetchOrNetworkError(
            baseUrl + "/api/extension/submit/" + submittedUrlId + "/status/",
            { headers: { Authorization: "Bearer " + token } }
        );
        if (!response.ok) {
            var errorMessage = await errorFromResponse(response);
            throw { kind: "http", message: errorMessage };
        }
        var data = await response.json();
        if (!data.terminal) {
            await sleep(POLL_INTERVAL_MS);
            return pollStatus(submittedUrlId, token, baseUrl);
        }
        if (data.review_url) {
            setBusy(false);
            setStatus("Done — review tab opened.");
            flowRunning = false;
            chrome.tabs.create({ url: data.review_url });
            return; // leave the button disabled - nothing left to retry
        }
        throw { kind: "http", message: data.generation_error || "card generation failed" };
    }

    async function submitContent(content, tabUrl, token, baseUrl) {
        // See the CORS note in pollStatus() above - it applies to this
        // fetch too.
        var payload = { url: tabUrl, title: content.title || "", text: content.text, images: content.images ?? [] };
        var deck = chosenDeckName();
        if (deck) { payload.deck_name = deck; }
        // Per-request LLM override (#106 field names, sent whenever the
        // dropdowns hold a live selection; omitted while degraded).
        var llm = chosenLlm();
        if (llm.provider) { payload.provider = llm.provider; }
        if (llm.model) { payload.model = llm.model; }
        var response = await fetchOrNetworkError(baseUrl + "/api/extension/submit/", {
            method: "POST",
            headers: {
                Authorization: "Bearer " + token,
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            var errorMessage = await errorFromResponse(response);
            throw { kind: "http", message: errorMessage };
        }
        var data = await response.json();
        return pollStatus(data.submitted_url_id, token, baseUrl);
    }

    async function runFlow() {
        button.disabled = true;
        flowRunning = true;
        setBusy(true);
        setStatus("Connecting to backend…");

        try {
            var backend;
            try {
                backend = await ensureBackend();
            } catch (err) {
                // ensureBackend() rejects for two different reasons: a real
                // connectNative failure (a plain Error, no .kind - e.g. the
                // native host manifest is missing/broken), or a native-error
                // it already tagged itself (the host replied {ok: false, ...},
                // e.g. a spawn/readiness timeout). Only the former is a true
                // "can't reach the native host" situation - the latter has its
                // own message and must not be collapsed into the generic text.
                if (err && err.kind === "native-error") { throw err; }
                throw { kind: "native-connect", message: err && err.message };
            }

            var token = backend.token;
            var baseUrl = backend.baseUrl;

            setStatus("Reading page…");
            var tabs = await chrome.tabs.query({ active: true, currentWindow: true });
            var tab = tabs[0];
            var content;
            try {
                content = await extractTabContent(tab.id);
            } catch (err) {
                throw { kind: "extract" };
            }
            setStatus("Generating cards…");
            await submitContent(content, tab.url, token, baseUrl);
        } catch (err) {
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
        }
    }

    button.addEventListener("click", function () {
        runFlow();
    });

    function loadPopup() {
        loadDecks();
        loadLlmConfig();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", loadPopup);
    } else {
        loadPopup();
    }
}());
