from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db import get_session
from app.models import Conversation, Ticket
from app.services.freshdesk import FreshdeskClient
from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

FRESHDESK_STATUS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
FRESHDESK_PRIORITY = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
PRIORITY_COLOR = {1: "gray", 2: "yellow", 3: "orange", 4: "red"}


@router.get("/tickets/{freshdesk_id}", response_class=HTMLResponse)
async def ticket_detail(
    freshdesk_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    ticket = session.exec(
        select(Ticket).where(Ticket.freshdesk_id == freshdesk_id)
    ).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    conversations = session.exec(
        select(Conversation)
        .where(Conversation.ticket_id == ticket.id)
        .order_by(Conversation.freshdesk_created_at)
    ).all()

    # If no conversations stored yet, fetch live from Freshdesk and save them
    if not conversations:
        client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
        try:
            raw = await client.get_conversations(freshdesk_id)
            for c in raw:
                conv = Conversation(
                    freshdesk_id=c["id"],
                    ticket_id=ticket.id,
                    direction=_direction(c),
                    body_text=_strip_html(c.get("body_text") or c.get("body") or ""),
                    author_email=c.get("from_email") or c.get("user_id") or "",
                    freshdesk_created_at=_parse_dt(c.get("created_at")),
                )
                session.add(conv)
            session.commit()
            conversations = session.exec(
                select(Conversation)
                .where(Conversation.ticket_id == ticket.id)
                .order_by(Conversation.freshdesk_created_at)
            ).all()
        finally:
            await client.close()

    return templates.TemplateResponse(
        request,
        "ticket.html",
        {
            "ticket": ticket,
            "conversations": conversations,
            "status_label": FRESHDESK_STATUS.get(ticket.status, "?"),
            "priority_label": FRESHDESK_PRIORITY.get(ticket.priority, "?"),
            "priority_color": PRIORITY_COLOR.get(ticket.priority, "gray"),
        },
    )


def _direction(c: dict) -> str:
    if c.get("private"):
        return "private_note"
    if c.get("incoming"):
        return "inbound"
    return "outbound"


def _strip_html(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", html).strip()


def _parse_dt(val: str | None):
    from datetime import datetime
    if not val:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None
