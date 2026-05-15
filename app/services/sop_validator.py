"""Stage 4: validate each SOP against its holdout tickets using Claude Haiku.

For each synthesised SOP, apply it to 20% holdout tickets and ask Haiku
whether the SOP describes the ticket.  Score = fraction of YES answers.
Results are written to run_dir/scorecard.json incrementally.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

PASS_THRESHOLD = 0.70


async def validate_all(taxonomy: dict, run) -> dict:
    """Validate all SOPs that have holdout files. Returns scorecard summary."""
    from app.services.trainer import TrainingRun
    assert isinstance(run, TrainingRun)

    sops_dir = run.output_dir / "sops"
    holdouts_dir = run.output_dir / "holdouts"
    scorecard_file = run.output_dir / "scorecard.json"

    done: set[str] = run.read_checkpoint("stage4_done")
    scorecard: dict[str, float] = {}
    if scorecard_file.exists():
        scorecard = json.loads(scorecard_file.read_text())

    sop_files = list(sops_dir.glob("*.md"))
    todo = [f for f in sop_files if f.stem not in done]

    log.info("Stage 4: validating %d SOPs (%d already done).", len(todo), len(done))

    sem = asyncio.Semaphore(4)
    tasks = [_validate_one(f, holdouts_dir, scorecard, scorecard_file, run, sem) for f in todo]
    await asyncio.gather(*tasks, return_exceptions=True)

    passed = sum(1 for v in scorecard.values() if v >= PASS_THRESHOLD)
    log.info(
        "Stage 4 done: %d/%d SOPs passed (threshold %.0f%%).",
        passed, len(scorecard), PASS_THRESHOLD * 100,
    )
    return {"total": len(scorecard), "passed": passed}


async def _validate_one(
    sop_file: Path,
    holdouts_dir: Path,
    scorecard: dict,
    scorecard_file: Path,
    run,
    sem: asyncio.Semaphore,
) -> None:
    cid_str = sop_file.stem
    holdout_file = holdouts_dir / f"{cid_str}.json"
    if not holdout_file.exists():
        return

    holdout = json.loads(holdout_file.read_text())
    ticket_ids = holdout.get("ticket_ids", [])
    if not ticket_ids:
        return

    sop_text = sop_file.read_text()
    cluster_name = holdout.get("cluster_name", "")

    tickets_text = await asyncio.to_thread(_fetch_ticket_texts, ticket_ids)

    async with sem:
        scores = await asyncio.gather(*[
            _score_ticket(sop_text, cluster_name, ticket_text)
            for ticket_text in tickets_text
        ], return_exceptions=True)

    valid_scores = [s for s in scores if isinstance(s, bool)]
    if not valid_scores:
        return

    accuracy = sum(valid_scores) / len(valid_scores)
    scorecard[cid_str] = accuracy

    # Write scorecard incrementally
    scorecard_file.write_text(json.dumps(scorecard, indent=2))
    run.append_checkpoint("stage4_done", cid_str)

    flag = "PASS" if accuracy >= PASS_THRESHOLD else "FAIL"
    log.info(
        "Cluster %s (%s): %.0f%% accuracy on %d holdouts [%s]",
        cid_str, cluster_name, accuracy * 100, len(valid_scores), flag,
    )


def _fetch_ticket_texts(ticket_ids: list[int]) -> list[str]:
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Conversation, Ticket
    from app.services.ticket_thread import first_ticket_description

    with Session(engine) as session:
        tickets = session.exec(select(Ticket).where(Ticket.id.in_(ticket_ids))).all()
        convs = session.exec(
            select(Conversation).where(Conversation.ticket_id.in_([t.id for t in tickets]))
        ).all()

    convs_by_ticket: dict[int, list] = {}
    for c in convs:
        convs_by_ticket.setdefault(c.ticket_id, []).append(c)

    texts = []
    for ticket in tickets:
        subject = ticket.subject or ""
        convs_t = convs_by_ticket.get(ticket.id, [])
        body_conv = first_ticket_description(convs_t)
        if not body_conv:
            body_conv = next((c for c in convs_t if c.direction == "inbound"), None)
        body = body_conv.body_text[:300] if body_conv else (ticket.raw_payload or {}).get("description_text", "")[:300]
        texts.append(f"Subject: {subject}\n{body}")

    return texts


async def _score_ticket(sop_text: str, cluster_name: str, ticket_text: str) -> bool:
    import anthropic

    prompt = f"""SOP category: {cluster_name}

SOP (abbreviated):
{sop_text[:600]}

---
Ticket:
{ticket_text}

Does this SOP apply to this ticket? Answer YES or NO only."""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = await asyncio.to_thread(
        client.messages.create,
        model="claude-haiku-4-5-20251001",
        max_tokens=5,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip().upper()
    return answer.startswith("YES")
