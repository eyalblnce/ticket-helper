"""Download all historical Freshdesk tickets and conversations.

Usage:
    uv run python scripts/download_history.py
    uv run python scripts/download_history.py --months 2
    uv run python scripts/download_history.py --skip-conversations
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.downloader import download_conversations, download_tickets, TICKETS_FILE, CONVERSATIONS_FILE


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=None)
    parser.add_argument("--skip-conversations", action="store_true")
    args = parser.parse_args()

    print("=== Phase 1: Tickets ===")
    t = await download_tickets(max_months=args.months)
    print(f"Phase 1 complete — {t['tickets']} new tickets, {t['months_skipped']} month(s) skipped (already done).\n")

    if not args.skip_conversations:
        print("=== Phase 2: Conversations ===")
        count = await download_conversations()
        print(f"Phase 2 complete — {count} conversation batches written to {CONVERSATIONS_FILE}")
    else:
        print("Conversations skipped (--skip-conversations)")


if __name__ == "__main__":
    asyncio.run(main())
