"""Download Freshchat conversations and messages to JSONL files."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.config import settings
from app.services.freshchat import FreshchatClient, FreshchatError

log = logging.getLogger(__name__)

DATA_DIR = Path("data")
FC_CONVERSATIONS_FILE = DATA_DIR / "freshchat_conversations.jsonl"
FC_MESSAGES_FILE = DATA_DIR / "freshchat_messages.jsonl"
FC_STATE_FILE = DATA_DIR / "freshchat_state.json"
MESSAGE_DELAY = 0.3


def _load_state() -> dict:
    if FC_STATE_FILE.exists():
        return json.loads(FC_STATE_FILE.read_text())
    return {"conversations_last_page": 0, "message_ids_done": []}


def _save_state(state: dict) -> None:
    FC_STATE_FILE.write_text(json.dumps(state, indent=2))


async def download_fc_conversations(items_per_page: int = 50) -> dict:
    """Page through all Freshchat conversations and append to freshchat_conversations.jsonl.

    Resumes from the last completed page if interrupted.
    Returns {"conversations": total_written, "pages": pages_fetched}.
    """
    DATA_DIR.mkdir(exist_ok=True)
    state = _load_state()
    start_page = state.get("conversations_last_page", 0) + 1

    # Build set of already-written conversation IDs for deduplication on resume.
    seen_ids: set[str] = set()
    if FC_CONVERSATIONS_FILE.exists():
        with FC_CONVERSATIONS_FILE.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    cid = obj.get("conversation_id") or obj.get("id")
                    if cid:
                        seen_ids.add(str(cid))

    client = FreshchatClient(settings.freshchat_domain, settings.freshchat_token)
    total_written = 0
    pages_fetched = 0

    try:
        page = start_page
        while True:
            log.info("fetching conversations page %d …", page)
            try:
                data = await client._get(
                    "/conversations",
                    {"page": page, "items_per_page": items_per_page},
                )
            except FreshchatError as e:
                log.error("freshchat error on page %d: %s", page, e)
                break

            batch = data.get("conversations", data) if isinstance(data, dict) else data
            if not batch:
                log.info("page %d returned 0 results — done", page)
                break

            new_in_page = 0
            with FC_CONVERSATIONS_FILE.open("a") as f:
                for conv in batch:
                    cid = str(conv.get("conversation_id") or conv.get("id", ""))
                    if cid and cid not in seen_ids:
                        f.write(json.dumps(conv) + "\n")
                        seen_ids.add(cid)
                        total_written += 1
                        new_in_page += 1

            state["conversations_last_page"] = page
            _save_state(state)
            pages_fetched += 1
            log.info("page %d: %d new conversations (total: %d)", page, new_in_page, total_written)

            if len(batch) < items_per_page:
                break
            page += 1
    finally:
        await client.close()

    return {"conversations": total_written, "pages": pages_fetched}


async def download_fc_messages() -> int:
    """Fetch messages for every conversation in freshchat_conversations.jsonl.

    Appends {"conversation_id": ..., "messages": [...]} lines to freshchat_messages.jsonl.
    Returns count of message batches fetched.
    """
    if not FC_CONVERSATIONS_FILE.exists():
        log.error("freshchat_conversations.jsonl not found — run Phase 1 first")
        return 0

    state = _load_state()
    done_ids: set[str] = set(str(x) for x in state.get("message_ids_done", []))

    all_ids: list[str] = []
    seen: set[str] = set()
    with FC_CONVERSATIONS_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            cid = str(obj.get("conversation_id") or obj.get("id", ""))
            if cid and cid not in seen:
                seen.add(cid)
                all_ids.append(cid)

    remaining = [cid for cid in all_ids if cid not in done_ids]
    log.info("%d conversations total, %d need messages", len(all_ids), len(remaining))

    client = FreshchatClient(settings.freshchat_domain, settings.freshchat_token)
    fetched = 0

    try:
        for i, cid in enumerate(remaining, 1):
            try:
                messages = await client.get_messages(cid)
                with FC_MESSAGES_FILE.open("a") as f:
                    f.write(json.dumps({"conversation_id": cid, "messages": messages}) + "\n")
                done_ids.add(cid)
            except FreshchatError as e:
                log.error("[%s] error fetching messages: %s", cid, e)

            fetched += 1
            if i % 100 == 0 or i == len(remaining):
                state["message_ids_done"] = list(done_ids)
                _save_state(state)
                log.info("messages: %d/%d (%.0f%%)", i, len(remaining), i / len(remaining) * 100)

            await asyncio.sleep(MESSAGE_DELAY)
    finally:
        await client.close()

    return fetched
