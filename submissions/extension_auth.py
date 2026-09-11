"""Shared-secret token lifecycle for browser-extension auth (issue #33).

A single local token authenticates the browser extension's requests to this
Django backend, independent of (and in addition to) any CSRF exemption a
future endpoint might need (#35). This module only mints/stores/reads/
compares that token; nothing here checks it on an incoming request.

* :func:`mint_token` generates a new token and writes it atomically to
  ``settings.EXTENSION_TOKEN_FILE`` with mode ``0600``. Refuses to clobber an
  existing token unless ``force=True``.
* :func:`read_token` returns the current token, or ``None`` if none has been
  minted yet. A tiny mtime-keyed cache avoids re-reading the file on every
  call while still picking up a rotation made by another process (e.g. a
  running dev server) without a restart.
* :func:`token_matches` does a constant-time comparison against the current
  token, and safely returns ``False`` (never raises) when no token exists.
"""

import hmac
import os
import secrets
import tempfile

from django.conf import settings

# Cache keyed by the token file path (as a string) so tests that override
# ``settings.EXTENSION_TOKEN_FILE`` to different tmp paths never see a stale
# value cached under a different path. Value is (mtime, token).
_cache: dict[str, tuple[float, str]] = {}


def mint_token(force: bool = False) -> str:
    """Generate a new token and write it to ``EXTENSION_TOKEN_FILE``.

    Raises :class:`FileExistsError` if a token file already exists and
    ``force`` is not ``True`` - never silently overwrites an existing token.
    """
    path = settings.EXTENSION_TOKEN_FILE
    if path.exists() and not force:
        raise FileExistsError(
            f"Extension token already exists at {path}; pass force=True "
            "(or use --rotate) to overwrite it."
        )

    token = secrets.token_urlsafe(32)

    # Atomic write: create a temp file in the same directory, then
    # os.replace() it into place, so a concurrent read_token() never
    # observes a partially-written token.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    _cache.pop(str(path), None)
    return token


def read_token() -> str | None:
    """Return the current token, or ``None`` if none has been minted yet."""
    path = settings.EXTENSION_TOKEN_FILE
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        _cache.pop(key, None)
        return None

    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    token = path.read_text().strip()
    _cache[key] = (mtime, token)
    return token


def token_matches(candidate: str) -> bool:
    """Constant-time check that ``candidate`` matches the current token.

    Returns ``False`` (never raises) when no token has been minted yet.
    """
    current = read_token()
    if current is None:
        return False
    return hmac.compare_digest(current, candidate)
