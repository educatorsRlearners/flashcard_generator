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
