from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.agents.classifier import classify
from app.config import settings
from app.services.rules import classify_ticket
from app.db import get_session
from app.models import Classification, Conversation, Ticket
from app.services.freshdesk import FreshdeskClient

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

FRESHDESK_STATUS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
FRESHDESK_PRIORITY = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
PRIORITY_COLOR = {1: "gray", 2: "yellow", 3: "orange", 4: "red"}
CATEGORY_COLOR = {
    "shipping_status":       "blue",
    "invoice_question":      "purple",
    "payment_status":        "indigo",
    "payment_failed":        "red",
    "credit_limit_question": "orange",
    "refund_request":        "pink",
    "return_request":        "pink",
    "damaged_or_wrong_item": "red",
    "product_question":      "teal",
    "account_access":        "yellow",
    "other":                 "gray",
}


@router.get("/tickets/{freshdesk_id}", response_class=HTMLResponse)
async def ticket_detail(
    freshdesk_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    ticket, conversations = await _ensure_ticket(freshdesk_id, session)

    classification = session.exec(
        select(Classification)
        .where(Classification.ticket_id == ticket.id)
        .order_by(Classification.created_at.desc())
    ).first()

    if not classification:
        classification = _run_rule_classify(ticket, conversations, session)
    if settings.anthropic_api_key and classification.model == "rules-v1":
        classification = await _run_classify(ticket, conversations, session)

    return templates.TemplateResponse(
        request,
        "ticket.html",
        _ctx(ticket, conversations, classification),
    )


@router.post("/tickets/{freshdesk_id}/classify", response_class=HTMLResponse)
async def reclassify(
    freshdesk_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """HTMX endpoint — re-runs classifier and returns the classification partial."""
    ticket, conversations = await _ensure_ticket(freshdesk_id, session)
    classification = await _run_classify(ticket, conversations, session)

    return templates.TemplateResponse(
        request,
        "partials/_classification.html",
        {"classification": classification, "category_color": CATEGORY_COLOR},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _ensure_ticket(
    freshdesk_id: int, session: Session
) -> tuple[Ticket, list[Conversation]]:
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

    if not conversations:
        client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
        try:
            raw = await client.get_conversations(freshdesk_id)
            for c in raw:
                session.add(Conversation(
                    freshdesk_id=c["id"],
                    ticket_id=ticket.id,
                    direction=_direction(c),
                    body_text=_strip_html(c.get("body_text") or c.get("body") or ""),
                    author_email=c.get("from_email") or c.get("user_id") or "",
                    freshdesk_created_at=_parse_dt(c.get("created_at")),
                ))
            session.commit()
            conversations = session.exec(
                select(Conversation)
                .where(Conversation.ticket_id == ticket.id)
                .order_by(Conversation.freshdesk_created_at)
            ).all()
        finally:
            await client.close()

    return ticket, list(conversations)


def _run_rule_classify(
    ticket: Ticket, conversations: list[Conversation], session: Session
) -> Classification:
    body = next((c.body_text for c in conversations if c.direction == "inbound"), "")
    payload = ticket.raw_payload or {}
    result = classify_ticket(
        subject=ticket.subject,
        body=body,
        requester_email=ticket.requester_email,
        priority=ticket.priority,
        tags=payload.get("tags") or [],
        source=payload.get("source"),
    )
    cl = Classification(ticket_id=ticket.id, **result)
    session.add(cl)
    session.commit()
    session.refresh(cl)
    return cl


async def _run_classify(
    ticket: Ticket, conversations: list[Conversation], session: Session
) -> Classification:
    first_inbound = next(
        (c.body_text for c in conversations if c.direction == "inbound"), ""
    )
    result = await classify(ticket.subject, first_inbound)

    cl = Classification(
        ticket_id=ticket.id,
        category=result.category,
        urgency=result.urgency,
        sentiment=result.sentiment,
        suggested_destination=result.suggested_destination,
        sender_type=result.sender_type,
        entities=result.entities.model_dump(exclude_none=True),
        model="claude-sonnet-4-6",
    )
    session.add(cl)
    session.commit()
    session.refresh(cl)
    return cl


def _ctx(
    ticket: Ticket,
    conversations: list[Conversation],
    classification: Classification | None,
) -> dict:
    return {
        "ticket": ticket,
        "conversations": conversations,
        "classification": classification,
        "status_label": FRESHDESK_STATUS.get(ticket.status, "?"),
        "priority_label": FRESHDESK_PRIORITY.get(ticket.priority, "?"),
        "priority_color": PRIORITY_COLOR.get(ticket.priority, "gray"),
        "category_color": CATEGORY_COLOR,
    }


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
