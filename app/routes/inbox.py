import math

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, func, select

from app.config import settings
from app.db import get_session
from app.models import Classification, Ticket
from app.services.inbox_query import apply_inbox_filters
from app.web_templates import templates

router = APIRouter()

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
TEAM_COLOR = {
    "collections": "amber",
    "risk":         "red",
    "payment_ops":  "blue",
    "other":        "gray",
}
ALL_TEAMS = list(TEAM_COLOR.keys())
PER_PAGE = 50


@router.get("/", response_class=HTMLResponse)
async def inbox(
    request: Request,
    session: Session = Depends(get_session),
    q: str = Query(default=""),
    priority: str = Query(default=""),
    category: str = Query(default=""),
    sender_type: str = Query(default=""),
    team: str = Query(default=""),
    status: str = Query(default="2"),
    page: int = Query(default=1),
):
    priority_int = int(priority) if priority else None

    stmt = apply_inbox_filters(
        select(Ticket),
        status=status,
        q=q,
        priority_int=priority_int,
        category=category,
        sender_type=sender_type,
        team=team,
    )

    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))

    tickets = session.exec(
        stmt.order_by(Ticket.freshdesk_updated_at.desc())
            .offset((page - 1) * PER_PAGE)
            .limit(PER_PAGE)
    ).all()

    # Load classifications only for this page
    ticket_ids = [t.id for t in tickets]
    classifications: dict[int, Classification] = {}
    if ticket_ids:
        for cl in session.exec(select(Classification).where(Classification.ticket_id.in_(ticket_ids))).all():
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
            "all_categories": ALL_CATEGORIES,
            "team_color": TEAM_COLOR,
            "all_teams": ALL_TEAMS,
            "status_labels": FRESHDESK_STATUS,
            "filter_q": q,
            "filter_priority": priority_int,
            "filter_category": category,
            "filter_sender_type": sender_type,
            "filter_team": team,
            "freshdesk_base": f"https://{settings.freshdesk_domain}",
            "filter_status": status,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "per_page": PER_PAGE,
        },
    )
