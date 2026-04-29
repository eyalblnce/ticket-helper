"""ticket-helper CLI — entry point for all commands."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Annotated

import typer

app = typer.Typer(help="Support Co-Pilot management commands.")


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
    """Download the latest tickets from Freshdesk into the local database."""
    from app.db import create_tables
    from app.services.poller import sync_once

    create_tables()
    updated_since = datetime.utcnow() - timedelta(days=days)
    typer.echo(f"Syncing tickets updated since {updated_since.date()} …")
    count = asyncio.run(sync_once(updated_since=updated_since))
    typer.echo(f"Done — {count} ticket(s) synced.")


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
