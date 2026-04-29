from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import join
from sqlmodel import Session, func, select

from app.db import get_session
from app.models import Classification, Ticket

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

DAYS = 30


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: Session = Depends(get_session),
    status: str = Query(default=""),          # "" = all, "2" = open, "closed" = 4+5
    sender_type: str = Query(default=""),     # "" | "merchant" | "buyer"
    q: str = Query(default=""),               # merchant / requester name search
):
    since = _days_ago(DAYS)

    # Build base ticket filter
    stmt = (
        select(
            func.strftime("%Y-%m-%d", Ticket.freshdesk_created_at).label("day"),
            func.count(Ticket.id).label("count"),
        )
        .where(Ticket.freshdesk_created_at >= since)
    )

    stmt = _apply_filters(stmt, status, sender_type, q)
    stmt = stmt.group_by("day").order_by("day")

    rows = session.exec(stmt).all()

    # Fill zeros for missing days
    counts_by_day = {row.day: row.count for row in rows}
    labels, values = [], []
    for i in range(DAYS, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        labels.append(d)
        values.append(counts_by_day.get(d, 0))

    total = sum(values)
    peak = max(values) if values else 0
    avg = round(total / DAYS, 1)

    # Hour-of-day distribution
    hour_stmt = (
        select(
            func.strftime("%H", Ticket.freshdesk_created_at).label("hour"),
            func.count(Ticket.id).label("count"),
        )
        .where(Ticket.freshdesk_created_at >= since)
    )
    hour_stmt = _apply_filters(hour_stmt, status, sender_type, q)
    hour_stmt = hour_stmt.group_by("hour").order_by("hour")
    hour_rows = session.exec(hour_stmt).all()

    counts_by_hour = {int(r.hour): r.count for r in hour_rows}
    hour_values = [counts_by_hour.get(h, 0) for h in range(24)]
    hour_labels = [f"{h:02d}:00" for h in range(24)]
    peak_hour = hour_values.index(max(hour_values)) if any(hour_values) else 0
    heuristic = _peak_window(hour_values)

    # Day-of-week distribution (SQLite %w: 0=Sunday … 6=Saturday)
    dow_stmt = (
        select(
            func.strftime("%w", Ticket.freshdesk_created_at).label("dow"),
            func.count(Ticket.id).label("count"),
        )
        .where(Ticket.freshdesk_created_at >= since)
    )
    dow_stmt = _apply_filters(dow_stmt, status, sender_type, q)
    dow_stmt = dow_stmt.group_by("dow").order_by("dow")
    dow_rows = session.exec(dow_stmt).all()

    counts_by_dow = {int(r.dow): r.count for r in dow_rows}
    dow_order = [1, 2, 3, 4, 5, 6, 0]          # Mon–Sun
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_values = [counts_by_dow.get(d, 0) for d in dow_order]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "labels": labels,
            "values": values,
            "total": total,
            "peak": peak,
            "avg": avg,
            "days": DAYS,
            "hour_labels": hour_labels,
            "hour_values": hour_values,
            "peak_hour": peak_hour,
            "heuristic": heuristic,
            "dow_labels": dow_names,
            "dow_values": dow_values,
            "filter_status": status,
            "filter_sender_type": sender_type,
            "filter_q": q,
        },
    )


def _apply_filters(stmt, status: str, sender_type: str, q: str):
    if status == "open":
        stmt = stmt.where(Ticket.status == 2)
    elif status == "closed":
        stmt = stmt.where(Ticket.status.in_([4, 5]))

    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Ticket.requester_name.ilike(like)) | (Ticket.requester_email.ilike(like))
        )

    if sender_type:
        # Subquery: ticket IDs that have a matching classification
        sub = select(Classification.ticket_id).where(
            Classification.sender_type == sender_type
        )
        stmt = stmt.where(Ticket.id.in_(sub))

    return stmt


def _peak_window(hour_values: list[int]) -> str:
    """Find the 3-hour rolling window with the most tickets and describe it."""
    if not any(hour_values):
        return "No data"
    best_start, best_count = 0, 0
    for h in range(24):
        window = sum(hour_values[h % 24] for h in range(h, h + 3))
        if window > best_count:
            best_count, best_start = window, h
    def fmt(h: int) -> str:
        suffix = "am" if h < 12 else "pm"
        return f"{h % 12 or 12}{suffix}"
    return f"{fmt(best_start)}–{fmt((best_start + 3) % 24)} ({best_count} tickets in peak window)"


def _days_ago(n: int) -> datetime:
    return datetime.combine(date.today() - timedelta(days=n), datetime.min.time())
