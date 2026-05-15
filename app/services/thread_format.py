"""Plain-text thread bodies: normalize HTML-to-text spacing for UI display."""

from __future__ import annotations

import re


def format_thread_body(text: str) -> str:
    """Improve readability of stored `body_text` / `description_text` in the UI.

    - Normalizes newlines and non-breaking spaces.
    - Turns long runs of spaces (typical when HTML tags became spaces) into line breaks.
    - Collapses remaining double spaces and trims each line.
    - Limits stacked blank lines so signatures do not float mid-air.
    """
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    # Runs of 3+ spaces often separate list rows or paragraphs in HTML→text / pasted email
    t = re.sub(r" {3,}", "\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
