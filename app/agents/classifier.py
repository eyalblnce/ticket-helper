"""ClassifierAgent: single LLM call, structured output, no tools."""
from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel
from pydantic_ai import Agent

MODEL = "anthropic:claude-sonnet-4-6"

SYSTEM_PROMPT = """
You are a support ticket classifier for Balance, a B2B payments platform.
Classify the ticket into exactly one category, rate urgency 1-5, assess sentiment,
extract any identifiers mentioned, and suggest where the reply should be sent.

Categories:
- shipping_status: questions about shipment tracking or delivery
- invoice_question: questions about invoice details, line items, or billing
- payment_status: questions about whether a payment was received or processed
- payment_failed: a payment attempt failed or was declined
- credit_limit_question: questions about credit limits or net terms
- refund_request: customer requesting a refund
- return_request: customer requesting a return or exchange
- damaged_or_wrong_item: item arrived damaged or incorrect
- product_question: questions about a product or service
- account_access: login issues, permissions, account setup
- other: anything that doesn't fit the above

Urgency scale: 1=low, 2=low-medium, 3=medium, 4=high, 5=critical/urgent

Sender type — who sent this ticket:
- merchant: a business that uses Balance to offer payment terms to their buyers.
  Signals: identified as merchant in context block, or asks about account settings,
  buyer management, integration issues, or outbound invoicing.
- buyer: a company that buys from a Balance merchant on net terms.
  Signals: identified as buyer in context block, or asks about their own invoice,
  payment due date, credit limit, or order they placed.
- unknown: cannot determine from the available text.

Suggested destination:
- balance_outbox: use for payment_status, payment_failed, invoice_question, credit_limit_question
- freshdesk_reply: use for everything else

Entities to extract:
- order_id: any order number mentioned
- invoice_id: any invoice number mentioned
- tracking_id: any shipment tracking number mentioned
- balance_buyer_id: Balance buyer public ID (byr_...) — use value from context block if provided
- merchant_id: Balance vendor public ID (ven_...) — use value from context block if provided
- merchant_name: merchant's company name — use value from context block if provided
- email: any email address mentioned in the body

Return ONLY valid JSON matching the schema — no explanation.
""".strip()


class MerchantContext(TypedDict, total=False):
    name: str
    public_id: str
    domain: str
    status: str


class BuyerContext(TypedDict, total=False):
    name: str
    public_id: str
    merchant_name: str
    terms_status: str


class ExtractedEntities(BaseModel):
    order_id: str | None = None
    invoice_id: str | None = None
    tracking_id: str | None = None
    balance_buyer_id: str | None = None
    merchant_id: str | None = None
    merchant_name: str | None = None
    buyer_id: str | None = None
    email: str | None = None


class TicketClassification(BaseModel):
    category: Literal[
        "shipping_status",
        "invoice_question",
        "payment_status",
        "payment_failed",
        "credit_limit_question",
        "refund_request",
        "return_request",
        "damaged_or_wrong_item",
        "product_question",
        "account_access",
        "other",
    ]
    urgency: int
    sentiment: Literal["positive", "neutral", "negative"]
    suggested_destination: Literal["freshdesk_reply", "balance_outbox"]
    sender_type: Literal["merchant", "buyer", "unknown"]
    entities: ExtractedEntities


_agent = Agent(
    MODEL,
    output_type=TicketClassification,
    system_prompt=SYSTEM_PROMPT,
    defer_model_check=True,
)


_CONV_LABEL = {"inbound": "Customer", "outbound": "Support", "private_note": "Internal Note"}
_PER_MSG_LIMIT = 1500
_TOTAL_LIMIT = 8000


async def classify(
    subject: str,
    conversations: list[dict],
    merchant: MerchantContext | None = None,
    buyers: list[BuyerContext] | None = None,
) -> TicketClassification:
    thread_parts = []
    for i, c in enumerate(conversations, 1):
        label = _CONV_LABEL.get(c["direction"], c["direction"])
        thread_parts.append(f"[Message {i} – {label}]:\n{c['body_text'][:_PER_MSG_LIMIT]}")
    thread = "\n\n".join(thread_parts)[:_TOTAL_LIMIT]
    parts = [f"Subject: {subject}\n\n{thread}"]

    if merchant:
        parts.append(
            f"\n\n[Context: Sender is a known merchant — "
            f"name={merchant.get('name')}, "
            f"id={merchant.get('public_id')}, "
            f"status={merchant.get('status')}]"
        )
    elif buyers:
        buyer_lines = "; ".join(
            f"name={b.get('name', 'unknown')}, id={b.get('public_id')}, "
            f"merchant={b.get('merchant_name')}, terms={b.get('terms_status')}"
            for b in buyers
        )
        parts.append(f"\n\n[Context: Sender is a known buyer — {buyer_lines}]")

    result = await _agent.run("".join(parts))
    return result.output
