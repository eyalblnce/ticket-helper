"""Batch classification task and shared classification helpers."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from sqlmodel import Session, select

from app.agents.classifier import BuyerContext, MerchantContext, classify
from app.config import settings
from app.db import engine
from app.models import Classification, Conversation, Ticket
from app.services.freshdesk import FreshdeskClient
from app.services.reference_lookup import (
    find_buyer_by_public_id,
    find_buyers_by_email,
    find_buyers_by_phone,
    find_merchant_by_domain,
    find_merchant_by_public_id,
)
from app.services.rules import classify_ticket

log = logging.getLogger(__name__)

CLASSIFY_DELAY = 0.3  # seconds between tickets to avoid hammering the API


# ---------------------------------------------------------------------------
# Shared utilities (also imported by routes/ticket.py)
# ---------------------------------------------------------------------------

def direction(c: dict) -> str:
    if c.get("private"):
        return "private_note"
    if c.get("incoming"):
        return "inbound"
    return "outbound"


def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


def parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Conversation fetching
# ---------------------------------------------------------------------------

async def ensure_conversations(ticket: Ticket, session: Session) -> list[Conversation]:
    """Return cached conversations or fetch from Freshdesk and store them."""
    conversations = session.exec(
        select(Conversation)
        .where(Conversation.ticket_id == ticket.id)
        .order_by(Conversation.freshdesk_created_at)
    ).all()

    if not conversations:
        client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
        try:
            raw = await client.get_conversations(ticket.freshdesk_id)
            for c in raw:
                session.add(Conversation(
                    freshdesk_id=c["id"],
                    ticket_id=ticket.id,
                    direction=direction(c),
                    body_text=strip_html(c.get("body_text") or c.get("body") or ""),
                    author_email=c.get("from_email") or c.get("user_id") or "",
                    freshdesk_created_at=parse_dt(c.get("created_at")),
                ))
            session.commit()
            conversations = session.exec(
                select(Conversation)
                .where(Conversation.ticket_id == ticket.id)
                .order_by(Conversation.freshdesk_created_at)
            ).all()
        finally:
            await client.close()

    return list(conversations)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def run_rule_classify(
    ticket: Ticket, conversations: list[Conversation], session: Session
) -> Classification:
    body = "\n\n".join(c.body_text for c in conversations if c.direction == "inbound")
    payload = ticket.raw_payload or {}
    result = classify_ticket(
        subject=ticket.subject,
        body=body,
        requester_email=ticket.requester_email,
        priority=ticket.priority,
        tags=payload.get("tags") or [],
        source=payload.get("source"),
    )

    entities: dict = result.get("entities") or {}
    domain_match = re.search(r"@([\w.-]+)$", ticket.requester_email.lower())
    domain = domain_match.group(1) if domain_match else ""
    db_merchant = find_merchant_by_domain(domain, session) if domain else None
    if db_merchant:
        entities["merchant_id"] = db_merchant.public_id
        entities["merchant_name"] = db_merchant.merchant_name
    else:
        db_buyers = find_buyers_by_email(ticket.requester_email, session)
        if not db_buyers:
            phone = (payload.get("requester") or {}).get("phone") or ""
            db_buyers = find_buyers_by_phone(phone, session) if phone else []
        if db_buyers:
            b = db_buyers[0]
            entities["buyer_id"] = b.public_id
            entities["merchant_name"] = b.merchant_name
    result["entities"] = entities

    cl = Classification(ticket_id=ticket.id, **result)
    session.add(cl)
    session.commit()
    session.refresh(cl)
    return cl


async def run_classify(
    ticket: Ticket, conversations: list[Conversation], session: Session
) -> Classification:
    conv_dicts = [{"direction": c.direction, "body_text": c.body_text} for c in conversations]

    domain_match = re.search(r"@([\w.-]+)$", ticket.requester_email.lower())
    domain = domain_match.group(1) if domain_match else ""
    db_merchant = find_merchant_by_domain(domain, session) if domain else None
    merchant_ctx: MerchantContext | None = (
        MerchantContext(
            name=db_merchant.merchant_name,
            public_id=db_merchant.public_id,
            domain=db_merchant.domain,
            status=db_merchant.status,
        )
        if db_merchant else None
    )

    db_buyers = find_buyers_by_email(ticket.requester_email, session)
    if not db_buyers:
        phone = (ticket.raw_payload.get("requester") or {}).get("phone") or ""
        db_buyers = find_buyers_by_phone(phone, session) if phone else []
    buyer_ctxs: list[BuyerContext] = [
        BuyerContext(
            name=b.buyer_name,
            public_id=b.public_id,
            merchant_name=b.merchant_name,
            terms_status=b.terms_status,
        )
        for b in db_buyers
    ]

    cf = (ticket.raw_payload or {}).get("custom_fields") or {}
    bv_id = cf.get("cf_buyervendor_id") or ""
    if bv_id.startswith("byr_") and not merchant_ctx:
        db_buyer = find_buyer_by_public_id(bv_id, session)
        if db_buyer and not buyer_ctxs:
            buyer_ctxs = [BuyerContext(
                name=db_buyer.buyer_name,
                public_id=db_buyer.public_id,
                merchant_name=db_buyer.merchant_name,
                terms_status=db_buyer.terms_status,
            )]
    elif bv_id.startswith("ven_") and not merchant_ctx:
        db_merchant = find_merchant_by_public_id(bv_id, session)
        if db_merchant:
            merchant_ctx = MerchantContext(
                name=db_merchant.merchant_name,
                public_id=db_merchant.public_id,
                domain=db_merchant.domain,
                status=db_merchant.status,
            )

    result = await classify(
        ticket.subject,
        conv_dicts,
        merchant=merchant_ctx,
        buyers=buyer_ctxs or None,
    )

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


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def classify_all_unclassified(force: bool = False) -> int:
    """Classify tickets that have no Classification record.

    Args:
        force: When True, re-classify tickets that already have a classification.
    """
    with Session(engine) as session:
        classified_ticket_ids = set(session.exec(select(Classification.ticket_id)).all())
        all_tickets = session.exec(select(Ticket)).all()
        if force:
            unclassified_fids = [t.freshdesk_id for t in all_tickets]
        else:
            unclassified_fids = [t.freshdesk_id for t in all_tickets if t.id not in classified_ticket_ids]

    log.info("classify_all: %d tickets to classify", len(unclassified_fids))
    count = 0

    for freshdesk_id in unclassified_fids:
        try:
            with Session(engine) as session:
                ticket = session.exec(
                    select(Ticket).where(Ticket.freshdesk_id == freshdesk_id)
                ).first()
                if not ticket:
                    continue
                already = session.exec(
                    select(Classification).where(Classification.ticket_id == ticket.id)
                ).first()
                if already and not force:
                    continue
                conversations = await ensure_conversations(ticket, session)
                if settings.anthropic_api_key:
                    await run_classify(ticket, conversations, session)
                else:
                    run_rule_classify(ticket, conversations, session)
            count += 1
            log.info("classify_all: classified ticket %d (%d done)", freshdesk_id, count)
        except Exception:
            log.exception("classify_all: error on ticket %d", freshdesk_id)

        await asyncio.sleep(CLASSIFY_DELAY)

    log.info("classify_all: done — %d classified", count)
    return count
