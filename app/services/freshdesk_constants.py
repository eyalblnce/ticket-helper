"""Freshdesk API numeric codes used for ticket provenance and UI labels."""

from __future__ import annotations

# Ticket `source` (channel) — see Freshdesk Tickets API.
FRESHDESK_SOURCE_EMAIL = 1
FRESHDESK_SOURCE_PORTAL = 2
FRESHDESK_SOURCE_PHONE = 3
FRESHDESK_SOURCE_CHAT = 7
FRESHDESK_SOURCE_FEEDBACK_WIDGET = 9
FRESHDESK_SOURCE_OUTBOUND_EMAIL = 10

FRESHDESK_SOURCE_LABEL: dict[int, str] = {
    FRESHDESK_SOURCE_EMAIL: "Email",
    FRESHDESK_SOURCE_PORTAL: "Portal",
    FRESHDESK_SOURCE_PHONE: "Phone",
    FRESHDESK_SOURCE_CHAT: "Chat",
    FRESHDESK_SOURCE_FEEDBACK_WIDGET: "Feedback widget",
    FRESHDESK_SOURCE_OUTBOUND_EMAIL: "Outbound email",
}


def freshdesk_source_label(source: int | None) -> str:
    if source is None:
        return "Unknown channel"
    return FRESHDESK_SOURCE_LABEL.get(source, f"Source {source}")


def ticket_source_label_from_raw_payload(raw_payload: dict | None) -> str:
    """Human label for ticket `source` (channel), for thread UI."""
    raw = (raw_payload or {}).get("source")
    try:
        return freshdesk_source_label(int(raw) if raw is not None else None)
    except (TypeError, ValueError):
        return freshdesk_source_label(None)
