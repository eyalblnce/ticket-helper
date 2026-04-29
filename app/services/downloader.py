"""Download historical Freshdesk tickets and conversations to JSONL files."""
from __future__ import annotations

import json
import logging
from calendar import monthrange
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.services.freshdesk import FreshdeskClient, FreshdeskError

log = logging.getLogger(__name__)

DATA_DIR = Path("data")
TICKETS_FILE = DATA_DIR / "tickets.jsonl"
CONVERSATIONS_FILE = DATA_DIR / "conversations.jsonl"
STATE_FILE = DATA_DIR / "state.json"
CONVERSATION_DELAY = 0.3


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"phase": "tickets", "months_done": [], "conversation_ids_done": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def month_window(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    last_day = monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59)
    return start, end


def prev_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


async def download_tickets(max_months: int | None = None) -> dict:
    """Fetch all ticket metadata month-by-month.

    Returns dict with fetched/skipped month counts and total tickets written.
    """
    DATA_DIR.mkdir(exist_ok=True)
    state = load_state()
    months_done: set[str] = set(state.get("months_done", []))

    client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
    total = 0
    months_fetched = 0
    months_skipped = 0
    now = datetime.utcnow()
    year, month = now.year, now.month

    try:
        while True:
            label = f"{year}-{month:02d}"

            if max_months and months_fetched >= max_months:
                log.info("reached --months %d limit", max_months)
                break

            if label in months_done:
                months_skipped += 1
                year, month = prev_month(year, month)
                months_fetched += 1
                continue

            start, end = month_window(year, month)
            log.info("fetching %s (%s → %s) …", label, start.date(), end.date())

            try:
                tickets = await client.list_tickets(
                    updated_since=start,
                    until=end,
                    order_by="updated_at",
                    order_type="asc",
                )
            except FreshdeskError as e:
                log.error("freshdesk error: %s", e)
                break

            if not tickets:
                log.info("0 tickets — reached account history start")
                break

            with TICKETS_FILE.open("a") as f:
                for t in tickets:
                    f.write(json.dumps(t) + "\n")

            months_done.add(label)
            state["months_done"] = sorted(months_done, reverse=True)
            save_state(state)

            total += len(tickets)
            log.info("%s: %d tickets (total: %d)", label, len(tickets), total)

            months_fetched += 1
            year, month = prev_month(year, month)
    finally:
        await client.close()

    return {"tickets": total, "months_fetched": months_fetched - months_skipped, "months_skipped": months_skipped}


async def download_conversations() -> int:
    """Fetch conversations for every ticket in tickets.jsonl. Returns count fetched."""
    if not TICKETS_FILE.exists():
        log.error("tickets.jsonl not found — run download tickets first")
        return 0

    state = load_state()
    done_ids: set[int] = set(state.get("conversation_ids_done", []))

    all_ids: list[int] = []
    seen: set[int] = set()
    with TICKETS_FILE.open() as f:
        for line in f:
            tid = json.loads(line)["id"]
            if tid not in seen:
                seen.add(tid)
                all_ids.append(tid)

    remaining = [tid for tid in all_ids if tid not in done_ids]
    log.info("%d tickets total, %d need conversations", len(all_ids), len(remaining))

    import asyncio
    client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
    fetched = 0

    try:
        for i, ticket_id in enumerate(remaining, 1):
            try:
                convs = await client.get_conversations(ticket_id)
                with CONVERSATIONS_FILE.open("a") as f:
                    f.write(json.dumps({"ticket_id": ticket_id, "conversations": convs}) + "\n")
                done_ids.add(ticket_id)
            except FreshdeskError as e:
                log.error("[#%d] error: %s", ticket_id, e)

            fetched += 1
            if i % 100 == 0 or i == len(remaining):
                state["conversation_ids_done"] = list(done_ids)
                save_state(state)
                log.info("conversations: %d/%d (%.0f%%)", i, len(remaining), i / len(remaining) * 100)

            await asyncio.sleep(CONVERSATION_DELAY)
    finally:
        await client.close()

    return fetched
