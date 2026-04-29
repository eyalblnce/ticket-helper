import math

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, or_, select

from app.config import settings
from app.db import get_session
from app.models import Classification, Conversation, Ticket

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

    # Build filtered query entirely in DB
    stmt = select(Ticket)
    if status:
        stmt = stmt.where(Ticket.status == int(status))
    if q:
        stmt = _apply_search(stmt, q)
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


def _apply_search(stmt, q: str):
    """Apply search to a Ticket query.

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
        stmt = stmt.where(or_(
            Ticket.subject.ilike(like),
            Ticket.requester_email.ilike(like),
            Ticket.requester_name.ilike(like),
            Ticket.id.in_(conv_sub),
        ))
    return stmt
