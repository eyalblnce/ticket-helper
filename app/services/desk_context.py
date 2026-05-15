"""Desk “what we found” — resolve Buyer/Merchant the same way as rule classification."""

from __future__ import annotations

import re

from sqlmodel import Session

from app.models import Buyer, Conversation, Merchant, Ticket
from app.services.reference_lookup import (
    find_buyer_by_public_id,
    find_buyers_by_email,
    find_buyers_by_phone,
    find_merchant_by_domain,
)


def desk_thread_section_title(ticket: Ticket, conversations: list[Conversation]) -> str:
    """Desk column heading above the thread (depends on who opened the ticket text)."""
    from app.services.ticket_thread import first_ticket_description

    td = first_ticket_description(conversations)
    if td and td.direction == "ticket_description_outbound":
        return "Support-initiated thread"
    return "From the customer"


def effective_requester_email(ticket: Ticket, conversations: list[Conversation]) -> str:
    return ticket.requester_email or next(
        (c.author_email for c in conversations if c.direction == "inbound"), ""
    )


def resolve_merchant_buyer_for_ticket(
    ticket: Ticket, conversations: list[Conversation], session: Session
) -> tuple[Merchant | None, Buyer | None]:
    """Match merchant domain first, else buyer by email → phone → cf_buyervendor_id (byr_)."""
    eff_email = effective_requester_email(ticket, conversations)
    payload = ticket.raw_payload or {}
    domain_match = re.search(r"@([\w.-]+)$", eff_email.lower())
    domain = domain_match.group(1) if domain_match else ""
    db_merchant = find_merchant_by_domain(domain, session) if domain else None
    if db_merchant:
        return db_merchant, None

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
        return None, db_buyers[0]
    return None, None


ENTITY_LABELS: dict[str, str] = {
    "order_id": "Order ID",
    "invoice_id": "Invoice ID",
    "payment_id": "Payment ID",
    "buyer_id": "Buyer ID",
    "merchant_id": "Merchant ID",
    "merchant_name": "Merchant name",
    "transaction_id": "Transaction ID",
    "balance_buyer_id": "Balance buyer ID",
}


def humanize_entities(entities: dict | None) -> tuple[list[tuple[str, str]], dict]:
    """Return (labeled rows, remaining raw dict) for desk display."""
    if not entities:
        return [], {}
    labeled: list[tuple[str, str]] = []
    rest: dict = {}
    for k, v in entities.items():
        if v is None or v == "":
            continue
        label = ENTITY_LABELS.get(k)
        if label:
            labeled.append((label, str(v)))
        else:
            rest[k] = v
    return labeled, rest
