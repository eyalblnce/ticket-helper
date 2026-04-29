"""Freshdesk v2 REST API client."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from typing import Any

import httpx


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


class FreshdeskError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        super().__init__(f"Freshdesk {status}: {body}")


class FreshdeskClient:
    def __init__(self, domain: str, api_key: str) -> None:
        # domain: "yourcompany.freshdesk.com"
        self._base = f"https://{domain}/api/v2"
        token = base64.b64encode(f"{api_key}:X".encode()).decode()
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
            timeout=30,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(3):
            r = await self._client.get(f"{self._base}{path}", params=params)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                print(f"  rate limited — waiting {retry_after}s (attempt {attempt + 1}/3)")
                await asyncio.sleep(retry_after)
                continue
            if r.status_code != 200:
                raise FreshdeskError(r.status_code, r.text)
            return r.json()
        raise FreshdeskError(429, "rate limit exceeded after retries")

    async def _post(self, path: str, json: dict[str, Any]) -> Any:
        r = await self._client.post(f"{self._base}{path}", json=json)
        if r.status_code not in (200, 201):
            raise FreshdeskError(r.status_code, r.text)
        return r.json()

    async def _put(self, path: str, json: dict[str, Any]) -> Any:
        r = await self._client.put(f"{self._base}{path}", json=json)
        if r.status_code != 200:
            raise FreshdeskError(r.status_code, r.text)
        return r.json()

    # --- Ticket methods ---

    async def list_tickets(
        self,
        updated_since: datetime | None = None,
        until: datetime | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
        per_page: int = 100,
        max_pages: int = 300,
        include: str | None = "description",
    ) -> list[dict[str, Any]]:
        """Return tickets, handling pagination up to max_pages.

        updated_since: only tickets updated after this datetime
        until: stop collecting tickets updated at or after this datetime (client-side cutoff)
        order_by / order_type: Freshdesk sort params
        include: comma-separated Freshdesk includes (default "description" for description_text)
        """
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": 1,
            "order_by": order_by,
            "order_type": order_type,
        }
        if updated_since:
            params["updated_since"] = updated_since.strftime("%Y-%m-%dT%H:%M:%SZ")
        if include:
            params["include"] = include

        tickets: list[dict[str, Any]] = []
        while params["page"] <= max_pages:
            page = await self._get("/tickets", params)
            if not page:
                break
            for t in page:
                if until:
                    t_updated = _parse_dt(t.get("updated_at"))
                    if t_updated and t_updated >= until:
                        return tickets  # past the window — done
                tickets.append(t)
            if len(page) < per_page:
                break
            params["page"] += 1

        return tickets

    async def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        return await self._get(f"/tickets/{ticket_id}")

    async def get_conversations(self, ticket_id: int) -> list[dict[str, Any]]:
        """Return all conversation messages for a ticket."""
        return await self._get(f"/tickets/{ticket_id}/conversations")

    async def add_private_note(self, ticket_id: int, body: str) -> dict[str, Any]:
        return await self._post(
            f"/tickets/{ticket_id}/notes",
            {"body": body, "private": True},
        )

    async def reply(self, ticket_id: int, body: str) -> dict[str, Any]:
        return await self._post(
            f"/tickets/{ticket_id}/reply",
            {"body": body},
        )

    async def update_ticket(self, ticket_id: int, **fields: Any) -> dict[str, Any]:
        return await self._put(f"/tickets/{ticket_id}", fields)
