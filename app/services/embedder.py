"""Stage 1: embed resolved tickets using bge-small-en-v1.5 (local CPU).

Embeddings are stored in data/embeddings.npy with a companion index at
data/embeddings_index.json ({ticket_id: row}).  Reruns skip already-embedded
tickets so incremental daily runs are fast.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.models import Conversation, Ticket

from app.services.ticket_thread import first_ticket_description

log = logging.getLogger(__name__)

DATA_DIR = Path("data")
EMBEDDINGS_FILE = DATA_DIR / "embeddings.npy"
EMBEDDINGS_INDEX_FILE = DATA_DIR / "embeddings_index.json"

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
MAX_BODY_CHARS = 800
BATCH_SIZE = 256


def _ticket_text(ticket: Ticket, conversations: list[Conversation]) -> str:
    """Build the text to embed: subject + best available body."""
    body = ""

    desc_row = first_ticket_description(conversations)
    if desc_row:
        body = desc_row.body_text[:MAX_BODY_CHARS]
    else:
        inbound = next((c for c in conversations if c.direction == "inbound"), None)
        if inbound:
            body = inbound.body_text[:MAX_BODY_CHARS]

    if not body:
        body = (ticket.raw_payload or {}).get("description_text", "")[:MAX_BODY_CHARS]

    return f"{ticket.subject}\n{body}".strip()


def load_index() -> dict[int, int]:
    if EMBEDDINGS_INDEX_FILE.exists():
        return {int(k): v for k, v in json.loads(EMBEDDINGS_INDEX_FILE.read_text()).items()}
    return {}


def load_matrix() -> np.ndarray | None:
    if EMBEDDINGS_FILE.exists():
        return np.load(str(EMBEDDINGS_FILE))
    return None


def _save(matrix: np.ndarray, index: dict[int, int]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    np.save(str(EMBEDDINGS_FILE), matrix)
    EMBEDDINGS_INDEX_FILE.write_text(json.dumps(index))
    log.debug("Saved %d embeddings to %s", len(index), EMBEDDINGS_FILE)


def embed_all_resolved(
    since_days: int | None = None,
) -> tuple[np.ndarray, dict[int, int]]:
    """Embed all resolved/closed tickets, skipping already-embedded ones.

    Returns (matrix, index) where index maps ticket.id → row in matrix.
    """
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Conversation, Ticket

    index = load_index()
    existing_matrix = load_matrix()
    already_done: set[int] = set(index.keys())

    with Session(engine) as session:
        query = select(Ticket).where(Ticket.status.in_([4, 5]))
        tickets = session.exec(query).all()

        if since_days is not None:
            cutoff = datetime.utcnow() - timedelta(days=since_days)
            tickets = [
                t for t in tickets
                if t.freshdesk_updated_at and t.freshdesk_updated_at >= cutoff
            ]

        todo = [t for t in tickets if t.id not in already_done]

        if not todo:
            log.info(
                "All eligible tickets already embedded (%d total).", len(already_done)
            )
            if existing_matrix is None:
                raise RuntimeError("Index is non-empty but embeddings.npy is missing.")
            return existing_matrix, index

        log.info(
            "Embedding %d new tickets (%d already cached).", len(todo), len(already_done)
        )

        ticket_ids = [t.id for t in todo]
        raw_convs = session.exec(
            select(Conversation).where(Conversation.ticket_id.in_(ticket_ids))
        ).all()

    convs_by_ticket: dict[int, list[Conversation]] = {}
    for c in raw_convs:
        convs_by_ticket.setdefault(c.ticket_id, []).append(c)

    texts = [_ticket_text(t, convs_by_ticket.get(t.id, [])) for t in todo]

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBED_MODEL)
    chunks: list[np.ndarray] = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        vecs = model.encode(
            batch,
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=64,
        )
        chunks.append(vecs.astype(np.float32))
        done = min(i + BATCH_SIZE, len(texts))
        log.info("  Embedded %d / %d", done, len(texts))

    new_matrix = np.vstack(chunks)

    start_row = len(index)
    full_matrix = (
        np.vstack([existing_matrix, new_matrix])
        if existing_matrix is not None
        else new_matrix
    )

    for i, ticket in enumerate(todo):
        index[ticket.id] = start_row + i

    _save(full_matrix, index)
    log.info("Embedding complete — %d total tickets in index.", len(index))
    return full_matrix, index
