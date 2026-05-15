"""Stage 3: synthesise one SOP per qualifying cluster using Claude Sonnet.

Per-cluster checkpointing means a crashed run resumes from the first
incomplete cluster.  Budget is tracked in state/budget.json; synthesis
halts (non-fatally) if the limit would be exceeded.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.config import settings
from app.services.ticket_thread import first_ticket_description

log = logging.getLogger(__name__)

MIN_LABELER_CONFIDENCE = 0.6

_SYSTEM_PROMPT = """\
You are writing a Standard Operating Procedure (SOP) for a B2B fintech support team at
Balance (getbalance.com), which provides net-terms / BNPL payments between merchants and buyers.

Given a cluster of similar resolved support tickets, write a concise, actionable SOP
that a support agent can follow the next time a similar ticket arrives.

Respond in markdown with exactly these sections (use ## headings):
## Problem signature
## Required information
## Resolution steps
## Escalation triggers
## Edge cases observed
## Example phrasings

Keep each section brief (3–7 bullet points). Total length 300–500 words.
Do NOT include any introduction or conclusion outside the sections."""


async def synthesize_all(taxonomy: dict, run) -> dict:
    """Synthesise SOPs for all qualifying clusters. Returns stats dict."""
    from app.services.trainer import TrainingRun
    assert isinstance(run, TrainingRun)

    clusters = taxonomy.get("clusters", [])
    done: set[str] = run.read_checkpoint("stage3_done")

    qualifying = [
        c for c in clusters
        if c["size"] >= settings.training_min_cluster_size_for_sop
        and c.get("labeler_confidence", 0.0) >= MIN_LABELER_CONFIDENCE
        and str(c["cluster_id"]) not in done
    ]

    skipped_existing = sum(
        1 for c in clusters
        if c.get("maps_to_existing") and c.get("maps_to_existing") != "other"
        and c["size"] >= settings.training_min_cluster_size_for_sop
        and c.get("labeler_confidence", 0.0) >= MIN_LABELER_CONFIDENCE
    )

    log.info(
        "Stage 3: %d clusters to synthesise (%d already done, %d below threshold/conf).",
        len(qualifying), len(done),
        len(clusters) - len(qualifying) - len(done),
    )

    sops_dir = run.output_dir / "sops"
    holdouts_dir = run.output_dir / "holdouts"
    sops_dir.mkdir(exist_ok=True)
    holdouts_dir.mkdir(exist_ok=True)

    budget = _load_budget(run)
    sem = asyncio.Semaphore(settings.training_synthesis_concurrency)

    tasks = [
        _synthesize_one(cluster, run, sops_dir, holdouts_dir, budget, sem)
        for cluster in qualifying
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    synthesized = sum(1 for r in results if r == "ok")
    budget_halted = sum(1 for r in results if r == "budget")
    failed = sum(1 for r in results if r not in ("ok", "budget") or isinstance(r, Exception))

    _save_budget(run, budget)
    log.info(
        "Stage 3 done: %d synthesised, %d budget-deferred, %d failed. Spent: $%.3f",
        synthesized, budget_halted, failed, budget["spent"],
    )
    return {"synthesized": synthesized, "skipped": budget_halted + failed}


async def _synthesize_one(
    cluster: dict,
    run,
    sops_dir: Path,
    holdouts_dir: Path,
    budget: dict,
    sem: asyncio.Semaphore,
) -> str:
    cid = cluster["cluster_id"]
    member_ids: list[int] = cluster["member_ticket_ids"]

    n_holdout = max(1, int(len(member_ids) * settings.training_holdout_fraction))
    holdout_ids = member_ids[:n_holdout]
    train_ids = member_ids[n_holdout: n_holdout + settings.training_max_tickets_per_cluster]

    (holdouts_dir / f"{cid}.json").write_text(json.dumps({
        "cluster_id": cid,
        "cluster_name": cluster["name"],
        "ticket_ids": holdout_ids,
    }))

    async with sem:
        estimated = _estimate_cost(len(train_ids))
        if budget["spent"] + estimated > budget["limit"]:
            log.warning(
                "Cluster %d: budget $%.2f/$%.2f — deferring.",
                cid, budget["spent"], budget["limit"],
            )
            return "budget"

        examples = await asyncio.to_thread(_fetch_examples, train_ids)

        sop_text, tokens = await _call_sonnet(
            cluster["name"], cluster.get("description", ""), examples
        )

        if not _has_required_sections(sop_text):
            log.warning("Cluster %d: missing sections — retrying.", cid)
            sop_text, tokens2 = await _call_sonnet(
                cluster["name"], cluster.get("description", ""), examples, retry=True
            )
            tokens += tokens2
            if not _has_required_sections(sop_text):
                log.error("Cluster %d: synthesis failed after retry.", cid)
                budget["spent"] += _tokens_to_usd(tokens)
                run.append_checkpoint("stage3_failed", str(cid))
                return "failed"

        full_sop = (
            f"# SOP: {cluster['name']}\n\n"
            f"_{cluster.get('description', '')}_\n\n"
            f"{sop_text}\n\n"
            f"---\n*Cluster {cid} · {cluster['size']} tickets · "
            f"conf {cluster.get('labeler_confidence', 0):.2f}*\n"
        )
        (sops_dir / f"{cid}.md").write_text(full_sop)

        budget["spent"] += _tokens_to_usd(tokens)
        run.append_checkpoint("stage3_done", str(cid))
        log.info(
            "Cluster %d (%s): SOP written. Spent $%.3f total.",
            cid, cluster["name"], budget["spent"],
        )
        return "ok"


def _fetch_examples(train_ids: list[int]) -> str:
    """Fetch ticket subjects + bodies for the synthesis prompt."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Conversation, Ticket

    with Session(engine) as session:
        tickets = session.exec(select(Ticket).where(Ticket.id.in_(train_ids))).all()
        convs = session.exec(
            select(Conversation).where(Conversation.ticket_id.in_([t.id for t in tickets]))
        ).all()

    convs_by_ticket: dict[int, list] = {}
    for c in convs:
        convs_by_ticket.setdefault(c.ticket_id, []).append(c)

    parts = []
    for ticket in tickets:
        subject = ticket.subject or "(no subject)"
        body = _best_body(ticket, convs_by_ticket.get(ticket.id, []))
        parts.append(f"**Subject:** {subject}\n**Message:** {body or '(no body)'}")

    return "\n\n---\n\n".join(parts)


def _best_body(ticket, convs: list) -> str:
    desc_row = first_ticket_description(convs)
    if desc_row:
        return desc_row.body_text[:400]
    inbound = next((c for c in convs if c.direction == "inbound"), None)
    if inbound:
        return inbound.body_text[:400]
    return (ticket.raw_payload or {}).get("description_text", "")[:400]


async def _call_sonnet(
    name: str, description: str, examples: str, retry: bool = False
) -> tuple[str, int]:
    import anthropic

    retry_note = (
        "\n\nPrevious attempt was missing required sections. "
        "Include ALL of: Problem signature, Required information, Resolution steps, "
        "Escalation triggers, Edge cases observed, Example phrasings."
        if retry else ""
    )
    user_msg = (
        f"Category: {name}\nDescription: {description}\n\n"
        f"Representative tickets:\n\n{examples}"
        f"{retry_note}\n\nWrite the SOP:"
    )

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = await asyncio.to_thread(
        client.messages.create,
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    tokens = response.usage.input_tokens + response.usage.output_tokens
    return response.content[0].text, tokens


def _has_required_sections(text: str) -> bool:
    required = ["resolution steps", "escalation triggers"]
    lower = text.lower()
    return all(s in lower for s in required)


def _estimate_cost(n_tickets: int) -> float:
    input_t = n_tickets * 550 + 800
    output_t = 700
    return input_t / 1_000_000 * 3.0 + output_t / 1_000_000 * 15.0


def _tokens_to_usd(tokens: int) -> float:
    return (tokens * 0.7 / 1_000_000 * 3.0) + (tokens * 0.3 / 1_000_000 * 15.0)


def _load_budget(run) -> dict:
    f = run.state_dir / "budget.json"
    return json.loads(f.read_text()) if f.exists() else {
        "spent": 0.0, "limit": settings.training_budget_usd
    }


def _save_budget(run, budget: dict) -> None:
    (run.state_dir / "budget.json").write_text(json.dumps(budget, indent=2))
