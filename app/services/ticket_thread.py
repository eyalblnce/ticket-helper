"""Synthetic ticket description + thread helpers (Freshdesk omits description in conversations API)."""

from __future__ import annotations

import re
from datetime import datetime

from app.models import Conversation, Ticket
from app.services.freshdesk_constants import FRESHDESK_SOURCE_OUTBOUND_EMAIL

# Legacy + current directions for the row built from ticket.description_text
TICKET_DESCRIPTION_DIRECTIONS = frozenset(
    {
        "ticket_body",
        "ticket_description_inbound",
        "ticket_description_outbound",
    }
)


def normalize_body_for_match(text: str) -> str:
    """Collapse whitespace for comparing ticket description to first conversation."""
    return re.sub(r"\s+", " ", (text or "").strip())


def first_ticket_description(conversations: list[Conversation]) -> Conversation | None:
    """Return the synthesised ticket-description row, if present."""
    for c in conversations:
        if c.direction in TICKET_DESCRIPTION_DIRECTIONS:
            return c
    return None


def synthetic_description_provenance(
    ticket: Ticket,
    real_conversations: list[Conversation],
    description_text: str,
) -> tuple[str, str]:
    """Return (direction, author_email) for the synthesised description row.

    Rules (deterministic): A) Freshdesk outbound-email ticket → support description.
    B) Earliest public conversation body matches description → same side as that message.
    C) Else default inbound; author falls back to requester email.
    """
    payload = ticket.raw_payload or {}
    raw_source = payload.get("source")
    try:
        source_int = int(raw_source) if raw_source is not None else None
    except (TypeError, ValueError):
        source_int = None

    if source_int == FRESHDESK_SOURCE_OUTBOUND_EMAIL:
        return ("ticket_description_outbound", "")

    desc_norm = normalize_body_for_match(description_text)
    if not desc_norm:
        return ("ticket_description_inbound", ticket.requester_email or "")

    public_thread = [
        c
        for c in real_conversations
        if c.direction != "private_note"
    ]
    public_thread.sort(
        key=lambda c: (c.freshdesk_created_at or datetime.min, c.freshdesk_id),
    )

    for c in public_thread:
        conv_norm = normalize_body_for_match(c.body_text)
        if not conv_norm:
            continue
        if desc_norm == conv_norm or (
            len(desc_norm) >= 30 and conv_norm.startswith(desc_norm)
        ):
            if c.direction == "inbound":
                return (
                    "ticket_description_inbound",
                    c.author_email or ticket.requester_email or "",
                )
            if c.direction == "outbound":
                return ("ticket_description_outbound", c.author_email or "")

    return ("ticket_description_inbound", ticket.requester_email or "")


def ticket_description_body_parts(conversations: list[Conversation]) -> list[str]:
    """Body snippets for rules/LLM: inbound + opening description rows (chronological order)."""
    ordered = sorted(
        conversations,
        key=lambda c: (c.freshdesk_created_at or datetime.min, c.freshdesk_id),
    )
    parts: list[str] = []
    for c in ordered:
        if c.direction in (
            "inbound",
            "ticket_body",
            "ticket_description_inbound",
            "ticket_description_outbound",
        ):
            parts.append(c.body_text)
    return parts
