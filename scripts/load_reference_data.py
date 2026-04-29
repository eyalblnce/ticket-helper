"""Populate Merchant and Buyer tables from data/merchants.csv and data/buyers.csv.

Usage:
    uv run python scripts/load_reference_data.py

Safe to re-run: truncates and reloads both tables each time.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

# Ensure the project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session, text

from app.db import create_tables, engine
from app.models import Buyer, Merchant

DATA_DIR = Path(__file__).parent.parent / "data"
BATCH_SIZE = 5000


def _normalize_domain(raw: str) -> str:
    d = re.sub(r"^https?://", "", raw.strip().lower())
    d = re.sub(r"^www\.", "", d)
    return d.rstrip("/")


def _normalize_phone(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def load_merchants(session: Session) -> int:
    session.exec(text("DELETE FROM merchant"))
    session.commit()

    path = DATA_DIR / "merchants.csv"
    count = 0
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            merchant_id_raw = row.get("MERCHANT_ID", "").strip()
            if not merchant_id_raw:
                continue
            domain = _normalize_domain(row.get("MERCHANT_DOMAIN", ""))
            try:
                num_buyers = int(row.get("NUMBER_OF_BUYERS") or 0)
            except ValueError:
                num_buyers = 0
            session.add(Merchant(
                merchant_id=int(merchant_id_raw),
                merchant_name=row.get("MERCHANT_NAME", "").strip(),
                domain=domain,
                public_id=row.get("MERCHANT_PUBLIC_ID", "").strip(),
                status=row.get("MERCHANT_STATUS", "").strip(),
                number_of_buyers=num_buyers,
            ))
            count += 1

    session.commit()
    return count


def load_buyers(session: Session) -> int:
    session.exec(text("DELETE FROM buyer"))
    session.commit()

    path = DATA_DIR / "buyers.csv"
    count = 0
    seen_ids: set[int] = set()
    batch: list[Buyer] = []

    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            buyer_id_raw = row.get("BUYER_ID", "").strip()
            if not buyer_id_raw:
                continue
            buyer_id = int(buyer_id_raw)
            if buyer_id in seen_ids:
                continue
            seen_ids.add(buyer_id)
            merchant_id_raw = row.get("BUYER_MERCHANT_ID", "").strip()
            if not merchant_id_raw:
                continue
            batch.append(Buyer(
                buyer_id=int(buyer_id_raw),
                public_id=row.get("BUYER_PUBLIC_ID", "").strip(),
                buyer_name=row.get("BUYER_NAME", "").strip(),
                email=row.get("BUYER_EMAIL", "").strip().lower(),
                qualification_email=row.get("QUALIFICATION_EMAIL", "").strip().lower(),
                phone=_normalize_phone(row.get("BUYER_PHONE", "")),
                merchant_id=int(merchant_id_raw),
                merchant_name=row.get("BUYER_MERCHANT_NAME", "").strip(),
                terms_status=row.get("CURRENT_TERMS_STATUS", "").strip(),
                is_suspended=(row.get("IS_CURRENTLY_SUSPENDED", "No").strip().lower() == "yes"),
            ))
            count += 1
            if len(batch) >= BATCH_SIZE:
                session.add_all(batch)
                session.commit()
                batch = []
                print(f"  {count} buyers loaded...", end="\r")

    if batch:
        session.add_all(batch)
        session.commit()

    return count


def main() -> None:
    create_tables()
    with Session(engine) as session:
        print("Loading merchants...")
        m = load_merchants(session)
        print(f"  {m} merchants loaded.")

        print("Loading buyers...")
        b = load_buyers(session)
        print(f"\n  {b} buyers loaded.")

    print(f"\nDone. Loaded {m} merchants and {b} buyers.")


if __name__ == "__main__":
    main()
