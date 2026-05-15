"""Desk SOP shortlist ranking, rationale strings, and steps extraction from markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlmodel import Session, select

from app.models import SopProposal

# Section titles (case-insensitive) for the “steps” beat
_STEPS_SECTION_PATTERN = re.compile(
    r"^##\s+(steps|checklist|procedure|handling|resolution)\b",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class RankedSopItem:
    proposal: SopProposal
    rank_index: int
    rationale: str
    score_label: str


def extract_steps_markdown(sop_markdown: str) -> str | None:
    """Return markdown inside the first matching ## Steps / Checklist / … section, or None."""
    if not sop_markdown or not sop_markdown.strip():
        return None
    m = _STEPS_SECTION_PATTERN.search(sop_markdown)
    if not m:
        return None
    start = m.end()
    rest = sop_markdown[start:]
    next_header = re.search(r"^##\s+", rest, re.MULTILINE)
    chunk = rest[: next_header.start()] if next_header else rest
    chunk = chunk.strip()
    return chunk or None


def _score_tuple(p: SopProposal) -> tuple:
    vs = p.validation_score
    if vs is None:
        vs_key = -1.0
    else:
        vs_key = float(vs)
    return (-vs_key, -p.cluster_size, -(p.id or 0))


def rank_sops_for_category(
    session: Session,
    category: str,
    *,
    max_items: int = 5,
) -> list[RankedSopItem]:
    """Approved SOPs with proposed_category == category, sorted for explainable shortlist."""
    rows = list(
        session.exec(
            select(SopProposal)
            .where(SopProposal.status == "approved")
            .where(SopProposal.proposed_category == category)
        ).all()
    )
    rows.sort(key=_score_tuple)
    out: list[RankedSopItem] = []
    for i, p in enumerate(rows[:max_items]):
        vs = p.validation_score
        score_label = f"validation {vs:.0%}" if vs is not None else "validation —"
        if i == 0:
            rationale = (
                f"Top pick: highest validation score among approved SOPs for category “{category}”."
            )
        else:
            rationale = "Same category; lower rank by validation score and cluster size than the primary."
        out.append(RankedSopItem(proposal=p, rank_index=i, rationale=rationale, score_label=score_label))
    return out


def pick_selected_proposal(
    ranked: list[RankedSopItem],
    sop_proposal_id: int | None,
) -> SopProposal | None:
    if not ranked:
        return None
    if sop_proposal_id is None:
        return ranked[0].proposal
    for item in ranked:
        if item.proposal.id == sop_proposal_id:
            return item.proposal
    return ranked[0].proposal
