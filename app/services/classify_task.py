"""Batch classification task and shared classification helpers."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app.agents.classifier import BuyerContext, MerchantContext, classify
from app.config import settings
from app.db import engine
from app.models import Buyer, Classification, Conversation, Merchant, Ticket
from app.services.freshdesk import FreshdeskClient
from app.services.reference_lookup import (
    find_buyer_by_public_id,
    find_buyers_by_email,
    find_buyers_by_phone,
    find_merchant_by_domain,
    find_merchant_by_public_id,
)
from app.services.rules import assign_team, classify_ticket

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
        log.debug("ticket %d: fetching conversations from Freshdesk", ticket.freshdesk_id)
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
            log.debug("ticket %d: stored %d conversation(s)", ticket.freshdesk_id, len(conversations))
        finally:
            await client.close()
    else:
        log.debug("ticket %d: %d conversation(s) from cache", ticket.freshdesk_id, len(conversations))

    conversations = list(conversations)

    # Freshdesk conversations API omits the original ticket description (only replies/notes).
    # Synthesise a ticket_body entry from the description stored during sync.
    if not any(c.direction == "ticket_body" for c in conversations):
        desc = strip_html((ticket.raw_payload or {}).get("description_text") or "")
        if desc:
            body_conv = Conversation(
                freshdesk_id=-ticket.freshdesk_id,
                ticket_id=ticket.id,
                direction="ticket_body",
                body_text=desc,
                author_email=ticket.requester_email,
                freshdesk_created_at=ticket.freshdesk_created_at,
            )
            session.add(body_conv)
            session.commit()
            session.refresh(body_conv)
            conversations.insert(0, body_conv)
            log.debug("ticket %d: synthesised ticket_body conversation", ticket.freshdesk_id)

    return conversations


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def run_rule_classify(
    ticket: Ticket, conversations: list[Conversation], session: Session
) -> Classification:
    body = "\n\n".join(c.body_text for c in conversations if c.direction in ("inbound", "ticket_body"))
    payload = ticket.raw_payload or {}
    eff_email = _effective_email(ticket, conversations)
    result = classify_ticket(
        subject=ticket.subject,
        body=body,
        requester_email=eff_email,
        priority=ticket.priority,
        tags=payload.get("tags") or [],
        source=payload.get("source"),
    )

    entities: dict = result.get("entities") or {}
    domain_match = re.search(r"@([\w.-]+)$", eff_email.lower())
    domain = domain_match.group(1) if domain_match else ""
    db_merchant = find_merchant_by_domain(domain, session) if domain else None
    is_suspended = False
    if db_merchant:
        entities["merchant_id"] = db_merchant.public_id
        entities["merchant_name"] = db_merchant.merchant_name
        log.debug("ticket %d: matched merchant %s (%s)", ticket.freshdesk_id, db_merchant.merchant_name, db_merchant.public_id)
    else:
        db_buyers = find_buyers_by_email(eff_email, session)
        if not db_buyers:
            phone = (payload.get("requester") or {}).get("phone") or ""
            db_buyers = find_buyers_by_phone(phone, session) if phone else []
        if not db_buyers:
            bv_id = (payload.get("custom_fields") or {}).get("cf_buyervendor_id") or ""
            if bv_id.startswith("byr_"):
                db_buyer = find_buyer_by_public_id(bv_id, session)
                if db_buyer:
                    db_buyers = [db_buyer]
        if db_buyers:
            b = db_buyers[0]
            entities["buyer_id"] = b.public_id
            entities["merchant_name"] = b.merchant_name
            is_suspended = b.is_suspended
            log.debug("ticket %d: matched buyer %s (%s) suspended=%s", ticket.freshdesk_id, b.buyer_name, b.public_id, is_suspended)
        else:
            log.debug("ticket %d: no merchant/buyer match", ticket.freshdesk_id)
    result["entities"] = entities

    cl = Classification(ticket_id=ticket.id, **result, team=assign_team(result["category"], is_suspended))
    session.add(cl)
    session.commit()
    session.refresh(cl)
    log.info(
        "ticket %d: rules → category=%s urgency=%d sentiment=%s sender=%s team=%s",
        ticket.freshdesk_id, cl.category, cl.urgency, cl.sentiment, cl.sender_type, cl.team,
    )
    return cl


async def run_classify(
    ticket: Ticket, conversations: list[Conversation], session: Session
) -> Classification:
    conv_dicts = [{"direction": c.direction, "body_text": c.body_text} for c in conversations]

    email = _effective_email(ticket, conversations)
    domain_match = re.search(r"@([\w.-]+)$", email.lower())
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

    raw_db_buyers = find_buyers_by_email(email, session)
    if not raw_db_buyers:
        phone = (ticket.raw_payload.get("requester") or {}).get("phone") or ""
        raw_db_buyers = find_buyers_by_phone(phone, session) if phone else []
    buyer_ctxs: list[BuyerContext] = [
        BuyerContext(
            name=b.buyer_name,
            public_id=b.public_id,
            merchant_name=b.merchant_name,
            terms_status=b.terms_status,
        )
        for b in raw_db_buyers
    ]
    is_suspended = any(b.is_suspended for b in raw_db_buyers)

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
            is_suspended = db_buyer.is_suspended
    elif bv_id.startswith("ven_") and not merchant_ctx:
        db_merchant = find_merchant_by_public_id(bv_id, session)
        if db_merchant:
            merchant_ctx = MerchantContext(
                name=db_merchant.merchant_name,
                public_id=db_merchant.public_id,
                domain=db_merchant.domain,
                status=db_merchant.status,
            )

    log.debug(
        "ticket %d: calling LLM (%d message(s), merchant=%s, buyers=%d)",
        ticket.freshdesk_id, len(conv_dicts),
        merchant_ctx.get("name") if merchant_ctx else None,
        len(buyer_ctxs),
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
        team=assign_team(result.category, is_suspended),
    )
    session.add(cl)
    session.commit()
    session.refresh(cl)
    log.info(
        "ticket %d: llm → category=%s urgency=%d sentiment=%s sender=%s team=%s",
        ticket.freshdesk_id, cl.category, cl.urgency, cl.sentiment, cl.sender_type, cl.team,
    )
    return cl


# ---------------------------------------------------------------------------
# Conversation loader (from downloaded JSONL)
# ---------------------------------------------------------------------------

CONVERSATIONS_JSONL = Path("data/conversations.jsonl")
LOAD_BATCH_SIZE = 500
LOAD_LOG_EVERY = 2000  # lines


def load_conversations_from_jsonl(path: Path = CONVERSATIONS_JSONL) -> dict:
    """Load conversations.jsonl into the Conversation table.

    Skips conversations already present (by freshdesk_id).
    Returns a dict with loaded/skipped/missing_ticket counts.
    """
    if not path.exists():
        log.warning("conversations file not found: %s", path)
        return {"loaded": 0, "skipped": 0, "missing_ticket": 0}

    with Session(engine) as session:
        ticket_map: dict[int, int] = {
            t.freshdesk_id: t.id
            for t in session.exec(select(Ticket)).all()
        }
        existing_ids: set[int] = set(
            session.exec(select(Conversation.freshdesk_id)).all()
        )

    log.info("load_conversations: %d tickets in DB, %d conversations already loaded", len(ticket_map), len(existing_ids))

    total_lines = sum(1 for _ in path.open())
    loaded = skipped = missing_ticket = 0
    batch: list[Conversation] = []

    def _flush() -> None:
        nonlocal loaded
        if not batch:
            return
        with Session(engine) as session:
            session.add_all(batch)
            session.commit()
        loaded += len(batch)
        batch.clear()

    with path.open() as f:
        for i, line in enumerate(f, 1):
            entry = json.loads(line)
            ticket_db_id = ticket_map.get(entry["ticket_id"])

            if ticket_db_id is None:
                missing_ticket += len(entry["conversations"])
                continue

            for c in entry["conversations"]:
                conv_fid = c["id"]
                if conv_fid in existing_ids:
                    skipped += 1
                    continue
                existing_ids.add(conv_fid)
                batch.append(Conversation(
                    freshdesk_id=conv_fid,
                    ticket_id=ticket_db_id,
                    direction=direction(c),
                    body_text=strip_html(c.get("body_text") or c.get("body") or ""),
                    author_email=c.get("from_email") or str(c.get("user_id") or ""),
                    freshdesk_created_at=parse_dt(c.get("created_at")),
                ))
                if len(batch) >= LOAD_BATCH_SIZE:
                    _flush()

            if i % LOAD_LOG_EVERY == 0 or i == total_lines:
                log.info(
                    "load_conversations: [%d/%d lines] loaded=%d skipped=%d missing_ticket=%d",
                    i, total_lines, loaded + len(batch), skipped, missing_ticket,
                )

    _flush()
    log.info(
        "load_conversations: done — loaded=%d skipped=%d missing_ticket=%d",
        loaded, skipped, missing_ticket,
    )
    return {"loaded": loaded, "skipped": skipped, "missing_ticket": missing_ticket}


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

INSERT_BATCH_SIZE = 500


async def classify_all_unclassified(force: bool = False) -> int:
    """Classify tickets that have no Classification record.

    Bulk-loads all tickets, conversations, merchants, and buyers into memory
    before processing to avoid N+1 queries.

    Args:
        force: When True, re-classify tickets that already have a classification.
    """
    with Session(engine) as session:
        classified_ids: set[int] = (
            set() if force
            else set(session.exec(select(Classification.ticket_id)).all())
        )
        tickets = session.exec(select(Ticket)).all()
        to_classify = [t for t in tickets if t.id not in classified_ids]

        if not to_classify:
            log.info("classify_all: nothing to classify")
            return 0

        ticket_ids = {t.id for t in to_classify}
        all_convs = session.exec(
            select(Conversation).where(Conversation.ticket_id.in_(ticket_ids))
        ).all()
        convs_by_ticket: dict[int, list[Conversation]] = {}
        for c in all_convs:
            convs_by_ticket.setdefault(c.ticket_id, []).append(c)

        merchant_by_domain: dict[str, Merchant] = {
            m.domain: m for m in session.exec(select(Merchant)).all() if m.domain
        }
        buyers_by_email: dict[str, list[Buyer]] = {}
        buyers_by_phone: dict[str, list[Buyer]] = {}
        buyers_by_public_id: dict[str, Buyer] = {}
        for b in session.exec(select(Buyer)).all():
            for addr in filter(None, [b.email, b.qualification_email]):
                buyers_by_email.setdefault(addr.lower(), []).append(b)
            if b.phone:
                buyers_by_phone.setdefault(b.phone, []).append(b)
            if b.public_id:
                buyers_by_public_id[b.public_id] = b

    total = len(to_classify)
    log.info(
        "classify_all: %d ticket(s) to classify, %d with conversations (force=%s)",
        total, sum(1 for t in to_classify if t.id in convs_by_ticket), force,
    )

    count = errors = skipped = 0
    pending: list[Classification] = []

    def _flush() -> None:
        if not pending:
            return
        with Session(engine) as session:
            session.add_all(pending)
            session.commit()
        pending.clear()

    for i, ticket in enumerate(to_classify, 1):
        convs = sorted(
            convs_by_ticket.get(ticket.id, []),
            key=lambda c: c.freshdesk_created_at or datetime.min,
        )
        if not convs:
            skipped += 1
            log.debug("classify_all: ticket %d has no conversations, skipping", ticket.freshdesk_id)
            continue

        try:
            if settings.anthropic_api_key:
                cl = await _classify_llm(ticket, convs, merchant_by_domain, buyers_by_email, buyers_by_phone, buyers_by_public_id)
                await asyncio.sleep(CLASSIFY_DELAY)
            else:
                cl = _classify_rules(ticket, convs, merchant_by_domain, buyers_by_email, buyers_by_phone, buyers_by_public_id)

            pending.append(cl)
            count += 1
            log.info("classify_all: [%d/%d] ticket %d → %s", i, total, ticket.freshdesk_id, cl.category)

            if len(pending) >= INSERT_BATCH_SIZE:
                _flush()

        except Exception:
            errors += 1
            log.exception("classify_all: ticket %d failed", ticket.freshdesk_id)

    _flush()
    log.info("classify_all: done — %d classified, %d no conversations, %d error(s)", count, skipped, errors)
    return count


def _effective_email(ticket: Ticket, convs: list[Conversation]) -> str:
    """Requester email, falling back to the first inbound conversation author."""
    return ticket.requester_email or next(
        (c.author_email for c in convs if c.direction == "inbound"), ""
    )


def _classify_rules(
    ticket: Ticket,
    convs: list[Conversation],
    merchant_by_domain: dict[str, Merchant],
    buyers_by_email: dict[str, list[Buyer]],
    buyers_by_phone: dict[str, list[Buyer]],
    buyers_by_public_id: dict[str, Buyer] | None = None,
) -> Classification:
    body = "\n\n".join(c.body_text for c in convs if c.direction == "inbound")
    payload = ticket.raw_payload or {}
    email = _effective_email(ticket, convs)
    result = classify_ticket(
        subject=ticket.subject,
        body=body,
        requester_email=email,
        priority=ticket.priority,
        tags=payload.get("tags") or [],
        source=payload.get("source"),
    )

    entities: dict = result.get("entities") or {}
    domain_match = re.search(r"@([\w.-]+)$", email.lower())
    domain = domain_match.group(1) if domain_match else ""
    db_merchant = merchant_by_domain.get(domain)
    is_suspended = False
    if db_merchant:
        entities["merchant_id"] = db_merchant.public_id
        entities["merchant_name"] = db_merchant.merchant_name
        log.debug("ticket %d: matched merchant %s", ticket.freshdesk_id, db_merchant.merchant_name)
    else:
        db_buyers = buyers_by_email.get(email.lower(), [])
        if not db_buyers:
            phone = re.sub(r"\D", "", (payload.get("requester") or {}).get("phone") or "")
            db_buyers = buyers_by_phone.get(phone, []) if phone else []
        if not db_buyers and buyers_by_public_id:
            bv_id = (payload.get("custom_fields") or {}).get("cf_buyervendor_id") or ""
            if bv_id.startswith("byr_") and bv_id in buyers_by_public_id:
                db_buyers = [buyers_by_public_id[bv_id]]
        if db_buyers:
            b = db_buyers[0]
            entities["buyer_id"] = b.public_id
            entities["merchant_name"] = b.merchant_name
            is_suspended = b.is_suspended
            log.debug("ticket %d: matched buyer %s suspended=%s", ticket.freshdesk_id, b.buyer_name, is_suspended)
        else:
            log.debug("ticket %d: no merchant/buyer match", ticket.freshdesk_id)
    result["entities"] = entities

    log.info(
        "ticket %d: rules → category=%s urgency=%d sentiment=%s sender=%s team=%s",
        ticket.freshdesk_id, result["category"], result["urgency"], result["sentiment"], result["sender_type"],
        assign_team(result["category"], is_suspended),
    )
    return Classification(ticket_id=ticket.id, **result, team=assign_team(result["category"], is_suspended))


async def _classify_llm(
    ticket: Ticket,
    convs: list[Conversation],
    merchant_by_domain: dict[str, Merchant],
    buyers_by_email: dict[str, list[Buyer]],
    buyers_by_phone: dict[str, list[Buyer]],
    buyers_by_public_id: dict[str, Buyer] | None = None,
) -> Classification:
    conv_dicts = [{"direction": c.direction, "body_text": c.body_text} for c in convs]

    email = _effective_email(ticket, convs)
    domain_match = re.search(r"@([\w.-]+)$", email.lower())
    domain = domain_match.group(1) if domain_match else ""
    db_merchant = merchant_by_domain.get(domain)
    merchant_ctx: MerchantContext | None = (
        MerchantContext(name=db_merchant.merchant_name, public_id=db_merchant.public_id,
                        domain=db_merchant.domain, status=db_merchant.status)
        if db_merchant else None
    )

    raw_db_buyers = buyers_by_email.get(email.lower(), [])
    if not raw_db_buyers:
        payload = ticket.raw_payload or {}
        phone = re.sub(r"\D", "", (payload.get("requester") or {}).get("phone") or "")
        raw_db_buyers = buyers_by_phone.get(phone, []) if phone else []
    if not raw_db_buyers and buyers_by_public_id:
        payload = ticket.raw_payload or {}
        bv_id = (payload.get("custom_fields") or {}).get("cf_buyervendor_id") or ""
        if bv_id.startswith("byr_") and bv_id in buyers_by_public_id:
            raw_db_buyers = [buyers_by_public_id[bv_id]]
    buyer_ctxs = [
        BuyerContext(name=b.buyer_name, public_id=b.public_id,
                     merchant_name=b.merchant_name, terms_status=b.terms_status)
        for b in raw_db_buyers
    ]
    is_suspended = any(b.is_suspended for b in raw_db_buyers)

    log.debug(
        "ticket %d: calling LLM (%d message(s), merchant=%s, buyers=%d)",
        ticket.freshdesk_id, len(conv_dicts),
        merchant_ctx.get("name") if merchant_ctx else None, len(buyer_ctxs),
    )
    result = await classify(ticket.subject, conv_dicts, merchant=merchant_ctx, buyers=buyer_ctxs or None)
    log.info(
        "ticket %d: llm → category=%s urgency=%d sentiment=%s sender=%s team=%s",
        ticket.freshdesk_id, result.category, result.urgency, result.sentiment, result.sender_type,
        assign_team(result.category, is_suspended),
    )
    return Classification(
        ticket_id=ticket.id,
        category=result.category, urgency=result.urgency, sentiment=result.sentiment,
        suggested_destination=result.suggested_destination, sender_type=result.sender_type,
        entities=result.entities.model_dump(exclude_none=True),
        model="claude-sonnet-4-6",
        team=assign_team(result.category, is_suspended),
    )
