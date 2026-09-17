"""Shared boolean-setting parsing helpers (issue #149).

:func:`parse_bool_setting` implements the isinstance/None/string-normalization
logic shared by default-on master switches such as
:func:`submissions.feedback.fewshot_enabled`.

Dependency-free on purpose: imported by ``config/settings.py`` (which loads
before the Django app registry) as well as by the ``submissions`` call sites.
"""

from __future__ import annotations

#: String forms (case-insensitive, surrounding whitespace ignored) that disable
#: a default-on boolean setting — alongside ``False`` itself.
FALSY_SETTING_FORMS = ("0", "false", "no", "off", "")


def parse_bool_setting(raw, *, default: bool = True) -> bool:
    """Parse a default-on boolean Django setting value.

    * ``bool`` passes through unchanged.
    * ``None`` (and a missing setting, which callers translate to ``None``
      or pass as *default*) yields *default* (``True``: default-on preserved).
    * Anything else is stringified, stripped and lowercased: ``"0"`` /
      ``"false"`` / ``"no"`` / ``"off"`` / ``""`` disable, everything else
      (``"1"`` / ``"true"`` / ...) enables.
    """
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() not in FALSY_SETTING_FORMS
