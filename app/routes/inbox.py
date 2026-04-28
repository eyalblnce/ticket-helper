from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db import get_session
from app.models import Ticket

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

FRESHDESK_STATUS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
FRESHDESK_PRIORITY = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}


@router.get("/", response_class=HTMLResponse)
async def inbox(request: Request, session: Session = Depends(get_session)):
    tickets = session.exec(
        select(Ticket)
        .where(Ticket.status == 2)
        .order_by(Ticket.freshdesk_updated_at.desc())
    ).all()

    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "tickets": tickets,
            "status_labels": FRESHDESK_STATUS,
            "priority_labels": FRESHDESK_PRIORITY,
        },
    )
