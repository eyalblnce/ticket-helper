"""Quick smoke-test: verify Freshdesk and Freshchat credentials work."""
import asyncio
import sys
from pathlib import Path

# Allow running from repo root: python scripts/verify_connections.py
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.services.freshchat import FreshchatClient, FreshchatError
from app.services.freshdesk import FreshdeskClient, FreshdeskError


async def check_freshdesk() -> None:
    print(f"Freshdesk → https://{settings.freshdesk_domain}/api/v2")
    client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
    try:
        # Fetch only the first page (5 tickets) to verify connectivity
        tickets = await client.list_tickets(per_page=5, max_pages=1)
        print(f"  ✓ Listed tickets — got {len(tickets)}")
        if tickets:
            t = tickets[0]
            print(f"  ✓ First ticket: #{t['id']} — {t.get('subject', '(no subject)')!r}")
            convs = await client.get_conversations(t["id"])
            print(f"  ✓ Conversations for #{t['id']}: {len(convs)} message(s)")
    except FreshdeskError as e:
        print(f"  ✗ {e}")
    finally:
        await client.close()


async def check_freshchat() -> None:
    if not settings.freshchat_domain or not settings.freshchat_token:
        print("Freshchat → skipped (FRESHCHAT_DOMAIN / FRESHCHAT_TOKEN not set)")
        return
    print(f"Freshchat → https://{settings.freshchat_domain}/v2")
    client = FreshchatClient(settings.freshchat_domain, settings.freshchat_token)
    try:
        convs = await client.list_conversations(items_per_page=5)
        print(f"  ✓ Listed conversations — got {len(convs)} (first page, up to 5)")
        if convs:
            c = convs[0]
            cid = c.get("conversation_id") or c.get("id")
            print(f"  ✓ First conversation: {cid}")
            msgs = await client.get_messages(str(cid), items_per_page=5)
            print(f"  ✓ Messages: {len(msgs)}")
    except FreshchatError as e:
        print(f"  ✗ {e}")
    finally:
        await client.close()


async def main() -> None:
    await check_freshdesk()
    print()
    await check_freshchat()


asyncio.run(main())
