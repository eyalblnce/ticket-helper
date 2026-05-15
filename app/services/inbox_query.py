"""Shared filtered ticket listing query for Inbox and Desk."""

from __future__ import annotations

from sqlmodel import or_, select

from app.models import Classification, Conversation, Ticket


def apply_ticket_search(stmt, q: str):
    """Apply search to a Ticket select.

    - Bare number → match freshdesk_id exactly (fast shortcut).
    - Otherwise split into words and require each to appear in at least one of:
      subject, requester_email, requester_name, or any conversation body.
    """
    stripped = q.strip()
    if stripped.isdigit():
        return stmt.where(Ticket.freshdesk_id == int(stripped))

    for word in stripped.split():
        like = f"%{word}%"
        conv_sub = select(Conversation.ticket_id).where(Conversation.body_text.ilike(like))
        stmt = stmt.where(
            or_(
                Ticket.subject.ilike(like),
                Ticket.requester_email.ilike(like),
                Ticket.requester_name.ilike(like),
                Ticket.id.in_(conv_sub),
            )
        )
    return stmt


def apply_inbox_filters(
    stmt,
    *,
    status: str,
    q: str,
    priority_int: int | None,
    category: str,
    sender_type: str,
    team: str,
):
    """Apply the same filters as the Inbox page to a Ticket select."""
    if status:
        stmt = stmt.where(Ticket.status == int(status))
    if q:
        stmt = apply_ticket_search(stmt, q)
    if priority_int:
        stmt = stmt.where(Ticket.priority == priority_int)
    if category:
        sub = select(Classification.ticket_id).where(Classification.category == category)
        stmt = stmt.where(Ticket.id.in_(sub))
    if sender_type:
        sub = select(Classification.ticket_id).where(Classification.sender_type == sender_type)
        stmt = stmt.where(Ticket.id.in_(sub))
    if team:
        sub = select(Classification.ticket_id).where(Classification.team == team)
        stmt = stmt.where(Ticket.id.in_(sub))
    return stmt
