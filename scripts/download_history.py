"""Download all historical Freshdesk tickets and conversations for training data.

Phase 1: Fetch all ticket metadata (month-by-month windows, latest first).
Phase 2: Fetch conversations for every ticket.

Resume-safe: re-running skips already-completed months and already-fetched conversations.

Usage:
    uv run python scripts/download_history.py                  # full run
    uv run python scripts/download_history.py --months 2       # last 2 months only
    uv run python scripts/download_history.py --skip-conversations
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from calendar import monthrange
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.services.freshdesk import FreshdeskClient, FreshdeskError

DATA_DIR = Path("data")
TICKETS_FILE = DATA_DIR / "tickets.jsonl"
CONVERSATIONS_FILE = DATA_DIR / "conversations.jsonl"
STATE_FILE = DATA_DIR / "state.json"
CONVERSATION_DELAY = 0.3  # seconds between conversation fetches


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"phase": "tickets", "months_done": [], "conversation_ids_done": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Month window utilities
# ---------------------------------------------------------------------------

def month_window(year: int, month: int) -> tuple[datetime, datetime]:
    """Return (month_start, month_end) as naive UTC datetimes."""
    start = datetime(year, month, 1)
    last_day = monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59)
    return start, end


def prev_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


# ---------------------------------------------------------------------------
# Phase 1: tickets
# ---------------------------------------------------------------------------

async def download_tickets(max_months: int | None) -> int:
    DATA_DIR.mkdir(exist_ok=True)
    state = load_state()
    months_done: set[str] = set(state.get("months_done", []))

    client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
    total = 0
    now = datetime.utcnow()
    year, month = now.year, now.month
    months_fetched = 0

    try:
        while True:
            label = f"{year}-{month:02d}"

            if max_months and months_fetched >= max_months:
                print(f"  reached --months {max_months} limit, stopping")
                break

            if label in months_done:
                print(f"  {label} already done, skipping")
                year, month = prev_month(year, month)
                months_fetched += 1
                continue

            start, end = month_window(year, month)
            print(f"  fetching {label} ({start.date()} → {end.date()}) ...", end=" ", flush=True)

            try:
                tickets = await client.list_tickets(
                    updated_since=start,
                    until=end,
                    order_by="updated_at",
                    order_type="asc",
                )
            except FreshdeskError as e:
                print(f"ERROR: {e}")
                break

            if not tickets:
                print("0 tickets — reached account history start")
                break

            with TICKETS_FILE.open("a") as f:
                for t in tickets:
                    f.write(json.dumps(t) + "\n")

            months_done.add(label)
            state["months_done"] = sorted(months_done, reverse=True)
            save_state(state)

            total += len(tickets)
            print(f"{len(tickets)} tickets  (total so far: {total})")

            months_fetched += 1
            year, month = prev_month(year, month)

    finally:
        await client.close()

    return total


# ---------------------------------------------------------------------------
# Phase 2: conversations
# ---------------------------------------------------------------------------

async def download_conversations() -> int:
    if not TICKETS_FILE.exists():
        print("  tickets.jsonl not found — run Phase 1 first")
        return 0

    state = load_state()
    done_ids: set[int] = set(state.get("conversation_ids_done", []))

    # Collect all ticket IDs from tickets.jsonl (deduplicated)
    all_ids: list[int] = []
    seen: set[int] = set()
    with TICKETS_FILE.open() as f:
        for line in f:
            t = json.loads(line)
            tid = t["id"]
            if tid not in seen:
                seen.add(tid)
                all_ids.append(tid)

    remaining = [tid for tid in all_ids if tid not in done_ids]
    print(f"  {len(all_ids)} tickets total, {len(remaining)} need conversations")

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
                print(f"  [#{ticket_id}] error: {e}")

            fetched += 1
            if i % 100 == 0 or i == len(remaining):
                state["conversation_ids_done"] = list(done_ids)
                save_state(state)
                pct = i / len(remaining) * 100
                print(f"  conversations: {i}/{len(remaining)} ({pct:.0f}%)")

            await asyncio.sleep(CONVERSATION_DELAY)
    finally:
        await client.close()

    return fetched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=None, help="limit to N most recent months")
    parser.add_argument("--skip-conversations", action="store_true")
    args = parser.parse_args()

    print("=== Phase 1: Tickets ===")
    ticket_count = await download_tickets(max_months=args.months)
    print(f"Phase 1 complete — {ticket_count} tickets written to {TICKETS_FILE}\n")

    if not args.skip_conversations:
        print("=== Phase 2: Conversations ===")
        conv_count = await download_conversations()
        print(f"Phase 2 complete — {conv_count} conversation batches written to {CONVERSATIONS_FILE}")
    else:
        print("Conversations skipped (--skip-conversations)")


asyncio.run(main())
