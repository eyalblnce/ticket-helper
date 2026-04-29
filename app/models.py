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


class Classification(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True, foreign_key="ticket.id")
    category: str = ""
    urgency: int = 3              # 1–5
    sentiment: str = "neutral"   # positive | neutral | negative
    suggested_destination: str = "freshdesk_reply"
    entities: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    sender_type: str = "unknown"   # merchant | buyer | unknown
    team: str = ""                 # collections | risk | payment_ops | other
    model: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Conversation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    freshdesk_id: int = Field(unique=True, index=True)
    ticket_id: int = Field(index=True, foreign_key="ticket.id")
    direction: str = "inbound"   # inbound | outbound | private_note
    body_text: str = ""
    author_email: str = ""
    freshdesk_created_at: datetime | None = None


class Merchant(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    merchant_id: int = Field(unique=True, index=True)
    merchant_name: str = ""
    domain: str = Field(default="", index=True)
    public_id: str = ""             # ven_...
    status: str = ""
    number_of_buyers: int = 0


class Buyer(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    buyer_id: int = Field(unique=True, index=True)
    public_id: str = ""             # byr_...
    buyer_name: str = ""
    email: str = Field(default="", index=True)
    qualification_email: str = Field(default="", index=True)
    phone: str = Field(default="", index=True)  # digits-only normalized
    merchant_id: int = Field(index=True)
    merchant_name: str = ""
    terms_status: str = ""
    is_suspended: bool = False
