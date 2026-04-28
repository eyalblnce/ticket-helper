"""ClassifierAgent: single LLM call, structured output, no tools."""
from __future__ import annotations

from typing import Literal

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

Suggested destination:
- balance_outbox: use for payment_status, payment_failed, invoice_question, credit_limit_question
- freshdesk_reply: use for everything else

Return ONLY valid JSON matching the schema — no explanation.
""".strip()


class ExtractedEntities(BaseModel):
    order_id: str | None = None
    invoice_id: str | None = None
    tracking_id: str | None = None
    balance_buyer_id: str | None = None
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
    entities: ExtractedEntities


_agent = Agent(
    MODEL,
    output_type=TicketClassification,
    system_prompt=SYSTEM_PROMPT,
    defer_model_check=True,
)


async def classify(subject: str, body: str) -> TicketClassification:
    prompt = f"Subject: {subject}\n\n{body[:3000]}"
    result = await _agent.run(prompt)
    return result.output
