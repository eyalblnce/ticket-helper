"""Background poller: syncs open tickets from Freshdesk into the local DB."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import Ticket
from app.services.freshdesk import FreshdeskClient, FreshdeskError

log = logging.getLogger(__name__)

POLL_INTERVAL = 90  # seconds


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(val, fmt)
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _upsert_ticket(session: Session, payload: dict) -> None:
    freshdesk_id = payload["id"]
    existing = session.exec(
        select(Ticket).where(Ticket.freshdesk_id == freshdesk_id)
    ).first()

    requester = payload.get("requester", {}) or {}

    values = dict(
        freshdesk_id=freshdesk_id,
        subject=payload.get("subject") or "",
        requester_email=requester.get("email") or payload.get("email") or "",
        requester_name=requester.get("name") or "",
        status=payload.get("status", 2),
        priority=payload.get("priority", 1),
        freshdesk_created_at=_parse_dt(payload.get("created_at")),
        freshdesk_updated_at=_parse_dt(payload.get("updated_at")),
        synced_at=datetime.utcnow(),
        raw_payload=payload,
    )

    if existing:
        for k, v in values.items():
            setattr(existing, k, v)
        session.add(existing)
    else:
        session.add(Ticket(**values))


TICKETS_JSONL = Path("data/tickets.jsonl")
LOAD_LOG_EVERY = 5000  # lines
INITIAL_SYNC_DAYS = 30  # on first run, fetch tickets updated in last N days


def load_tickets_from_jsonl(path: Path = TICKETS_JSONL) -> dict:
    """Load tickets.jsonl into the Ticket table. Returns loaded/skipped counts."""
    if not path.exists():
        log.warning("tickets file not found: %s", path)
        return {"loaded": 0, "updated": 0}

    total_lines = sum(1 for _ in path.open())
    log.info("load_tickets: %d lines in %s", total_lines, path)
    loaded = updated = 0

    with path.open() as f:
        with Session(engine) as session:
            for i, line in enumerate(f, 1):
                payload = json.loads(line)
                existing = session.exec(
                    select(Ticket).where(Ticket.freshdesk_id == payload["id"])
                ).first()
                _upsert_ticket(session, payload)
                if existing:
                    updated += 1
                else:
                    loaded += 1

                if i % 1000 == 0:
                    session.commit()
                    log.info("load_tickets: [%d/%d] loaded=%d updated=%d", i, total_lines, loaded, updated)

            session.commit()

    log.info("load_tickets: done — loaded=%d updated=%d", loaded, updated)
    return {"loaded": loaded, "updated": updated}


async def sync_once(updated_since: datetime | None = None) -> int:
    """Fetch tickets updated since a given time and upsert into DB. Returns count synced."""
    if updated_since is None:
        updated_since = datetime.utcnow() - timedelta(days=INITIAL_SYNC_DAYS)

    client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
    try:
        tickets = await client.list_tickets(updated_since=updated_since, per_page=100)
    finally:
        await client.close()

    with Session(engine) as session:
        for payload in tickets:
            _upsert_ticket(session, payload)
        session.commit()

    log.info("synced %d tickets", len(tickets))
    return len(tickets)


async def run_poller() -> None:
    """Long-running background task. Called from FastAPI lifespan."""
    log.info("poller starting")
    last_sync: datetime | None = None

    while True:
        try:
            updated_since = last_sync - timedelta(seconds=30) if last_sync else None
            count = await sync_once(updated_since=updated_since)
            last_sync = datetime.utcnow()
            log.info("poll complete, %d tickets synced", count)
        except FreshdeskError as e:
            log.error("freshdesk error: %s", e)
        except Exception:
            log.exception("unexpected poller error")

        await asyncio.sleep(POLL_INTERVAL)
