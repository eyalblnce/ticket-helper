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
def poll(
    interval: Annotated[
        int,
        typer.Option("--interval", "-i", help="Seconds between Freshdesk sync runs"),
    ] = 90,
) -> None:
    """Run continuous Freshdesk ticket sync (same loop the web app used to embed).

    Run this as a separate process alongside the web server, e.g.:

        uv run ticket-helper poll

    Or under systemd / cron with `ticket-helper sync` for periodic one-shot pulls.
    """
    import asyncio

    from app.db import create_tables
    from app.services.poller import run_poller

    create_tables()
    typer.echo(f"Poller starting (interval={interval}s). Press Ctrl+C to stop.")
    asyncio.run(run_poller(interval_seconds=interval))


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
def download_freshchat(
    skip_messages: Annotated[bool, typer.Option("--skip-messages", help="Download conversations only, skip messages")] = False,
) -> None:
    """Download Freshchat conversations (and messages) to data/."""
    from app.config import settings
    from app.services.freshchat_downloader import (
        FC_CONVERSATIONS_FILE,
        FC_MESSAGES_FILE,
        download_fc_conversations,
        download_fc_messages,
    )

    if not settings.freshchat_token or not settings.freshchat_domain:
        typer.echo("Error: FRESHCHAT_TOKEN and FRESHCHAT_DOMAIN must be set in .env", err=True)
        raise typer.Exit(1)

    typer.echo("=== Phase 1: Conversations ===")
    result = asyncio.run(download_fc_conversations())
    typer.echo(f"Done — {result['conversations']} new conversations written to {FC_CONVERSATIONS_FILE} ({result['pages']} pages fetched).")

    if not skip_messages:
        typer.echo("\n=== Phase 2: Messages ===")
        count = asyncio.run(download_fc_messages())
        typer.echo(f"Done — {count} message batches written to {FC_MESSAGES_FILE}.")
    else:
        typer.echo("Messages skipped (--skip-messages).")


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


@app.command()
def train(
    since: Annotated[
        str | None,
        typer.Option(help="Only embed tickets updated in the last N days, e.g. '7d'"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Discard previous run output and start fresh"),
    ] = False,
) -> None:
    """Run the SOP induction pipeline on resolved tickets.

    Embeds tickets, clusters them, synthesises per-cluster SOPs, validates
    them, then writes proposals to the DB for review at /training.
    """
    from pathlib import Path

    from app.db import create_tables
    from app.services.trainer import run_pipeline

    create_tables()

    since_days: int | None = None
    if since:
        since_days = int(since.rstrip("d"))

    run_dir = Path("data") / f"training/run_{datetime.utcnow().strftime('%Y%m%d_%H%M')}"

    # Reuse the most recent existing run dir unless --force
    if not force:
        training_root = Path("data/training")
        if training_root.exists():
            runs = sorted(training_root.glob("run_*"), reverse=True)
            if runs:
                run_dir = runs[0]
                typer.echo(f"Resuming existing run: {run_dir}")

    typer.echo(f"Output: {run_dir}")
    run_pipeline(run_dir, since_days=since_days, force=force)
