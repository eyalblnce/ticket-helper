# Support Co-Pilot — Build Plan

A back-office web application that pulls Freshdesk tickets, classifies them, drafts replies with AI, and lets agents review-and-send. Sits **alongside** Freshdesk rather than embedded in it. Built for ~50–200 tickets/day with a 2–3 day delivery window.

---

## 1. Goals

1. Cut the time agents spend gathering context (orders, invoices, payment status, customer history) by surfacing it on one screen.
2. Cut the time agents spend writing replies by producing AI drafts they edit-and-send.
3. Keep humans in the loop for v1: **drafts only, agent always sends**.
4. Capture every edit between draft and final reply so we can measure quality and graduate safe categories to auto-send later.
5. Provide a dashboard so leadership can see volume, throughput, and AI quality at a glance.

---

## 2. Why we're building (not buying)

Open-source landscape was checked. The viable options split into:

- **Full helpdesk replacements** (Chatwoot, FreeScout, Zammad) — wrong because they require migrating off Freshdesk.
- **Commercial AI layers** (eesel AI, CoSupport AI, Lindy) — closed source, recurring cost, less flexible around Balance integration.

There is no mature OSS "AI back-office layer for Freshdesk." For our volume and the custom Balance integration, a focused FastAPI app is faster to build than to evaluate, contract, and integrate any third-party tool.

---

## 3. Tech Stack

| Layer | Choice |
|---|---|
| Language / package mgr | Python 3.12, [uv](https://github.com/astral-sh/uv) |
| Web framework | FastAPI |
| Templating | Jinja2 (server-rendered) |
| Frontend interactivity | HTMX (partial swaps, no SPA) |
| AI agent framework | Pydantic AI |
| LLM | Anthropic Claude (Sonnet for classification, Opus for drafting) |
| ORM | SQLModel (SQLAlchemy 2 + Pydantic) |
| Database | Postgres (RDS in prod, SQLite for local dev) |
| Background work | `asyncio` task started in FastAPI lifespan |
| Container | Docker |
| Hosting | AWS App Runner |
| Secrets | AWS Secrets Manager |
| Logs / metrics | CloudWatch Logs |

---

## 4. High-Level Architecture

```
┌───────────────────────────────────────────────────────────┐
│  FastAPI app (single container on App Runner)             │
│                                                           │
│  ┌─────────────────┐    ┌──────────────────────────┐     │
│  │ Web UI          │    │ Background poller        │     │
│  │ Jinja2 + HTMX   │    │ asyncio task (60–120s)   │     │
│  │  - inbox        │    │  - poll Freshdesk        │     │
│  │  - ticket view  │    │  - classify              │     │
│  │  - dashboard    │    │  - draft                 │     │
│  └────────┬────────┘    └─────────┬────────────────┘     │
│           └───────────┬───────────┘                       │
│                       ▼                                   │
│              ┌────────────────┐                           │
│              │ Postgres (RDS) │                           │
│              └────────────────┘                           │
└───────────────────────────────────────────────────────────┘
        │                │                  │
        ▼                ▼                  ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
│  Freshdesk   │  │   Balance    │  │  Anthropic API   │
│     API      │  │  API + Outbox│  │  (Pydantic AI)   │
└──────────────┘  └──────────────┘  └──────────────────┘
```

Polling beats webhooks for a 2-day build (no public webhook endpoint to secure, no signature verification, no replay logic). At 50–200 tickets/day a 60–120s poll is fine.

---

## 5. Data Model

```python
# core entities (SQLModel)

Ticket
  id: int (pk)
  freshdesk_id: int (unique)
  subject: str
  requester_email: str
  status: str         # open | pending | resolved | replied_by_us
  channel: str        # email | chat (later)
  created_at, updated_at: datetime
  raw_payload: JSON   # last full Freshdesk payload, for debugging

Conversation              # ticket thread messages
  id, ticket_id (fk)
  freshdesk_id: int
  direction: enum(inbound, outbound, private_note)
  body_text: str
  author_email: str
  created_at: datetime

Classification
  id, ticket_id (fk)
  category: str          # shipping | invoice | payment_status | refund | ...
  urgency: int           # 1..5
  sentiment: str         # positive | neutral | negative
  entities: JSON         # {order_id, invoice_id, tracking_id, balance_buyer_id, ...}
  model: str
  created_at: datetime

Draft
  id, ticket_id (fk)
  body_text: str
  confidence: float
  needs_review_reason: str | null
  destination: enum(freshdesk_reply, balance_outbox)
  citations: JSON        # [{source: kb|order|invoice, ref: id}]
  tools_used: JSON       # [{name, args, result_summary}]
  status: enum(pending, edited, sent, discarded)
  model: str
  created_at: datetime

DraftEdit              # captured every time agent modifies a draft
  id, draft_id (fk)
  original_text: str
  final_text: str
  diff_chars: int
  edited_by: str
  edited_at: datetime

SentReply
  id, ticket_id (fk), draft_id (fk, nullable)
  body_text: str
  destination: enum(freshdesk, balance_outbox)
  external_id: str       # freshdesk reply id OR balance outbox message id
  sent_by: str
  sent_at: datetime

AgentEvent             # for dashboard/auditing
  id, agent_email, ticket_id (fk), event_type, payload: JSON, created_at
```

---

## 6. External Integrations

### 6.1 Freshdesk

Wrap the v2 REST API. Methods we need:

- `list_tickets(updated_since)` — for poller
- `get_ticket(id)` — full ticket
- `get_conversations(ticket_id)` — thread messages
- `add_private_note(ticket_id, body)` — write AI draft as private note
- `reply(ticket_id, body)` — send the agent-approved reply
- `update_ticket(id, tags=[...], priority=...)` — set category tag, urgency

Auth: API key in Secrets Manager.

### 6.2 Balance ([getbalance.com](https://www.getbalance.com))

Balance is the source of truth for invoices, transactions, net-terms, and buyer credit. Most B2B "where's my invoice / when's my payment due / why was this declined" questions resolve faster with Balance data than anywhere else.

**Read-side tools (used by the AI drafter):**

- `balance_get_buyer(buyer_id_or_email)` — credit limit, net terms tier, status
- `balance_list_buyer_transactions(buyer_id, since=...)` — recent transactions
- `balance_get_transaction(transaction_id)` — status, terms, amount, payment method
- `balance_get_invoice(invoice_id)` — invoice line items, due date, paid/unpaid status
- `balance_list_invoices(buyer_id, status=)` — open invoices for a buyer
- `balance_get_payment(payment_id)` — payment status, failure reason

**Write-side: Balance outbox**

Balance's outbox is the system of record for buyer-facing payment communications (invoice emails, payment reminders, dunning notices). For ticket categories that are payment/invoice related, the agent should be able to push the approved draft into Balance's outbox so the message goes out through Balance's branded buyer channel — not Freshdesk — keeping payment communications consolidated where the buyer expects them.

- `balance_create_outbox_draft(buyer_id, subject, body, related_invoice_id=None)` — creates a queued draft in Balance's outbox
- `balance_send_outbox_message(outbox_id)` — send immediately (only on agent confirmation)
- `balance_list_outbox(buyer_id)` — show outbox state in the ticket sidebar

**Implementation notes:**

- New module: `app/services/balance.py` with a typed client (`httpx.AsyncClient` + Pydantic response models).
- Auth: Balance API key in Secrets Manager. Sandbox env for development.
- Get exact endpoint paths from Balance's API v2 docs and the developer portal — the schema above is the contract our code expects; adapt the client to match. Email `support@getbalance.com` for any access questions.
- All Balance write operations require explicit agent confirmation in the UI. **No silent sends.**

### 6.3 LLM — Anthropic via Pydantic AI

- Sonnet for classification (cheap, fast, structured output).
- Opus for drafting (better tone and reasoning for B2B language).
- API key in Secrets Manager.

---

## 7. AI Agents (Pydantic AI)

### 7.1 ClassifierAgent

One LLM call, no tools, structured output.

```python
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
    urgency: int  # 1–5
    sentiment: Literal["positive", "neutral", "negative"]
    entities: ExtractedEntities  # order_id, invoice_id, tracking_id, balance_buyer_id, email
    suggested_destination: Literal["freshdesk_reply", "balance_outbox"]
```

The classifier also picks the suggested destination: payment/invoice categories default to Balance outbox, everything else to Freshdesk reply. The agent can override.

### 7.2 DrafterAgent

Tool-using agent. Tools registered:

- `get_order(order_id)` — commerce backend
- `get_tracking(tracking_id)` — carrier API
- `get_customer_history(email)` — internal lookup
- `search_kb(query)` — help center / past resolved tickets
- `balance_get_buyer(...)`, `balance_get_invoice(...)`, `balance_get_transaction(...)`, `balance_list_invoices(...)`, `balance_get_payment(...)` — Balance read tools

Returns:

```python
class DraftReply(BaseModel):
    body_text: str
    confidence: float
    needs_review_reason: str | None
    destination: Literal["freshdesk_reply", "balance_outbox"]
    citations: list[Citation]
```

Per-category system prompts in `app/agents/prompts/*.md`. Keep them small and specific — one prompt per category beats one mega-prompt.

---

## 8. UI Pages

### 8.1 Inbox — `GET /`

Table of tickets, sortable/filterable. Columns: requester, subject, category (colored chip), urgency, draft status, last update, assigned agent. HTMX live-refresh every 30s.

### 8.2 Ticket detail — `GET /tickets/{id}`

Three-column layout:

- **Left:** ticket thread (inbound + our replies + private notes)
- **Center:** AI draft in an editable textarea, destination toggle (Freshdesk reply / Balance outbox), buttons:
  - **Regenerate** (HTMX → swap draft partial)
  - **Send via Freshdesk** (POST → Freshdesk reply, save SentReply)
  - **Push to Balance Outbox** (POST → Balance outbox, save SentReply)
  - **Discard**
- **Right:** context panel (HTMX-loaded):
  - Customer summary
  - Order info (if extracted)
  - **Balance buyer card:** credit limit, net terms, recent transactions, open invoices with due dates
  - Related past tickets

Every edit captured to `DraftEdit` on send.

### 8.3 Dashboard — `GET /dashboard`

Server-rendered, no charting library overhead — use simple HTML tables and small inline SVG sparklines (or `<canvas>` + Chart.js if we want polish). Sections:

**Volume & throughput**
- Tickets per day (last 14 days), broken down by category
- Median + p90 time-to-first-response
- Tickets sent via Freshdesk vs. Balance outbox

**AI quality**
- % drafts sent unedited
- Median edit-distance (chars) between draft and sent reply, by category
- Top 10 categories by edit distance (tells us which prompts to improve)
- Drafts marked "needs review" rate

**Per-agent**
- Tickets handled, replies sent, average edit distance, average handle time

**Health**
- Poller last-run time, error count last 24h
- Last LLM error
- Balance API error rate

All metrics computed from the DB on page load (no separate metrics store needed at this scale). Cache for 60s with `fastapi-cache` to keep page load snappy.

---

## 9. Deployment (AWS)

| Component | Service | Notes |
|---|---|---|
| Web app + poller | **App Runner** | Single container, autoscale 1–3 instances |
| Database | **RDS Postgres** | `db.t4g.micro`, single AZ for v1 |
| Container registry | **ECR** | Pushed via GitHub Actions |
| Secrets | **Secrets Manager** | Freshdesk key, Balance key, Anthropic key, DB URL |
| Logs | **CloudWatch Logs** | Default App Runner integration |
| Auth | HTTP Basic Auth behind App Runner *or* ALB + Cognito | Internal tool, <10 agents — basic auth is fine for v1 |
| DNS / TLS | App Runner default domain | Custom domain in week 2 |

**Deferred:** VPC + ALB + Fargate, EKS, Terraform, multi-AZ, CDN. None of these are needed at our scale and they all cost half a day each.

---

## 10. Project Layout

```
support-copilot/
  pyproject.toml
  Dockerfile
  README.md
  app/
    main.py                   # FastAPI app, lifespan starts poller
    config.py                 # pydantic-settings, loads from Secrets Manager
    db.py                     # SQLModel engine + session
    models.py                 # Ticket, Draft, DraftEdit, SentReply, ...
    routes/
      inbox.py                # GET /
      ticket.py               # GET/POST /tickets/{id}
      dashboard.py            # GET /dashboard
      htmx.py                 # partials (regenerate, context, send)
    services/
      freshdesk.py            # API client
      balance.py              # API client + outbox
      commerce.py             # order/shipping
      poller.py               # asyncio background task
    agents/
      classifier.py
      drafter.py
      tools.py                # tool definitions wrapping services
      prompts/
        shipping_status.md
        invoice_question.md
        payment_status.md
        refund_request.md
        ...
    templates/
      base.html
      inbox.html
      ticket.html
      dashboard.html
      partials/
        _draft.html
        _context.html
        _balance_card.html
        _ticket_row.html
    static/
      htmx.min.js
      style.css
```

---

## 11. Day-by-Day Plan

### Day 1 — Foundation & ingestion

- [ ] `uv init`, install deps (FastAPI, jinja2, sqlmodel, pydantic-ai, httpx, alembic)
- [ ] Project skeleton, base template, HTMX wired
- [ ] DB schema + migrations
- [ ] Freshdesk client (list, get, conversations, private note, reply)
- [ ] Balance client skeleton (read endpoints + outbox draft endpoint)
- [ ] Polling loop in lifespan task, syncs tickets + conversations into DB
- [ ] Inbox page reads from DB, no AI yet

**Goal:** tickets flow into the DB and render in a list.

### Day 2 — Intelligence

- [ ] `ClassifierAgent` with structured output, runs on every new ticket
- [ ] `DrafterAgent` with tools — start with mocked commerce tools so we're not blocked
- [ ] Wire real Balance read tools (`get_buyer`, `get_invoice`, `get_transaction`, `list_invoices`, `get_payment`)
- [ ] Per-category prompts for the top 3 categories: `shipping_status`, `invoice_question`, `payment_status`
- [ ] Ticket detail page with three-column layout, draft textarea, destination toggle
- [ ] Balance context card in the right panel
- [ ] Regenerate-draft HTMX endpoint

**Goal:** open a real ticket, see a usable AI draft and full Balance context.

### Day 3 — Close the loop, dashboard, deploy

- [ ] **Send via Freshdesk:** posts reply, saves `SentReply`, captures `DraftEdit` diff
- [ ] **Push to Balance Outbox:** creates outbox draft via Balance API, optional immediate send on confirmation, saves `SentReply` with `destination=balance_outbox`
- [ ] Dashboard page (volume, throughput, AI quality, per-agent, health)
- [ ] Wire remaining commerce/shipping tools (replace mocks)
- [ ] Dockerfile, GitHub Actions → ECR → App Runner
- [ ] RDS, Secrets Manager, basic auth
- [ ] Smoke test with real tickets

**Goal:** deployed, agents handling real tickets end-to-end through both destinations.

---

## 12. Scope Cuts (Protect the Timeline)

1. **Three categories on day 2**, not all eleven. Other categories show in the inbox without a draft and get a "no template yet" placeholder. Adding categories later is just adding prompt files.
2. **Mock commerce/shipping APIs first**, swap to real on day 3. Don't block on access.
3. **Basic auth, not Cognito.** Cognito takes half a day; basic auth takes 10 minutes.
4. **No webhooks, polling only.**
5. **Single AZ Postgres.** Multi-AZ is a one-line change later.
6. **Dashboard uses HTML tables + sparklines, not a charting framework.** If we have spare time on day 3 afternoon, swap in Chart.js.

---

## 13. Out of Scope for v1 (Future Work)

- Chat channel (Freshchat) — phase 2 once email co-pilot is stable
- Auto-send for safe categories (shipping_status with high confidence)
- Self-service deflection (suggest answers to buyers before tickets are created)
- Multi-language support
- SSO / Cognito
- Webhook-based ingestion (replace polling)
- Vector DB / embedding-based KB retrieval (start with keyword search, upgrade later)
- Fine-tuning on captured edit data

---

## 14. Open Questions

1. **Balance API access:** do we have sandbox credentials and the v2 API docs link? Need before day 2 morning.
2. **Commerce backend:** which system (Shopify, internal ERP, custom)? Determines the order/customer-history tool implementation.
3. **Carrier:** which shipping carrier for tracking lookups, or do we read tracking from the order record only?
4. **Agent identity:** how do we authenticate agents in v1 — shared basic-auth password, individual accounts in a YAML file, or something else?
5. **Outbox-vs-Freshdesk policy:** should some categories *force* the Balance outbox destination, or is the suggested destination always overridable by the agent?
