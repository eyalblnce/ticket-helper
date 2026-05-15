from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import SopProposal
from app.web_templates import templates

router = APIRouter(prefix="/training")


@router.get("", response_class=HTMLResponse)
async def training_index(
    request: Request,
    session: Session = Depends(get_session),
    run_id: str = Query(default=""),
    status: str = Query(default="pending"),
):
    query = select(SopProposal)
    if run_id:
        query = query.where(SopProposal.run_id == run_id)
    if status:
        query = query.where(SopProposal.status == status)
    query = query.order_by(
        SopProposal.validation_score.desc(),
        SopProposal.cluster_size.desc(),
    )
    proposals = list(session.exec(query).all())

    all_rows = session.exec(select(SopProposal.run_id, SopProposal.status)).all()
    all_runs = sorted({r for r, _ in all_rows}, reverse=True)

    counts: dict[str, int] = {s: 0 for s in ("pending", "approved", "rejected", "merged")}
    for r, s in all_rows:
        if (not run_id or r == run_id) and s in counts:
            counts[s] += 1

    return templates.TemplateResponse(
        request,
        "training.html",
        {
            "proposals": proposals,
            "all_runs": all_runs,
            "selected_run": run_id,
            "selected_status": status,
            "counts": counts,
        },
    )


@router.post("/{proposal_id}/approve", response_class=HTMLResponse)
async def approve(
    request: Request,
    proposal_id: int,
    session: Session = Depends(get_session),
):
    p = session.get(SopProposal, proposal_id)
    if p:
        p.status = "approved"
        p.reviewed_at = datetime.utcnow()
        session.add(p)
        session.commit()
        session.refresh(p)
    return templates.TemplateResponse(request, "partials/_proposal_row.html", {"p": p})


@router.post("/{proposal_id}/reject", response_class=HTMLResponse)
async def reject(
    request: Request,
    proposal_id: int,
    session: Session = Depends(get_session),
):
    p = session.get(SopProposal, proposal_id)
    if p:
        p.status = "rejected"
        p.reviewed_at = datetime.utcnow()
        session.add(p)
        session.commit()
        session.refresh(p)
    return templates.TemplateResponse(request, "partials/_proposal_row.html", {"p": p})


@router.post("/{proposal_id}/merge", response_class=HTMLResponse)
async def merge(
    request: Request,
    proposal_id: int,
    session: Session = Depends(get_session),
    merged_into: str = Form(...),
):
    p = session.get(SopProposal, proposal_id)
    if p:
        p.status = "merged"
        p.merged_into = merged_into
        p.reviewed_at = datetime.utcnow()
        session.add(p)
        session.commit()
        session.refresh(p)
    return templates.TemplateResponse(request, "partials/_proposal_row.html", {"p": p})
