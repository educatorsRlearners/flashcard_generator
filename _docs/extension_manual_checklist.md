# Extension manual verification checklist

This is the consolidated, fresh-checkout, end-to-end manual checklist for
the browser extension + native messaging host + local backend flow (#41).
It supersedes the self-scoped spot-checks in the individual issues that
built each piece (#37, #38, #39, #40) — follow this document instead of
piecing those together.

**This checklist is not run by `uv run pytest` or any CI step.** Almost
everything below — native messaging registration/discovery by Chrome/
Brave, the cold-start spawn path, CORS preflight from a live
`chrome-extension://` origin, and content-extraction quality on real pages
— is outside what an automated suite can exercise. "Done" for the issue
that added this document means the document exists and is complete and
followable, not that someone has necessarily executed every item below
yet.

macOS only, matching the root `README.md`'s scope note (Linux/Windows are
tracked separately as #43).

Where a step is already fully documented elsewhere, this checklist links
to that document instead of restating it — you should not need any other
document open except where a link below sends you out.

---

## One-time setup

Do these once, in order, starting from a fresh checkout (nothing
installed, no extension loaded, backend never run).

### 1. Generate a signing keypair and pin the extension's key

Chrome/Brave derives an extension's ID from its manifest `"key"`. Pinning
one keeps the ID stable across reloads, which every later step depends on.
`extension/manifest.json` ships with a placeholder
(`"REPLACE_WITH_YOUR_OWN_OPENSSL_GENERATED_KEY"`) that does not work as
committed (see `extension/README.md`) — generate your own:

```
openssl genrsa -out extension-key.pem 2048
openssl rsa -in extension-key.pem -pubout -outform DER | openssl base64 -A
```

Copy the base64 output (one line, no `-----BEGIN/END-----` markers) into
`extension/manifest.json`'s `"key"` field, replacing the placeholder.

**Pass**: `extension/manifest.json`'s `"key"` field contains your own
base64 string, not the placeholder text.

Keep `extension-key.pem` somewhere outside the repo (or `.gitignore`d) —
it is a private key, not something to commit.

### 2. Load the extension unpacked

Open `chrome://extensions` (or `brave://extensions`) → enable **Developer
mode** → **Load unpacked** → select the `extension/` directory.

**Pass**: the extension appears in the list as "Flashcard Generator" with
no load errors, and the extensions page shows an **ID** field under it — a
32-character lowercase string. Note this ID; you need it to cross-check
against the derived ID in the next step (and in step 3a below).

### 3. Run the native host installer

```
uv run python manage.py install_native_host
```

No `--extension-id` needed — the installer derives the ID itself from
`extension/manifest.json`'s pinned key (step 1).

**Pass**: the command exits 0 and prints something like:

```
Extension ID derived from /path/to/repo/extension/manifest.json: <id>
Wrapper script written: /path/to/repo/native_host/run_host.sh
Chrome manifest written: /Users/you/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.flashcard_generator.native_host.json
Brave manifest written: /Users/you/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts/com.flashcard_generator.native_host.json
EXTENSION_ID set in .env: <id>
Restart any already-running backend process (manage.py dev, or runserver/run_huey started manually) for the new EXTENSION_ID to take effect - config/settings.py reads .env once at process start.
```

where the derived `<id>` matches the ID noted in step 2.

### 3a. One-time check: derived ID matches Chrome/Brave's assigned ID

This confirms the derivation algorithm (issue #53) against a real
browser — something the automated test suite cannot do itself, since it
never loads a real extension. Do this once; it doesn't need repeating on
every future reload as long as the signing key stays the same.

**Pass**: the `<id>` printed by step 3 (`Extension ID derived from
extension/manifest.json: <id>`) is character-for-character identical to
the **ID** field shown for the extension on `chrome://extensions` /
`brave://extensions` (step 2). If they differ, something is wrong with
the derivation or with which key is actually pinned — do not proceed
past this step until they match.

One "`<Browser> manifest written: ...`" line per browser it found
installed, plus one "`<Browser> not found (...) - skipped.`" line per
browser it didn't. If you only use one of Chrome/Brave, seeing exactly one
"written" line and one "not found" line is correct, not a failure — a
silent partial failure would instead look like the command exiting
non-zero with a `CommandError` (e.g. "Neither Chrome nor Brave appears to
be installed...").

### 4. Check that `EXTENSION_ID` was set for the backend

Step 3's installer already writes the backend's `EXTENSION_ID` into `.env`
for you — no hand-editing needed. This is required for CORS: the backend
only emits `Access-Control-Allow-Origin` for
`chrome-extension://<EXTENSION_ID>` when this setting matches the loaded
extension's actual ID, so verify it landed correctly before moving on.

**Check this first when something doesn't work.** A mismatched or unset
`EXTENSION_ID` makes every `fetch()` from the popup fail its CORS
preflight, which surfaces in the popup as a generic
"could not reach the backend" network error — indistinguishable from the
backend actually being down. `popup.js`'s own code comments call this out
as the single easiest thing to get wrong.

**Pass**: `.env` in the repo root contains an `EXTENSION_ID=<id>` line
matching the ID noted in step 2 (e.g. `grep EXTENSION_ID .env`), and if a
backend process was already running before step 3, it has been restarted
since — `.env` is only read once at process start, so a still-running
`manage.py dev`/`runserver`/`run_huey` keeps using its old value. Also
confirm `EXTENSION_ID` isn't separately set as a real shell environment
variable with a stale value — that would silently override `.env`.

### 5. Reload the extension once more

If Chrome/Brave was already open while you edited
`extension/manifest.json` (step 1) or ran the installer (step 3), reload
the extension once more from `chrome://extensions` / `brave://extensions`
(the reload icon on the extension's card).

**Pass**: reloading shows no new load errors, and the extension's ID on
the extensions page still matches the one used in steps 2-4 (it should not
change, since the key is now pinned).

---

## End-to-end flow checklist

Run each of these once one-time setup above is complete. Each item names
its own pass condition — "try X and see" is not sufficient, confirm the
specific text/behavior described.

### A. Cold start (backend not running)

1. Make sure the backend is **not** running (no `manage.py dev` /
   `manage.py runserver` process, no Huey consumer).
2. Open any regular webpage (not `chrome://`/`brave://`/an extension
   page), click the extension icon, then click
   **"Generate cards from this page."**

**Pass**: the status line reads "Connecting to backend…" (with the
spinner visible) and stays on that exact text for up to ~30 seconds while
`native_host/host.py` spawns `uv run python manage.py dev` in the
background — then the flow proceeds on its own (status moves to "Reading
page…" then "Generating cards…") with no further manual action. It does
not hang past ~30s and does not require you to click anything else.

### B. Click-to-review-tab on a real page

With the backend already running (from step A, or started manually),
open a real, live webpage with substantive text content (e.g. a
documentation page or article — not a blank/placeholder page) and click
**"Generate cards from this page."**

**Pass**: the status line progresses "Reading page…" → "Generating
cards…" → "Done — review tab opened." and a new browser tab opens showing
the review grid populated with cards whose front/back text is clearly
derived from that page's actual content (not an empty batch, not
placeholder/stub text).

### C. `chrome://` page injection failure

Open a `chrome://` (or `brave://`) internal page — e.g.
`chrome://extensions` itself, or `brave://settings` — and click
**"Generate cards from this page."**

**Pass**: the status line reads exactly:

```
Error: can't read this page (chrome:// and extension pages aren't supported — open a regular webpage and try again).
```

the button re-enables immediately (no ~30s wait, since this fails at
extraction, before any network call), and no other error text or a hang
occurs. Note: on Brave the same failure occurs for `brave://` pages, even
though the error text still says "chrome://" — that wording is accurate
enough (Brave is Chromium-based and shares the restriction) and is not a
bug.

### D. Misconfiguration error cases

Both of these are "something is misconfigured" scenarios; run both and
confirm they produce **distinct** error text so you can tell which one
you're looking at.

**D1. Invalid/expired token.** With the backend running and a normal
setup otherwise, delete or corrupt the token file at `.extension_token`
in the repo root (e.g. `rm .extension_token` or overwrite it with garbage
text), then click **"Generate cards from this page."** on a regular
webpage.

**Pass**: the status line shows an error rooted in a `401`/unauthorized
response from the backend (the exact text is whatever
`submissions/extension_api.py` returns as the `error` field of a 401
response, e.g. "Error: unauthorized" or similar — the key thing is it is
clearly auth-rooted, not a generic network error). Note: if you deleted
the file, the native host mints a fresh token itself on its next
`connectNative` call (see `native_host/host.py`'s `get_token`), so this
case is best exercised by **corrupting** the file's contents (invalid
token value) rather than deleting it, or by deleting it but only checking
the *first* click before a new one is minted.

**D2. Backend unreachable via the native host.** Simulate the native host
itself being broken — e.g. temporarily rename or break
`native_host/run_host.sh` (or edit the registered
`com.flashcard_generator.native_host.json` manifest to point at a bad
path), then click **"Generate cards from this page."**

**Pass**: the status line shows
`Error: could not reach native host — is the extension registered? See README.md.`
(a `native-connect` failure — the wrapper script itself couldn't run), or,
if the wrapper runs but the backend fails to spawn/become ready, the
native host's own `detail` text (e.g. "backend did not become ready
within 30s" or a `spawn_failed` message) appears in the status line
instead. Either way this is visibly different from D1's auth error and
from case A's normal cold-start text. Restore the wrapper script/manifest
afterward (re-run `install_native_host` if needed) before continuing.

### E. Review-tab accept/reject still works normally afterward

After a successful extension-triggered generation (case B above), in the
opened review tab: accept at least one card and reject at least one card.
Reload the review tab.

**Pass**: the accepted card still shows as accepted and the rejected card
still shows as rejected after the reload — persisted exactly as they are
for the pre-existing URL-submission review flow. This confirms the
extension path reuses the review UI without regressing it.

---

## See also

- Root `README.md`'s ["Browser extension setup (native messaging
  host)"](../README.md#browser-extension-setup-native-messaging-host)
  section — the canonical prose for the one-time setup steps summarized
  above.
- `extension/README.md` — the `manifest.json` `"key"` placeholder note.
