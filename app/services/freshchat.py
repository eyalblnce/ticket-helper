"""Freshchat v2 REST API client."""
from __future__ import annotations

from typing import Any

import httpx


class FreshchatError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        super().__init__(f"Freshchat {status}: {body}")


class FreshchatClient:
    def __init__(self, domain: str, token: str) -> None:
        # domain: "yourcompany.freshchat.com" or a regional host like "api.eu.freshchat.com"
        self._base = f"https://{domain}/v2"
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = await self._client.get(f"{self._base}{path}", params=params)
        if r.status_code != 200:
            raise FreshchatError(r.status_code, r.text)
        return r.json()

    # --- Conversation methods ---

    async def list_conversations(
        self,
        items_per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """Return all conversations, handling pagination automatically."""
        conversations: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._get(
                "/conversations",
                {"page": page, "items_per_page": items_per_page},
            )
            # Freshchat wraps results in {"conversations": [...]}
            batch = data.get("conversations", data) if isinstance(data, dict) else data
            if not batch:
                break
            conversations.extend(batch)
            if len(batch) < items_per_page:
                break
            page += 1

        return conversations

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self._get(f"/conversations/{conversation_id}")

    async def get_messages(
        self,
        conversation_id: str,
        items_per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """Return all messages for a conversation, handling pagination."""
        messages: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._get(
                f"/conversations/{conversation_id}/messages",
                {"page": page, "items_per_page": items_per_page},
            )
            batch = data.get("messages", data) if isinstance(data, dict) else data
            if not batch:
                break
            messages.extend(batch)
            if len(batch) < items_per_page:
                break
            page += 1

        return messages

    async def get_user(self, user_id: str) -> dict[str, Any]:
        return await self._get(f"/users/{user_id}")
