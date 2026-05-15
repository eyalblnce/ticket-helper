"""Training pipeline orchestrator.

Runs four stages in sequence, writing checkpoints after each so the pipeline
is fully resumable.  Pass the same --output directory on rerun to pick up
where a crash left off.

  Stage 1 – embed_all_resolved()          app/services/embedder.py
  Stage 2 – cluster_tickets()             app/services/clusterer.py   (Day 2)
  Stage 3 – synthesize_all()              app/services/sop_synthesizer.py (Day 3)
  Stage 4 – validate_all()               app/services/sop_validator.py   (Day 3)
  Stage 5 – write_proposals()            writes SopProposal rows to DB
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class TrainingRun:
    """Thin wrapper around a run directory for checkpoint read/write."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.state_dir = output_dir / "state"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(exist_ok=True)

    def is_done(self, stage: str) -> bool:
        return (self.state_dir / f"{stage}.done").exists()

    def mark_done(self, stage: str) -> None:
        (self.state_dir / f"{stage}.done").touch()

    def read_checkpoint(self, name: str) -> set[str]:
        f = self.state_dir / f"{name}.txt"
        return set(f.read_text().splitlines()) if f.exists() else set()

    def append_checkpoint(self, name: str, value: str) -> None:
        with open(self.state_dir / f"{name}.txt", "a") as fh:
            fh.write(value + "\n")

    def taxonomy(self) -> dict | None:
        p = self.output_dir / "taxonomy.json"
        return json.loads(p.read_text()) if p.exists() else None


def run_pipeline(
    output_dir: Path,
    since_days: int | None = None,
    force: bool = False,
) -> None:
    import shutil

    if force and output_dir.exists():
        shutil.rmtree(output_dir)
        log.info("--force: cleared %s", output_dir)

    run = TrainingRun(output_dir)
    stats: dict[str, object] = {}

    # ── Stage 1: Embed ────────────────────────────────────────────────────────
    if not run.is_done("stage1"):
        log.info("=== Stage 1: Embedding tickets ===")
        from app.services.embedder import embed_all_resolved

        matrix, index = embed_all_resolved(since_days=since_days)
        stats["embedded"] = len(index)
        run.mark_done("stage1")
        log.info("Stage 1 complete — %d tickets in embedding index.", len(index))
    else:
        from app.services.embedder import load_index

        stats["embedded"] = len(load_index())
        log.info("Stage 1: already done (%d tickets).", stats["embedded"])

    # ── Stage 2: Cluster ──────────────────────────────────────────────────────
    if not run.is_done("stage2"):
        log.info("=== Stage 2: Clustering ===")
        try:
            from app.services.clusterer import cluster_tickets

            taxonomy = cluster_tickets(run)
            (output_dir / "taxonomy.json").write_text(json.dumps(taxonomy, indent=2))
            stats["clusters"] = len(taxonomy["clusters"])
            run.mark_done("stage2")
            log.info("Stage 2 complete — %d clusters.", stats["clusters"])
        except ImportError:
            log.warning("Stage 2 not yet implemented (clusterer.py missing). Stopping here.")
            _write_partial_report(run, stats, halted_at="stage2")
            return
    else:
        taxonomy = run.taxonomy()
        stats["clusters"] = len(taxonomy["clusters"]) if taxonomy else 0
        log.info("Stage 2: already done (%d clusters).", stats["clusters"])

    # ── Stage 3: Synthesize SOPs ──────────────────────────────────────────────
    if not run.is_done("stage3"):
        log.info("=== Stage 3: Synthesizing SOPs ===")
        from app.services.sop_synthesizer import synthesize_all

        taxonomy = run.taxonomy()
        result = asyncio.run(synthesize_all(taxonomy, run))
        stats["sops_synthesized"] = result["synthesized"]
        stats["sops_skipped"] = result.get("skipped", 0)
        run.mark_done("stage3")
        log.info("Stage 3 complete — %d SOPs synthesized.", stats["sops_synthesized"])
    else:
        stats["sops_synthesized"] = len(list((output_dir / "sops").glob("*.md"))) if (output_dir / "sops").exists() else 0
        log.info("Stage 3: already done (%d SOPs).", stats["sops_synthesized"])

    # ── Stage 4: Validate ─────────────────────────────────────────────────────
    if not run.is_done("stage4"):
        log.info("=== Stage 4: Validating SOPs ===")
        from app.services.sop_validator import validate_all

        taxonomy = run.taxonomy()
        result4 = asyncio.run(validate_all(taxonomy, run))
        stats["sops_passed_validation"] = result4.get("passed", 0)
        run.mark_done("stage4")
        log.info("Stage 4 complete — %d/%d passed.", result4.get("passed", 0), result4.get("total", 0))
    else:
        log.info("Stage 4: already done.")

    # ── Stage 5: Write proposals to DB ────────────────────────────────────────
    if not run.is_done("stage5"):
        log.info("=== Stage 5: Writing proposals to DB ===")
        n = _write_proposals(run)
        stats["proposals"] = n
        run.mark_done("stage5")
        log.info("Stage 5 complete — %d proposals written.", n)
    else:
        log.info("Stage 5: already done.")

    _write_report(run, stats)
    log.info("Pipeline complete. Report: %s/REPORT.md", output_dir)


def _write_proposals(run: TrainingRun) -> int:
    """Read synthesised SOPs + scorecards and write SopProposal rows to DB."""
    from datetime import datetime

    from sqlmodel import Session

    from app.db import engine
    from app.models import SopProposal

    taxonomy = run.taxonomy()
    if not taxonomy:
        return 0

    run_id = run.output_dir.name
    sops_dir = run.output_dir / "sops"
    scorecard_file = run.output_dir / "scorecard.json"
    scorecards: dict[str, float] = {}
    if scorecard_file.exists():
        scorecards = json.loads(scorecard_file.read_text())

    proposals = []
    for cluster in taxonomy.get("clusters", []):
        sop_file = sops_dir / f"{cluster['cluster_id']}.md"
        if not sop_file.exists():
            continue
        sop_markdown = sop_file.read_text()
        proposals.append(
            SopProposal(
                run_id=run_id,
                cluster_id=cluster["cluster_id"],
                cluster_label=cluster.get("name", ""),
                proposed_category=_label_to_slug(cluster.get("name", "")),
                cluster_size=cluster.get("size", 0),
                validation_score=scorecards.get(str(cluster["cluster_id"])),
                sop_markdown=sop_markdown,
                sample_ticket_ids=cluster.get("sample_ticket_ids", []),
                status="pending",
                created_at=datetime.utcnow(),
            )
        )

    with Session(engine) as session:
        for p in proposals:
            session.add(p)
        session.commit()

    return len(proposals)


def _label_to_slug(label: str) -> str:
    """Convert 'billing.failed_payment_retry' → 'billing_failed_payment_retry'."""
    return label.replace(".", "_").replace(" ", "_").lower()[:64]


def _write_partial_report(
    run: TrainingRun, stats: dict, halted_at: str
) -> None:
    lines = [
        "# Training Run Report",
        "",
        f"**Status**: HALTED at {halted_at}",
        "",
        "## Stats so far",
        "",
    ]
    for k, v in stats.items():
        lines.append(f"- {k}: {v}")
    (run.output_dir / "REPORT.md").write_text("\n".join(lines))


def _write_report(run: TrainingRun, stats: dict) -> None:
    lines = [
        "# Training Run Report",
        "",
        "**Status**: COMPLETED",
        "",
        "## Stats",
        "",
    ]
    for k, v in stats.items():
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "## Next steps",
        "",
        "Review proposals at `/training` in the web UI.",
    ]
    (run.output_dir / "REPORT.md").write_text("\n".join(lines))
