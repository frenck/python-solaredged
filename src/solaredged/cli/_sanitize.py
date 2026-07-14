"""Sanitise untrusted device strings for terminal rendering.

Device-provided strings (identity, string labels) are not trusted: a hostile or
garbled device could embed ANSI escape sequences or Rich markup that would
rewrite or spoof the operator's terminal. Route every such string through
:func:`safe` before handing it to Rich or Textual.
"""

from __future__ import annotations

from rich.markup import escape


def safe(value: object) -> str:
    """Drop non-printable characters and escape Rich markup from a device string."""
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return escape(text)
