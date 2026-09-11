# Flashcard Generator extension

This directory is an unpacked Chrome/Brave (MV3) extension. See the root
`README.md`'s "Browser extension setup (native messaging host)" section
for the full setup sequence.

## `manifest.json`'s `"key"` field

`manifest.json`'s `"key"` field currently contains the placeholder value
`"REPLACE_WITH_YOUR_OWN_OPENSSL_GENERATED_KEY"`. **This does not work as
committed.** Chrome/Brave will refuse to derive a stable extension ID from
it (or may refuse to load the extension at all). Before loading this
extension, follow the root `README.md`'s "Browser extension setup" section,
step 1: generate your own `openssl` signing keypair and replace this
placeholder with that keypair's base64 public key. Do this first - the
rest of that section's steps (loading the extension, running
`install_native_host`) depend on the extension having a stable ID, which
only a real pinned key provides.
