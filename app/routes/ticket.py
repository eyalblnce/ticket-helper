from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import Classification, Conversation, Ticket
from app.services.classify_task import ensure_conversations, run_classify, run_rule_classify
from app.services.reference_lookup import find_buyer_by_public_id, find_merchant_by_public_id

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
        classification = run_rule_classify(ticket, conversations, session)

    cf = (ticket.raw_payload or {}).get("custom_fields") or {}
    bv_id = cf.get("cf_buyervendor_id") or ""
    db_buyer = find_buyer_by_public_id(bv_id, session) if bv_id.startswith("byr_") else None
    db_merchant = find_merchant_by_public_id(bv_id, session) if bv_id.startswith("ven_") else None
    if db_buyer and not db_merchant:
        db_merchant = find_merchant_by_public_id(
            (classification.entities or {}).get("merchant_id", ""), session
        )

    return templates.TemplateResponse(
        request,
        "ticket.html",
        _ctx(ticket, conversations, classification, db_buyer, db_merchant),
    )


@router.post("/tickets/{freshdesk_id}/classify", response_class=HTMLResponse)
async def reclassify(
    freshdesk_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """HTMX endpoint — re-runs rules+DB classifier and returns the classification partial."""
    ticket, conversations = await _ensure_ticket(freshdesk_id, session)
    if settings.anthropic_api_key:
        classification = await run_classify(ticket, conversations, session)
    else:
        classification = run_rule_classify(ticket, conversations, session)

    return templates.TemplateResponse(
        request,
        "partials/_classification.html",
        {"classification": classification, "category_color": CATEGORY_COLOR, "ticket": ticket},
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

    conversations = await ensure_conversations(ticket, session)
    return ticket, conversations



def _ctx(
    ticket: Ticket,
    conversations: list[Conversation],
    classification: Classification | None,
    db_buyer=None,
    db_merchant=None,
) -> dict:
    cf = (ticket.raw_payload or {}).get("custom_fields") or {}
    # Requester email: stored field first, then first inbound conversation author
    email = ticket.requester_email or next(
        (c.author_email for c in conversations if c.direction == "inbound"), ""
    )
    return {
        "ticket": ticket,
        "conversations": conversations,
        "classification": classification,
        "status_label": FRESHDESK_STATUS.get(ticket.status, "?"),
        "priority_label": FRESHDESK_PRIORITY.get(ticket.priority, "?"),
        "priority_color": PRIORITY_COLOR.get(ticket.priority, "gray"),
        "category_color": CATEGORY_COLOR,
        "requester_email": email,
        "cf_entity_type": cf.get("cf_entity_type") or "",
        "cf_company_name": cf.get("cf_company_name") or "",
        "cf_buyervendor_id": cf.get("cf_buyervendor_id") or "",
        "db_buyer": db_buyer,
        "db_merchant": db_merchant,
    }


