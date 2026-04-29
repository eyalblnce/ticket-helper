"""Rule-based ticket classifier — no external API calls.

Signal priority (highest → lowest):
  1. Freshdesk tags already set by agents
  2. Email domain (free providers → buyer; known merchant domains → merchant)
  3. Subject + body keywords
  4. Ticket source field (portal=2 → buyer)
  5. Default → unknown
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Domain lists
# ---------------------------------------------------------------------------

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "live.com", "msn.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "ymail.com", "zoho.com",
}

# Grow this list as you identify merchant domains from your customer data.
KNOWN_MERCHANT_DOMAINS: set[str] = set()

# ---------------------------------------------------------------------------
# Keyword sets  (all lowercase)
# ---------------------------------------------------------------------------

BUYER_KEYWORDS = {
    "my invoice", "my order", "my payment", "my balance", "payment due",
    "net terms", "net 30", "net 60", "net 90", "amount due", "due date",
    "i owe", "i was charged", "i received", "i purchased", "i bought",
    "our order", "our invoice", "our payment",
}

MERCHANT_KEYWORDS = {
    "my buyer", "my buyers", "my customer", "my customers",
    "our buyer", "our buyers", "our customer", "our customers",
    "api", "webhook", "integration", "sandbox", "onboarding",
    "my account", "our account", "seller", "vendor",
    "net terms offer", "offer terms", "enable terms",
}

CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("payment_failed",        ["payment failed", "payment declined", "declined", "failed payment", "card declined"]),
    ("payment_status",        ["payment status", "payment received", "payment processed", "did you receive", "was paid"]),
    ("invoice_question",      ["invoice", "billing", "bill", "statement", "line item"]),
    ("credit_limit_question", ["credit limit", "credit line", "net terms", "net 30", "net 60", "net 90", "credit increase"]),
    ("shipping_status",       ["shipping", "shipment", "tracking", "delivery", "delivered", "package", "carrier"]),
    ("refund_request",        ["refund", "money back", "reimburs"]),
    ("return_request",        ["return", "exchange", "send back", "rma"]),
    ("damaged_or_wrong_item", ["damaged", "broken", "wrong item", "incorrect item", "defective"]),
    ("account_access",        ["login", "password", "access", "sign in", "cant log", "locked out", "reset"]),
    ("product_question",      ["how do i", "how does", "feature", "documentation", "docs", "support"]),
]

FRESHDESK_SOURCE_PORTAL = 2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_sender_type(
    requester_email: str,
    subject: str,
    body: str,
    tags: list[str],
    source: int | None,
) -> str:
    """Return 'merchant', 'buyer', or 'unknown'."""

    # 1. Explicit agent tags take top priority
    tags_lower = {t.lower() for t in tags}
    if tags_lower & {"merchant", "seller", "vendor"}:
        return "merchant"
    if tags_lower & {"buyer", "customer", "end-customer", "end_customer"}:
        return "buyer"

    # 2. Email domain
    domain = _domain(requester_email)
    if domain in KNOWN_MERCHANT_DOMAINS:
        return "merchant"
    if domain in FREE_EMAIL_DOMAINS:
        return "buyer"

    # 3. Keywords in subject + body
    text = f"{subject} {body}".lower()
    buyer_hits    = sum(1 for kw in BUYER_KEYWORDS    if kw in text)
    merchant_hits = sum(1 for kw in MERCHANT_KEYWORDS if kw in text)
    if merchant_hits > buyer_hits:
        return "merchant"
    if buyer_hits > merchant_hits:
        return "buyer"

    # 4. Ticket source — portal submissions are usually buyers
    if source == FRESHDESK_SOURCE_PORTAL:
        return "buyer"

    return "unknown"


def classify_category(subject: str, body: str) -> str:
    """Return the best-matching category string, or 'other'."""
    text = f"{subject} {body}".lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in text for kw in keywords):
            return category
    return "other"


def classify_urgency(subject: str, body: str, priority: int) -> int:
    """Map Freshdesk priority + urgent keywords to a 1–5 urgency score."""
    base = {1: 1, 2: 2, 3: 4, 4: 5}.get(priority, 2)
    text = f"{subject} {body}".lower()
    urgent_words = {"urgent", "asap", "immediately", "critical", "escalat", "overdue", "past due"}
    if any(w in text for w in urgent_words):
        base = min(5, base + 1)
    return base


def classify_sentiment(body: str) -> str:
    """Very simple positive/negative/neutral sentiment based on keywords."""
    text = body.lower()
    negative = {"frustrated", "terrible", "awful", "unacceptable", "ridiculous",
                "disappointed", "angry", "furious", "worst", "horrible", "useless"}
    positive = {"thank", "great", "appreciate", "helpful", "excellent",
                "happy", "pleased", "wonderful", "fantastic", "love"}
    neg_hits = sum(1 for w in negative if w in text)
    pos_hits = sum(1 for w in positive if w in text)
    if neg_hits > pos_hits:
        return "negative"
    if pos_hits > neg_hits:
        return "positive"
    return "neutral"


PAYMENT_CATEGORIES = {"payment_status", "payment_failed", "invoice_question", "credit_limit_question"}


def classify_ticket(
    subject: str,
    body: str,
    requester_email: str,
    priority: int,
    tags: list[str],
    source: int | None,
) -> dict:
    """Run all rules and return a dict matching the Classification model fields."""
    category = classify_category(subject, body)
    return {
        "category": category,
        "urgency": classify_urgency(subject, body, priority),
        "sentiment": classify_sentiment(body),
        "sender_type": classify_sender_type(requester_email, subject, body, tags, source),
        "suggested_destination": (
            "balance_outbox" if category in PAYMENT_CATEGORIES else "freshdesk_reply"
        ),
        "entities": {},
        "model": "rules-v1",
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _domain(email: str) -> str:
    match = re.search(r"@([\w.-]+)$", email.lower())
    return match.group(1) if match else ""
