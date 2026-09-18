# Flashcard Generator extension

This directory is an unpacked Chrome/Brave (MV3) extension. For setup, follow the root
`README.md`'s [First time: one-time install (do once)](../README.md#first-time-one-time-install-do-once)
section (and [Extension internals](../README.md#extension-internals) for reference
detail).

## `manifest.json`'s `"key"` field

`manifest.json`'s `"key"` field currently contains the placeholder value
`"REPLACE_WITH_YOUR_OWN_OPENSSL_GENERATED_KEY"`. **This does not work as
committed** — Chrome/Brave will refuse to derive a stable extension ID from
it (or may refuse to load the extension at all). See the root `README.md`'s
[First time: one-time install (do once)](../README.md#first-time-one-time-install-do-once)
for the setup step that replaces it, then load the extension unpacked
(`brave://extensions` → enable Developer mode → "Load unpacked" → select
the `extension/` directory).

## Distribution: dev-only, localhost-only

This extension targets a **locally-run Django backend only**. There is no
Chrome Web Store listing and no hosted/production backend — none exists
today, and none is planned. (Speculative, unprioritized backlog for a real
packaging/hosted-backend pipeline, if this is ever revisited, is tracked in
issue #173.)

- **Backend origin.** The extension talks to the backend at
  `http://127.0.0.1:8000/*`, matching `manifest.json`'s `host_permissions`.
  It never calls the backend directly by a hardcoded URL: `popup.js`
  receives `base_url` at runtime from the native messaging host's reply
  (`reply.base_url`, set by `native_host/host.py`'s `reply_spawned`/
  `reply_already_running`). `content_extract.js` makes no backend calls at
  all — it only extracts page content locally via Readability.
  `host_permissions` therefore needs no change: it is already scoped
  correctly to the local dev origin.
- **`BACKEND_URL` is a local port override, not a remote-host switch.**
  `native_host/host.py` reads `BACKEND_URL` (default
  `http://127.0.0.1:8000/`) to decide where to spawn/find the backend and
  what `base_url` to hand back to the popup. It exists so a developer can
  move the dev server to a different local port, not to point the
  extension at a remote or hosted backend — the native host spawns
  `uv run python manage.py dev` on the *same machine* it's running on, so
  it only ever makes sense when the backend and the browser share a
  machine.
- **Why dev-only, not Chrome Web Store or managed/enterprise install.**
  `submissions/management/commands/install_native_host.py` bakes an
  absolute local Python interpreter path and this specific extension's ID
  into a per-machine native-messaging-host manifest, and writes that
  extension ID into `.env` as `EXTENSION_ID` for CORS. This setup only
  works for the single local install the command was run for — a
  Chrome-Web-Store-assigned extension ID, or any machine the installer
  hasn't been run on, would break both native host registration and the
  CORS allowlist without redoing this flow by hand. There is also
  currently no deployment/CI story for the Django backend (no Dockerfile,
  Procfile, `fly.toml`, or deploy workflow anywhere in the repo as of this
  writing), so there is no production origin to distribute a packaged
  extension against even if we wanted to.

Net effect: nothing in `extension/manifest.json`, `popup.js`, or
`content_extract.js` needs to change for this decision — the code already
matches it.
