from datetime import datetime
from typing import Any

from sqlmodel import JSON, Column, Field, SQLModel


class Ticket(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    freshdesk_id: int = Field(unique=True, index=True)
    subject: str = ""
    requester_email: str = ""
    requester_name: str = ""
    status: int = 2          # 2=open 3=pending 4=resolved 5=closed
    priority: int = 1        # 1=low 2=medium 3=high 4=urgent
    freshdesk_created_at: datetime | None = None
    freshdesk_updated_at: datetime | None = None
    synced_at: datetime = Field(default_factory=datetime.utcnow)
    raw_payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class Conversation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    freshdesk_id: int = Field(unique=True, index=True)
    ticket_id: int = Field(index=True, foreign_key="ticket.id")
    direction: str = "inbound"   # inbound | outbound | private_note
    body_text: str = ""
    author_email: str = ""
    freshdesk_created_at: datetime | None = None
