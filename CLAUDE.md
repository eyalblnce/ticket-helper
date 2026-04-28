# Support Co-Pilot

A back-office web app that pulls Freshdesk tickets, classifies them, drafts AI replies, and lets agents review-and-send. Sits alongside Freshdesk rather than embedded in it. Target: 50–200 tickets/day. Humans always send — v1 drafts only.

## Tech Stack

| Layer | Choice |
|---|---|
| Language / package manager | Python 3.12, `uv` |
| Web framework | FastAPI |
| Templating + interactivity | Jinja2 + HTMX (server-rendered, no SPA) |
| AI framework | Pydantic AI |
| LLM | Anthropic Claude — Sonnet for classification, Opus for drafting |
| ORM | SQLModel (SQLAlchemy 2 + Pydantic) |
| Database | **SQLite** (local dev and initial prod). Migrate to RDS Postgres when SQLite becomes a bottleneck. |
| Background work | `asyncio` task in FastAPI lifespan (polling every 60–120s) |
| Auth | HTTP Basic Auth for v1. Upgrade to OAuth for production. |
| Container | Docker |
| Hosting | AWS App Runner (simplest path — single container, autoscale) |
| Secrets | AWS Secrets Manager in prod; `.env` locally |
| Logs | CloudWatch Logs |

## Running Locally

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Set env vars (or `.env`):
```
FRESHDESK_API_KEY=...
FRESHDESK_DOMAIN=yourcompany.freshdesk.com
BALANCE_API_KEY=...
ANTHROPIC_API_KEY=...
DATABASE_URL=sqlite:///./dev.db   # default
BASIC_AUTH_USER=admin
BASIC_AUTH_PASSWORD=...
```

## Project Layout

```
app/
  main.py          # FastAPI app + lifespan (starts poller)
  config.py        # pydantic-settings, loads env / Secrets Manager
  db.py            # SQLModel engine + session
  models.py        # Ticket, Conversation, Classification, Draft, DraftEdit, SentReply, AgentEvent
  routes/
    inbox.py       # GET /
    ticket.py      # GET + POST /tickets/{id}
    dashboard.py   # GET /dashboard
    htmx.py        # HTMX partials (regenerate, context panel, send confirmations)
  services/
    freshdesk.py   # Freshdesk v2 REST client (httpx)
    balance.py     # Balance API client + outbox
    commerce.py    # Order / shipping lookups (mocked initially)
    poller.py      # asyncio background task
  agents/
    classifier.py  # ClassifierAgent — structured output, no tools
    drafter.py     # DrafterAgent — tool-using
    tools.py       # Pydantic AI tool definitions wrapping services
    prompts/       # Per-category system prompts (*.md), one file per category
  templates/
    base.html
    inbox.html
    ticket.html
    dashboard.html
    partials/      # _draft.html, _context.html, _balance_card.html, _ticket_row.html
  static/
    htmx.min.js
    style.css
```

## Architecture Decisions

- **Polling, not webhooks** — 60–120s poll avoids building a public endpoint with signature verification. Fine at this volume.
- **Server-rendered (Jinja2 + HTMX)** — no SPA, no build step, partial swaps via HTMX.
- **Drafts only** — agents always review and send. Auto-send is post-v1.
- **Single container** — web UI and background poller run in the same process via lifespan task.

## External Integrations

### Freshdesk (v2 REST API)
Methods: `list_tickets`, `get_ticket`, `get_conversations`, `add_private_note`, `reply`, `update_ticket`.

### Balance ([getbalance.com](https://www.getbalance.com))
Read: `get_buyer`, `list_buyer_transactions`, `get_transaction`, `get_invoice`, `list_invoices`, `get_payment`.
Write (outbox): `create_outbox_draft`, `send_outbox_message`, `list_outbox`.
All write operations require explicit agent confirmation in the UI.

### Anthropic (via Pydantic AI)
- Sonnet → ClassifierAgent (cheap, fast, structured output)
- Opus → DrafterAgent (better tone/reasoning for B2B)

## AI Agents

### ClassifierAgent
One LLM call, no tools. Structured output: `category`, `urgency` (1–5), `sentiment`, `entities` (order_id, invoice_id, etc.), `suggested_destination` (freshdesk_reply | balance_outbox).

Priority categories for v1: `shipping_status`, `invoice_question`, `payment_status`. Others get a "no template yet" placeholder.

### DrafterAgent
Tool-using agent. Tools: `get_order`, `get_tracking`, `get_customer_history`, `search_kb`, and all Balance read tools. Returns: `body_text`, `confidence`, `needs_review_reason`, `destination`, `citations`.

Per-category system prompts live in `app/agents/prompts/*.md`. One file per category.

## Scope Cuts (v1)

- Three categories on day 2; others deferred (just add a prompt file later)
- Commerce/shipping tools mocked initially, swapped to real on day 3
- Basic auth (not OAuth) for v1
- SQLite (not RDS) for v1
- No webhooks
- Dashboard uses plain HTML tables + inline SVG sparklines, no charting lib

## Deferred (Post-v1)

- Auto-send for high-confidence categories
- Chat channel (Freshchat)
- OAuth / SSO
- Webhook-based ingestion
- Vector DB / embedding KB retrieval
- Fine-tuning on captured edit data
- Multi-language support

## Open Questions

1. **Commerce backend** — Shopify, internal ERP, or custom? Determines `get_order` / `get_customer_history` implementation.
2. **Shipping carrier** — which carrier for tracking lookups, or read from order record only?
3. **Agent identity** — shared basic-auth password or per-agent credentials in a config file?
4. **Balance outbox policy** — is the suggested destination always overridable, or do some categories force Balance outbox?
5. **Balance API access** — sandbox credentials and v2 API docs needed before DrafterAgent work begins.
