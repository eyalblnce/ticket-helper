"""Single Jinja2 environment with shared filters (used by all HTML routes)."""

from __future__ import annotations

from functools import lru_cache

from fastapi.templating import Jinja2Templates


@lru_cache(maxsize=1)
def get_templates() -> Jinja2Templates:
    from app.services.thread_format import format_thread_body

    t = Jinja2Templates(directory="app/templates")
    t.env.filters["format_thread_body"] = format_thread_body
    return t


templates = get_templates()
