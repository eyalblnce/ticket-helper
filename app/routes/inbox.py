from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import settings
from app.db import get_session
from app.models import Classification, Ticket

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

FRESHDESK_PRIORITY = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
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
ALL_CATEGORIES = list(CATEGORY_COLOR.keys())


FRESHDESK_STATUS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}


@router.get("/", response_class=HTMLResponse)
async def inbox(
    request: Request,
    session: Session = Depends(get_session),
    q: str = Query(default=""),
    priority: str = Query(default=""),
    category: str = Query(default=""),
    sender_type: str = Query(default=""),
    status: int = Query(default=2),
):
    stmt = select(Ticket).where(Ticket.status == status)

    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Ticket.subject.ilike(like)) | (Ticket.requester_email.ilike(like)) | (Ticket.requester_name.ilike(like))
        )
    priority_int = int(priority) if priority else None
    if priority_int:
        stmt = stmt.where(Ticket.priority == priority_int)

    stmt = stmt.order_by(Ticket.freshdesk_updated_at.desc())
    tickets = session.exec(stmt).all()

    # Latest classification per ticket
    all_cls = session.exec(select(Classification)).all()
    classifications: dict[int, Classification] = {}
    for cl in all_cls:
        if cl.ticket_id not in classifications or cl.created_at > classifications[cl.ticket_id].created_at:
            classifications[cl.ticket_id] = cl

    # Client-side filters that join against classifications
    if category:
        matching = {tid for tid, cl in classifications.items() if cl.category == category}
        tickets = [t for t in tickets if t.id in matching]
    if sender_type:
        matching = {tid for tid, cl in classifications.items() if cl.sender_type == sender_type}
        tickets = [t for t in tickets if t.id in matching]

    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "tickets": tickets,
            "classifications": classifications,
            "priority_labels": FRESHDESK_PRIORITY,
            "category_color": CATEGORY_COLOR,
            "all_categories": ALL_CATEGORIES,
            # active filters (to keep form state)
            "status_labels": FRESHDESK_STATUS,
            "filter_q": q,
            "filter_priority": priority_int,
            "filter_category": category,
            "filter_sender_type": sender_type,
            "freshdesk_base": f"https://{settings.freshdesk_domain}",
            "filter_status": status,
        },
    )
