"""Deterministic stratified instance selection for official SWE-bench runs.

A subset score is only defensible when the subset is chosen before any result
is known and can be reproduced exactly by a third party.  This module fixes
the sampling contract:

* Strata are the dataset's own ``repo`` values.
* Each stratum receives a proportional quota via the largest-remainder method,
  so the subset keeps the population's repository mix instead of over-weighting
  whichever repository is easiest.
* Within a stratum, candidates are sorted by ``instance_id`` and drawn with a
  seeded ``random.Random``, so the draw depends on nothing but the seed and the
  dataset contents.
* A calibration subset (used for the engine matrix) is drawn from the selected
  instances by the same rule, so matrix cells are reusable rows of the main
  score rather than a separate, incomparable sample.

The emitted manifest carries a digest over the dataset identity, seed and the
resulting ID list.  Re-running this module must reproduce the digest; if it
does not, the selection was not what the report claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _largest_remainder(counts: dict[str, int], total: int) -> dict[str, int]:
    """Allocate ``total`` slots across strata proportionally to ``counts``."""

    population = sum(counts.values())
    if population == 0 or total <= 0:
        return {key: 0 for key in counts}
    exact = {key: value * total / population for key, value in counts.items()}
    quota = {key: int(value) for key, value in exact.items()}
    shortfall = total - sum(quota.values())
    # Ties are broken by stratum name so the allocation is order-independent.
    ranked = sorted(
        counts, key=lambda key: (-(exact[key] - quota[key]), key)
    )
    for key in ranked[:shortfall]:
        quota[key] += 1
    return quota


def select(
    instances: list[dict[str, Any]],
    size: int,
    seed: int,
    calibration_size: int = 0,
) -> dict[str, Any]:
    by_repo: dict[str, list[str]] = defaultdict(list)
    for row in instances:
        by_repo[row["repo"]].append(row["instance_id"])
    counts = {repo: len(ids) for repo, ids in by_repo.items()}
    if size > sum(counts.values()):
        raise ValueError(f"requested {size} instances, dataset has {sum(counts.values())}")
    quota = _largest_remainder(counts, size)

    chosen: list[str] = []
    for repo in sorted(by_repo):
        take = quota[repo]
        if take <= 0:
            continue
        candidates = sorted(by_repo[repo])
        rng = random.Random(f"{seed}:{repo}")
        chosen.extend(rng.sample(candidates, take))
    chosen.sort()

    calibration: list[str] = []
    if calibration_size > 0:
        if calibration_size > len(chosen):
            raise ValueError("calibration subset cannot exceed the selection")
        repo_of = {row["instance_id"]: row["repo"] for row in instances}
        selected_counts = Counter(repo_of[i] for i in chosen)
        cal_quota = _largest_remainder(dict(selected_counts), calibration_size)
        for repo in sorted(selected_counts):
            take = cal_quota[repo]
            if take <= 0:
                continue
            pool = sorted(i for i in chosen if repo_of[i] == repo)
            rng = random.Random(f"{seed}:calibration:{repo}")
            calibration.extend(rng.sample(pool, take))
        calibration.sort()

    return {"instance_ids": chosen, "calibration_ids": calibration}


def digest(dataset: str, split: str, seed: int, instance_ids: list[str]) -> str:
    payload = json.dumps(
        {"dataset": dataset, "split": split, "seed": seed, "instance_ids": instance_ids},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
    parser.add_argument("--split", default="test")
    parser.add_argument("--size", type=int, default=30)
    parser.add_argument("--calibration-size", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260729)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    from datasets import load_dataset

    data = load_dataset(args.dataset, split=args.split)
    instances = [{"instance_id": r["instance_id"], "repo": r["repo"]} for r in data]
    picked = select(instances, args.size, args.seed, args.calibration_size)

    repo_of = {r["instance_id"]: r["repo"] for r in instances}
    manifest = {
        "schema_version": 1,
        "dataset": args.dataset,
        "split": args.split,
        "population": len(instances),
        "seed": args.seed,
        "selection_rule": (
            "repo-stratified proportional allocation (largest remainder); "
            "within-stratum seeded random.Random(f'{seed}:{repo}').sample over "
            "instance_id-sorted candidates"
        ),
        "size": len(picked["instance_ids"]),
        "instance_ids": picked["instance_ids"],
        "calibration_ids": picked["calibration_ids"],
        "population_repo_counts": dict(sorted(Counter(r["repo"] for r in instances).items())),
        "selected_repo_counts": dict(
            sorted(Counter(repo_of[i] for i in picked["instance_ids"]).items())
        ),
        "digest": digest(args.dataset, args.split, args.seed, picked["instance_ids"]),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
