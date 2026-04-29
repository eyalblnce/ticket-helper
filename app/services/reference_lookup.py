"""DB-backed lookups for merchant and buyer reference data."""
from __future__ import annotations

import re

from sqlmodel import Session, or_, select

from app.models import Buyer, Merchant


def find_merchant_by_domain(domain: str, session: Session) -> Merchant | None:
    if not domain:
        return None
    return session.exec(select(Merchant).where(Merchant.domain == domain)).first()


def find_buyers_by_email(email: str, session: Session) -> list[Buyer]:
    if not email:
        return []
    email = email.strip().lower()
    return list(session.exec(
        select(Buyer).where(
            or_(Buyer.email == email, Buyer.qualification_email == email)
        ).limit(5)
    ).all())


def find_buyers_by_phone(phone: str, session: Session) -> list[Buyer]:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return []
    return list(session.exec(
        select(Buyer).where(Buyer.phone == digits).limit(5)
    ).all())


def find_buyer_by_public_id(public_id: str, session: Session) -> Buyer | None:
    if not public_id:
        return None
    return session.exec(select(Buyer).where(Buyer.public_id == public_id)).first()


def find_merchant_by_public_id(public_id: str, session: Session) -> Merchant | None:
    if not public_id:
        return None
    return session.exec(select(Merchant).where(Merchant.public_id == public_id)).first()


def get_merchant_domains(session: Session) -> set[str]:
    """Return all non-empty merchant domains. Called once at startup."""
    rows = session.exec(select(Merchant.domain).where(Merchant.domain != "")).all()
    return set(rows)
