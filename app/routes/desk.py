"""Agent desk — two-column ticket + found + treatment (SOP) view."""

from __future__ import annotations

import json
import math
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, func, select

from app.config import settings
from app.db import get_session
from app.models import Classification, Ticket
from app.routes.inbox import (
    ALL_CATEGORIES,
    ALL_TEAMS,
    CATEGORY_COLOR,
    FRESHDESK_PRIORITY,
    FRESHDESK_STATUS,
    TEAM_COLOR,
)
from app.services.classify_task import ensure_conversations
from app.services.desk_context import (
    desk_thread_section_title,
    effective_requester_email,
    humanize_entities,
    resolve_merchant_buyer_for_ticket,
)
from app.services.freshdesk_constants import ticket_source_label_from_raw_payload
from app.services.desk_sop import extract_steps_markdown, pick_selected_proposal, rank_sops_for_category
from app.services.inbox_query import apply_inbox_filters
from app.web_templates import templates

router = APIRouter()

DESK_PER_PAGE = 40

DESTINATION_LABELS = {
    "freshdesk_reply": "Freshdesk reply",
    "balance_outbox": "Balance outbox",
}


def _ticket_source_label(ticket: Ticket | None) -> str:
    if not ticket:
        return ""
    return ticket_source_label_from_raw_payload(ticket.raw_payload)


def _desk_qs(
    *,
    q: str,
    priority: str,
    category: str,
    sender_type: str,
    team: str,
    status: str,
    page: int,
    ticket_id: int | None = None,
) -> str:
    d: dict[str, str] = {}
    if q.strip():
        d["q"] = q.strip()
    if priority:
        d["priority"] = priority
    if category:
        d["category"] = category
    if sender_type:
        d["sender_type"] = sender_type
    if team:
        d["team"] = team
    if status:
        d["status"] = status
    if page and page != 1:
        d["page"] = str(page)
    if ticket_id is not None:
        d["ticket_id"] = str(ticket_id)
    return urlencode(d)


def _latest_classification(session: Session, ticket_db_id: int) -> Classification | None:
    return session.exec(
        select(Classification)
        .where(Classification.ticket_id == ticket_db_id)
        .order_by(Classification.created_at.desc())
    ).first()


@router.get("/desk", response_class=HTMLResponse)
async def desk_index(
    request: Request,
    session: Session = Depends(get_session),
    q: str = Query(default=""),
    priority: str = Query(default=""),
    category: str = Query(default=""),
    sender_type: str = Query(default=""),
    team: str = Query(default=""),
    status: str = Query(default="2"),
    page: int = Query(default=1),
    ticket_id: int | None = Query(default=None),
    sop_id: int | None = Query(default=None),
):
    """`ticket_id` is Freshdesk numeric id (same as /tickets/{freshdesk_id})."""
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
    total_pages = max(1, math.ceil(total / DESK_PER_PAGE))
    page = max(1, min(page, total_pages))

    tickets = session.exec(
        stmt.order_by(Ticket.freshdesk_updated_at.desc())
        .offset((page - 1) * DESK_PER_PAGE)
        .limit(DESK_PER_PAGE)
    ).all()

    ticket_ids = [t.id for t in tickets]
    classifications: dict[int, Classification] = {}
    if ticket_ids:
        for cl in session.exec(select(Classification).where(Classification.ticket_id.in_(ticket_ids))).all():
            if cl.ticket_id not in classifications or cl.created_at > classifications[cl.ticket_id].created_at:
                classifications[cl.ticket_id] = cl

    ticket_links = [
        {
            "ticket": t,
            "desk_url": "/desk?"
            + _desk_qs(
                q=q,
                priority=priority,
                category=category,
                sender_type=sender_type,
                team=team,
                status=status,
                page=page,
                ticket_id=t.freshdesk_id,
            ),
        }
        for t in tickets
    ]
    selected_ticket: Ticket | None = None
    conversations = []
    classification = None
    db_merchant = None
    db_buyer = None
    ranked_sops = []
    selected_sop = None
    steps_md = None
    entity_labeled: list[tuple[str, str]] = []
    entity_other: dict = {}
    requester_email = ""

    if ticket_id is not None:
        selected_ticket = session.exec(select(Ticket).where(Ticket.freshdesk_id == ticket_id)).first()
        if not selected_ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        conversations = await ensure_conversations(selected_ticket, session)
        classification = _latest_classification(session, selected_ticket.id)
        requester_email = effective_requester_email(selected_ticket, conversations)
        db_merchant, db_buyer = resolve_merchant_buyer_for_ticket(selected_ticket, conversations, session)
        if classification:
            entity_labeled, entity_other = humanize_entities(classification.entities)
            ranked_sops = rank_sops_for_category(session, classification.category)
            sel = pick_selected_proposal(ranked_sops, sop_id)
            selected_sop = sel
            if sel and sel.sop_markdown:
                steps_md = extract_steps_markdown(sel.sop_markdown)

    ctx = {
        "tickets": tickets,
        "ticket_links": ticket_links,
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
        "filter_status": status,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "per_page": DESK_PER_PAGE,
        "freshdesk_base": f"https://{settings.freshdesk_domain}",
        "selected_freshdesk_id": ticket_id,
        "selected_ticket": selected_ticket,
        "conversations": conversations,
        "classification": classification,
        "requester_email": requester_email,
        "db_buyer": db_buyer,
        "db_merchant": db_merchant,
        "ranked_sops": ranked_sops,
        "selected_sop": selected_sop,
        "steps_markdown": steps_md,
        "entity_labeled": entity_labeled,
        "entity_other_json": json.dumps(entity_other, indent=2) if entity_other else "",
        "destination_labels": DESTINATION_LABELS,
        "status_label": FRESHDESK_STATUS.get(selected_ticket.status, "?") if selected_ticket else "",
        "thread_section_title": (
            desk_thread_section_title(selected_ticket, conversations)
            if selected_ticket else ""
        ),
        "ticket_source_label": _ticket_source_label(selected_ticket),
        "desk_qs_base": _desk_qs(
            q=q,
            priority=priority,
            category=category,
            sender_type=sender_type,
            team=team,
            status=status,
            page=page,
        ),
    }
    return templates.TemplateResponse(request, "desk.html", ctx)


@router.get("/desk/fragments/detail/{freshdesk_id}", response_class=HTMLResponse)
async def desk_fragment_detail(
    freshdesk_id: int,
    request: Request,
    session: Session = Depends(get_session),
    sop_id: int | None = Query(default=None),
):
    """HTMX: out-of-band swap for thread + right column when selecting a ticket."""
    ticket = session.exec(select(Ticket).where(Ticket.freshdesk_id == freshdesk_id)).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    conversations = await ensure_conversations(ticket, session)
    classification = _latest_classification(session, ticket.id)
    requester_email = effective_requester_email(ticket, conversations)
    db_merchant, db_buyer = resolve_merchant_buyer_for_ticket(ticket, conversations, session)

    ranked_sops = []
    selected_sop = None
    steps_md = None
    entity_labeled: list[tuple[str, str]] = []
    entity_other: dict = {}
    if classification:
        entity_labeled, entity_other = humanize_entities(classification.entities)
        ranked_sops = rank_sops_for_category(session, classification.category)
        sel = pick_selected_proposal(ranked_sops, sop_id)
        selected_sop = sel
        if sel and sel.sop_markdown:
            steps_md = extract_steps_markdown(sel.sop_markdown)

    ctx = {
        "selected_ticket": ticket,
        "ticket": ticket,
        "conversations": conversations,
        "classification": classification,
        "requester_email": requester_email,
        "db_buyer": db_buyer,
        "db_merchant": db_merchant,
        "ranked_sops": ranked_sops,
        "selected_sop": selected_sop,
        "steps_markdown": steps_md,
        "entity_labeled": entity_labeled,
        "entity_other_json": json.dumps(entity_other, indent=2) if entity_other else "",
        "destination_labels": DESTINATION_LABELS,
        "status_label": FRESHDESK_STATUS.get(ticket.status, "?"),
        "thread_section_title": desk_thread_section_title(ticket, conversations),
        "ticket_source_label": _ticket_source_label(ticket),
        "category_color": CATEGORY_COLOR,
        "team_color": TEAM_COLOR,
    }
    return templates.TemplateResponse(request, "partials/_desk_detail_oob.html", ctx)


@router.get("/desk/fragments/treatment/{freshdesk_id}", response_class=HTMLResponse)
async def desk_fragment_treatment(
    freshdesk_id: int,
    request: Request,
    session: Session = Depends(get_session),
    sop_id: int | None = Query(default=None),
):
    """HTMX: OOB swap treatment panel only (SOP toggle)."""
    ticket = session.exec(select(Ticket).where(Ticket.freshdesk_id == freshdesk_id)).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    classification = _latest_classification(session, ticket.id)
    ranked_sops = []
    selected_sop = None
    steps_md = None
    if classification:
        ranked_sops = rank_sops_for_category(session, classification.category)
        sel = pick_selected_proposal(ranked_sops, sop_id)
        selected_sop = sel
        if sel and sel.sop_markdown:
            steps_md = extract_steps_markdown(sel.sop_markdown)

    ctx = {
        "selected_ticket": ticket,
        "ticket": ticket,
        "classification": classification,
        "ranked_sops": ranked_sops,
        "selected_sop": selected_sop,
        "steps_markdown": steps_md,
        "destination_labels": DESTINATION_LABELS,
        "category_color": CATEGORY_COLOR,
        "team_color": TEAM_COLOR,
    }
    return templates.TemplateResponse(request, "partials/_desk_treatment_oob.html", ctx)
