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
uv run ticket-helper web --reload              # start the web server

# First-time data load
uv run ticket-helper download --skip-conversations   # fetch full ticket history to data/tickets.jsonl
uv run ticket-helper sync                            # load JSONL into DB + pull latest from Freshdesk API
uv run ticket-helper classify                        # classify all tickets (--force to redo all)

# Ongoing
uv run ticket-helper sync                      # incremental sync (--days N, default 1)
uv run ticket-helper classify                  # classify any newly unclassified tickets
```

DB migrations are handled by `app/db._migrate()` — a plain `ALTER TABLE … ADD COLUMN IF NOT EXISTS` list, no Alembic. `create_tables()` runs it on every startup.

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
  cli.py           # Typer CLI — web / download / sync / classify commands
  config.py        # pydantic-settings, loads env / Secrets Manager
  db.py            # SQLModel engine + session
  models.py        # Ticket, Conversation, Classification, Draft, DraftEdit, SentReply, AgentEvent
                   # Classification.team: collections | risk | payment_ops | other (computed at classify time)
  routes/
    inbox.py       # GET / — paginated (50/page), filtered by status/category/sender/priority/q
    ticket.py      # GET + POST /tickets/{id}
    dashboard.py   # GET /dashboard — charts filtered by status/category/sender/q
    htmx.py        # HTMX partials (regenerate, context panel, send confirmations)
  services/
    freshdesk.py   # Freshdesk v2 REST client (httpx)
    balance.py     # Balance API client + outbox
    commerce.py    # Order / shipping lookups (mocked initially)
    poller.py      # asyncio background task (syncs tickets every 90s); load_tickets_from_jsonl()
    classify_task.py  # bulk classifier (bulk-loaded, in-memory lookups, batch inserts); load_conversations_from_jsonl()
    downloader.py  # download_tickets() / download_conversations() → data/*.jsonl
  agents/
    classifier.py  # ClassifierAgent — full conversation thread, structured output, no tools
    drafter.py     # DrafterAgent — tool-using
    tools.py       # Pydantic AI tool definitions wrapping services
    prompts/       # Per-category system prompts (*.md), one file per category
  templates/
    base.html
    inbox.html     # paginated ticket list
    ticket.html
    dashboard.html # volume/dow/hour charts + category donut, all filterable
    partials/      # _draft.html, _context.html, _balance_card.html, _ticket_row.html
  static/
    htmx.min.js
    style.css
scripts/
  download_history.py    # thin wrapper around app/services/downloader.py
  load_reference_data.py # load merchants/buyers from data/*.csv into DB
```

## Architecture Decisions

- **Polling, not webhooks** — 60–120s poll avoids building a public endpoint with signature verification. Fine at this volume.
- **Server-rendered (Jinja2 + HTMX)** — no SPA, no build step, partial swaps via HTMX.
- **Drafts only** — agents always review and send. Auto-send is post-v1.
- **Single container** — web UI and background poller run in the same process via lifespan task.
- **Bulk classification** — `classify_all_unclassified()` loads all tickets, conversations, merchants, and buyers in ~5 queries then processes entirely in memory; batch-inserts results. Rules path: ~10s for 30k tickets.
- **Paginated inbox** — 50 tickets per page; category/sender/status/team filters pushed to DB subqueries so only the current page is loaded.
- **Ticket body synthesis** — Freshdesk's conversations API omits the original message (it lives in `ticket.description_text`). `ensure_conversations()` synthesises a `direction="ticket_body"` Conversation (stored with `freshdesk_id = -ticket_id`) so the first message appears in the thread and is included in classification. `list_tickets` is called with `include=description` so `description_text` is present in `raw_payload` at sync time — no extra API calls needed later.
- **Inline attachment images** — Freshdesk embeds images as signed JWT URLs (`attachment.freshdesk.com/inline/attachment?token=…`). They are publicly accessible (no auth needed) but tokens are short-lived, so we don't store or render them. Plain `description_text` is used instead.

## External Integrations

### Freshdesk (v2 REST API)
Methods: `list_tickets`, `get_ticket`, `get_conversations`, `add_private_note`, `reply`, `update_ticket`.

`list_tickets` is called with `include=description` (default) so every ticket payload includes `description_text` and `description` (HTML). The original customer message is not returned by `get_conversations` — it must be read from `raw_payload["description_text"]`.

### Balance ([getbalance.com](https://www.getbalance.com))
Read: `get_buyer`, `list_buyer_transactions`, `get_transaction`, `get_invoice`, `list_invoices`, `get_payment`.
Write (outbox): `create_outbox_draft`, `send_outbox_message`, `list_outbox`.
All write operations require explicit agent confirmation in the UI.

### Anthropic (via Pydantic AI)
- Sonnet → ClassifierAgent (cheap, fast, structured output)
- Opus → DrafterAgent (better tone/reasoning for B2B)

## AI Agents

### ClassifierAgent
One LLM call, no tools. Structured output: `category`, `urgency` (1–5), `sentiment`, `sender_type`, `entities` (order_id, invoice_id, etc.), `suggested_destination` (freshdesk_reply | balance_outbox).

Classifies the **full conversation thread** including the synthesised `ticket_body` entry (the original customer message). Each message is labeled by direction (Customer / Support / Internal Note). Per-message cap: 1500 chars; total cap: 8000 chars.

After classification, `assign_team()` (`app/services/rules.py`) derives the `team` field from category + buyer `is_suspended`. The buyer is resolved via email, phone, or `cf_buyervendor_id` — whichever matches first.

A rules-based fallback (`app/services/rules.py`) runs without an API key and uses the same input shape.

Priority categories for v1: `shipping_status`, `invoice_question`, `payment_status`. Others get a "no template yet" placeholder.

### DrafterAgent
Tool-using agent. Tools: `get_order`, `get_tracking`, `get_customer_history`, `search_kb`, and all Balance read tools. Returns: `body_text`, `confidence`, `needs_review_reason`, `destination`, `citations`.

Per-category system prompts live in `app/agents/prompts/*.md`. One file per category.

## Team Routing

`assign_team(category, is_suspended)` in `app/services/rules.py` — called after every classification and stored as `Classification.team`.

| Condition | Team |
|---|---|
| `is_suspended=True` + `credit_limit_question` | **Risk** |
| `is_suspended=True` + anything else | **Collections** |
| `payment_status` or `invoice_question` | **Collections** |
| `credit_limit_question` | **Risk** |
| `payment_failed` | **Payment Ops** |
| everything else | **Other** |

Buyer is matched via email → phone → `cf_buyervendor_id` (in that order). Suspension overrides category-based routing. `terms_status`-based routing is deferred pending confirmation of the exact status strings in the Balance data.

## Scope Cuts (v1)

- Three categories on day 2; others deferred (just add a prompt file later)
- Commerce/shipping tools mocked initially, swapped to real on day 3
- Basic auth (not OAuth) for v1
- SQLite (not RDS) for v1
- No webhooks
- Dashboard uses Chart.js (CDN) for bar/donut charts — no build step

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
6. **Buyer `terms_status` values** — what are the possible strings in the Balance data? Needed to extend team routing beyond `is_suspended` (e.g. "overdue" → Collections).
