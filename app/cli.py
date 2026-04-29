"""ticket-helper CLI — entry point for all commands."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Annotated

import typer

app = typer.Typer(help="Support Co-Pilot management commands.")


@app.callback()
def _setup(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show DEBUG logs")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command()
def web(
    host: Annotated[str, typer.Option(help="Bind host")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Bind port")] = 8000,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes")] = False,
) -> None:
    """Start the FastAPI web server."""
    import uvicorn
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


@app.command()
def sync(
    days: Annotated[int, typer.Option(help="Fetch tickets updated in the last N days")] = 1,
) -> None:
    """Download the latest tickets from Freshdesk and load conversations from data/conversations.jsonl."""
    from app.db import create_tables
    from app.services.classify_task import CONVERSATIONS_JSONL, load_conversations_from_jsonl
    from app.services.poller import TICKETS_JSONL, load_tickets_from_jsonl, sync_once

    create_tables()

    updated_since = datetime.utcnow() - timedelta(days=days)
    typer.echo(f"Syncing tickets updated since {updated_since.date()} from Freshdesk …")
    ticket_count = asyncio.run(sync_once(updated_since=updated_since))
    typer.echo(f"  {ticket_count} ticket(s) synced.")

    typer.echo(f"Loading tickets from {TICKETS_JSONL} …")
    t_stats = load_tickets_from_jsonl()
    typer.echo(f"  {t_stats['loaded']} new, {t_stats['updated']} updated.")

    typer.echo(f"Loading conversations from {CONVERSATIONS_JSONL} …")
    c_stats = load_conversations_from_jsonl()
    typer.echo(f"  {c_stats['loaded']} loaded, {c_stats['skipped']} already in DB, {c_stats['missing_ticket']} skipped (ticket not in DB).")

    from sqlmodel import Session, func, select
    from app.db import engine
    from app.models import Conversation, Ticket
    with Session(engine) as session:
        total_tickets = session.exec(select(func.count()).select_from(Ticket)).one()
        total_convs = session.exec(select(func.count()).select_from(Conversation)).one()
    typer.echo(f"\nDB totals: {total_tickets} tickets, {total_convs} conversations.")


@app.command()
def download(
    months: Annotated[int | None, typer.Option(help="Limit to N most recent months")] = None,
    skip_conversations: Annotated[bool, typer.Option("--skip-conversations", help="Download tickets only")] = False,
) -> None:
    """Download historical tickets (and optionally conversations) from Freshdesk to data/."""
    from app.services.downloader import (
        CONVERSATIONS_FILE,
        TICKETS_FILE,
        download_conversations,
        download_tickets,
    )

    typer.echo("=== Phase 1: Tickets ===")
    t = asyncio.run(download_tickets(max_months=months))
    typer.echo(f"Done — {t['tickets']} new tickets fetched, {t['months_skipped']} month(s) already complete.")

    if not skip_conversations:
        typer.echo("\n=== Phase 2: Conversations ===")
        count = asyncio.run(download_conversations())
        typer.echo(f"Done — {count} conversation batches written to {CONVERSATIONS_FILE}")
    else:
        typer.echo("Conversations skipped (--skip-conversations).")


@app.command()
def classify(
    force: Annotated[bool, typer.Option("--force", help="Re-classify already-classified tickets")] = False,
) -> None:
    """Classify (or re-classify) tickets using rules and the LLM."""
    from app.db import create_tables
    from app.services.classify_task import classify_all_unclassified

    create_tables()
    count = asyncio.run(classify_all_unclassified(force=force))
    typer.echo(f"Done — {count} ticket(s) classified.")
