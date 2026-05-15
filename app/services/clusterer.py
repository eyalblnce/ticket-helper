"""Stage 2: cluster ticket embeddings with HDBSCAN, then auto-label each
cluster using Claude Haiku.

Output:
  run_dir/taxonomy.json   — cluster list with labels, sizes, member ticket IDs
  run_dir/umap.png        — 2-D scatter plot for morning review
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import numpy as np

from app.config import settings
from app.services.embedder import load_index, load_matrix
from app.services.trainer import TrainingRun

log = logging.getLogger(__name__)

EXISTING_CATEGORIES = [
    "shipping_status", "invoice_question", "payment_status", "payment_failed",
    "credit_limit_question", "refund_request", "return_request",
    "damaged_or_wrong_item", "product_question", "account_access",
]


def cluster_tickets(run: TrainingRun) -> dict:
    """Main entry point — returns taxonomy dict and writes umap.png."""
    matrix = load_matrix()
    index = load_index()

    if matrix is None or not index:
        raise RuntimeError("No embeddings found. Run `ticket-helper train` first.")

    row_to_ticket: dict[int, int] = {v: k for k, v in index.items()}

    # ── UMAP dimensionality reduction (384 → 50 dims) ────────────────────────
    # HDBSCAN degrades to O(n²) on raw 384-dim embeddings; reducing first is
    # the standard workflow and cuts clustering time from minutes to seconds.
    import umap

    log.info("UMAP: reducing %d × %d → 50 dims for clustering …", *matrix.shape)
    reducer_50 = umap.UMAP(
        n_components=50,
        n_neighbors=30,
        min_dist=0.0,
        random_state=42,
        low_memory=True,
    )
    matrix_50 = reducer_50.fit_transform(matrix).astype(np.float32)
    log.info("UMAP reduction done.")

    # ── HDBSCAN on 50-dim representations ────────────────────────────────────
    log.info("Running HDBSCAN on %d × 50 matrix …", len(matrix_50))
    import hdbscan as hdbscan_lib

    clusterer = hdbscan_lib.HDBSCAN(
        min_cluster_size=settings.training_min_cluster_size,
        min_samples=5,
        metric="euclidean",
        core_dist_n_jobs=-1,
    )
    labels: np.ndarray = clusterer.fit_predict(matrix_50)

    n_clusters = int(np.unique(labels[labels >= 0]).size)
    n_noise = int((labels == -1).sum())
    log.info("HDBSCAN: %d clusters, %d noise points.", n_clusters, n_noise)

    # Group ticket IDs by cluster
    clusters_raw: dict[int, list[int]] = {}
    for row_idx, label in enumerate(labels):
        if label < 0:
            continue
        tid = row_to_ticket[row_idx]
        clusters_raw.setdefault(int(label), []).append(tid)

    # Pick up to 10 representative tickets per cluster (closest to centroid)
    cluster_samples: dict[int, list[int]] = {}
    for cid, tids in clusters_raw.items():
        rows = np.array([index[tid] for tid in tids])
        vecs = matrix[rows]
        centroid = vecs.mean(axis=0)
        dists = np.linalg.norm(vecs - centroid, axis=1)
        top = min(10, len(tids))
        closest = dists.argsort()[:top].tolist()
        cluster_samples[cid] = [tids[i] for i in closest]

    # ── Haiku labeling ────────────────────────────────────────────────────────
    if settings.anthropic_api_key:
        log.info("Labeling %d clusters with Haiku …", n_clusters)
        label_map = asyncio.run(_label_all(cluster_samples))
    else:
        log.warning("No ANTHROPIC_API_KEY — skipping cluster labeling.")
        label_map = {}

    # ── UMAP visualization (reuse 50-dim reduction, project to 2D) ───────────
    _generate_umap(matrix_50, labels, run.output_dir)

    # ── Build taxonomy ────────────────────────────────────────────────────────
    clusters_out = []
    for cid, tids in sorted(clusters_raw.items(), key=lambda x: -len(x[1])):
        info = label_map.get(cid, {})
        clusters_out.append({
            "cluster_id": cid,
            "name": info.get("label", f"cluster_{cid}"),
            "description": info.get("description", ""),
            "size": len(tids),
            "member_ticket_ids": tids,
            "sample_ticket_ids": cluster_samples.get(cid, [])[:5],
            "maps_to_existing": info.get("maps_to_existing"),
            "labeler_confidence": info.get("confidence", 0.0),
        })

    return {
        "clusters": clusters_out,
        "noise_bucket_size": n_noise,
        "total_tickets_clustered": int((labels >= 0).sum()),
    }


# ── Haiku labeling ─────────────────────────────────────────────────────────────

async def _label_all(cluster_samples: dict[int, list[int]]) -> dict[int, dict]:
    """Label all clusters concurrently (max 8 in flight)."""
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Ticket

    all_ids = [tid for tids in cluster_samples.values() for tid in tids]
    with Session(engine) as session:
        tickets = session.exec(select(Ticket).where(Ticket.id.in_(all_ids))).all()
    ticket_map = {t.id: t for t in tickets}

    sem = asyncio.Semaphore(8)
    tasks = [
        _label_one(cid, sample_tids, ticket_map, sem)
        for cid, sample_tids in cluster_samples.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[int, dict] = {}
    for r in results:
        if isinstance(r, Exception):
            log.warning("Labeling error: %s", r)
        else:
            cid, info = r
            out[cid] = info
    return out


async def _label_one(
    cid: int,
    sample_tids: list[int],
    ticket_map: dict,
    sem: asyncio.Semaphore,
) -> tuple[int, dict]:
    import anthropic

    subjects = [
        ticket_map[tid].subject
        for tid in sample_tids
        if tid in ticket_map and ticket_map[tid].subject
    ]
    subjects_block = "\n".join(f"- {s}" for s in subjects[:10])

    prompt = f"""You are labeling a cluster of B2B fintech support tickets.

Representative ticket subjects:
{subjects_block}

Known categories (use one if this cluster clearly belongs there):
{", ".join(EXISTING_CATEGORIES)}

Respond with JSON only — no prose, no markdown fences:
{{
  "label": "dot.separated.slug",
  "description": "One sentence describing this cluster.",
  "maps_to_existing": "existing_category_name or null",
  "confidence": 0.0
}}"""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    async with sem:
        response = await asyncio.to_thread(
            client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )

    text = response.content[0].text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return cid, json.loads(m.group())
        except json.JSONDecodeError:
            pass

    log.warning("Could not parse label for cluster %d: %s", cid, text[:120])
    return cid, {
        "label": f"cluster_{cid}",
        "description": "",
        "maps_to_existing": None,
        "confidence": 0.0,
    }


# ── UMAP visualization ─────────────────────────────────────────────────────────

def _generate_umap(
    matrix: np.ndarray,
    labels: np.ndarray,
    output_dir: Path,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import umap

        n_sample = min(5000, len(matrix))
        idx = np.random.default_rng(42).choice(len(matrix), n_sample, replace=False)
        sampled = matrix[idx]
        sampled_labels = labels[idx]

        log.info("Fitting UMAP on %d sample points …", n_sample)
        reducer = umap.UMAP(n_components=2, random_state=42, low_memory=True)
        reduced = reducer.fit_transform(sampled)

        fig, ax = plt.subplots(figsize=(12, 9))
        sc = ax.scatter(
            reduced[:, 0], reduced[:, 1],
            c=sampled_labels,
            cmap="tab20",
            s=2,
            alpha=0.5,
        )
        plt.colorbar(sc, ax=ax, label="cluster_id  (−1 = noise)")
        ax.set_title(f"Ticket embedding clusters  (UMAP, n={n_sample:,} sample)")
        ax.set_xlabel("UMAP-1")
        ax.set_ylabel("UMAP-2")
        plt.tight_layout()
        out = output_dir / "umap.png"
        plt.savefig(out, dpi=150, bbox_inches="tight")
        plt.close()
        log.info("UMAP saved → %s", out)
    except Exception as exc:
        log.warning("UMAP visualization failed (non-fatal): %s", exc)
