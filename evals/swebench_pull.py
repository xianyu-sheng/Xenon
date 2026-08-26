"""Pre-pull official SWE-bench instance images listed in a selection manifest.

The eval harness can pull on demand, but doing it inside the timed run mixes
image-transfer time into engine latency and serialises a network-bound step.
This module resolves image keys through the official ``make_test_spec`` API and
pulls them with bounded concurrency, reporting per-image outcome and the disk
cost so a run can be aborted before it fills the volume.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


def image_keys(
    instance_ids: list[str], dataset: str, split: str, namespace: str = "swebench"
) -> dict[str, str]:
    from datasets import load_dataset
    from swebench.harness.test_spec.test_spec import make_test_spec

    wanted = set(instance_ids)
    data = load_dataset(dataset, split=split)
    keys: dict[str, str] = {}
    for row in data:
        if row["instance_id"] in wanted:
            keys[row["instance_id"]] = make_test_spec(
                dict(row), namespace=namespace
            ).instance_image_key
    missing = wanted - keys.keys()
    if missing:
        raise SystemExit(f"unknown instance(s): {sorted(missing)}")
    return keys


def _free_gb(path: str = "/") -> float:
    return shutil.disk_usage(path).free / 1024**3


def _pull(key: str) -> tuple[str, bool, str]:
    proc = subprocess.run(
        ["docker", "pull", "--quiet", key],
        capture_output=True,
        text=True,
    )
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return key, proc.returncode == 0, (detail[-1] if detail else "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
    parser.add_argument("--split", default="test")
    parser.add_argument("--namespace", default="swebench")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=40.0,
        help="Abort before a pull if free disk drops below this.",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    manifest: dict[str, Any] = json.loads(args.manifest.read_text())
    ids = manifest["instance_ids"]
    keys = image_keys(ids, args.dataset, args.split, args.namespace)
    start_free = _free_gb()
    print(f"resolved {len(keys)} image keys; free disk {start_free:.1f} GB", flush=True)

    results: dict[str, dict[str, Any]] = {}
    pending = sorted(set(keys.values()))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for key in pending:
            if _free_gb() < args.min_free_gb:
                print(f"ABORT: free disk below {args.min_free_gb} GB", flush=True)
                break
            futures[pool.submit(_pull, key)] = key
        for done in as_completed(futures):
            key, ok, detail = done.result()
            results[key] = {"pulled": ok, "detail": detail}
            print(
                f"{'OK  ' if ok else 'FAIL'} {key} ({_free_gb():.1f} GB free)",
                flush=True,
            )

    report = {
        "manifest": str(args.manifest),
        "namespace": args.namespace,
        "requested_instances": len(ids),
        "requested_images": len(pending),
        "pulled": sum(1 for v in results.values() if v["pulled"]),
        "failed": sorted(k for k, v in results.items() if not v["pulled"]),
        "disk_free_gb_before": round(start_free, 2),
        "disk_free_gb_after": round(_free_gb(), 2),
        "disk_cost_gb": round(start_free - _free_gb(), 2),
        "images": results,
        "instance_image_keys": keys,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("requested_images", "pulled", "failed", "disk_cost_gb")
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
