from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db import get_session
from app.models import Classification, Ticket

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

FRESHDESK_STATUS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
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


@router.get("/", response_class=HTMLResponse)
async def inbox(request: Request, session: Session = Depends(get_session)):
    tickets = session.exec(
        select(Ticket)
        .where(Ticket.status == 2)
        .order_by(Ticket.freshdesk_updated_at.desc())
    ).all()

    # Latest classification per ticket, keyed by ticket.id
    classifications: dict[int, Classification] = {}
    if tickets:
        ticket_ids = [t.id for t in tickets]
        all_cls = session.exec(select(Classification)).all()
        # Keep only the most recent per ticket
        for cl in all_cls:
            if cl.ticket_id not in classifications or cl.created_at > classifications[cl.ticket_id].created_at:
                classifications[cl.ticket_id] = cl

    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "tickets": tickets,
            "classifications": classifications,
            "priority_labels": FRESHDESK_PRIORITY,
            "category_color": CATEGORY_COLOR,
        },
    )
