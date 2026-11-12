"""Measure DeepSeek prompt-cache TTL directly against the official API.

Design doc §3.1 leaves one question open: does a cache hit extend the entry's
life, and does a pause longer than the TTL make the next call pay full price?
That cannot be answered from a third-party router -- some fake cache metrics.
This probe talks to the official endpoint, uses one stable prefix, and reports
the provider's own ``prompt_cache_hit_tokens`` / miss counters.

Usage::

    python docs/evidence/cache-ttl/probe.py [--gap 300] [--long-gap 0]

The first two calls are seconds apart (expect a hit).  After ``--gap`` seconds
the third call answers the TTL question; an optional ``--long-gap`` adds a
fourth call after an even longer pause (0 = skip).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from xenon.utils.credentials_io import load_document

ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

# A stable prefix long enough to be cacheable (DeepSeek's cache works in
# 64-token blocks).  It must be byte-identical across calls.
PREFIX = (
    "You are a cache probe. The following facts are fixed for every call and "
    "must never be repeated back. " + "Fact: the sky is blue and the sea is "
    "deep. " * 60
)
ASK = "Reply with exactly: ok"


def _call(key: str) -> dict:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": PREFIX},
                {"role": "user", "content": ASK},
            ],
            "max_tokens": 8,
            "temperature": 0,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    started = time.time()
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))
    usage = payload.get("usage") or {}
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "latency_ms": int((time.time() - started) * 1000),
        "prompt_tokens": usage.get("prompt_tokens"),
        "cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
        "cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


def _hit(row: dict) -> bool:
    return bool(row.get("cache_hit_tokens"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gap", type=int, default=300)
    parser.add_argument("--long-gap", type=int, default=0)
    parser.add_argument(
        "--control",
        action="store_true",
        help="skip the immediate second call (tests TTL without a warm hit)",
    )
    parser.add_argument("--first-gap", type=int, default=3)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    credentials = load_document(Path.home() / ".xenon" / "credentials.yaml")
    key = str(credentials.get("deepseek") or "").strip()
    if not key:
        print("no deepseek key in the credentials document", file=sys.stderr)
        return 2

    rows: list[dict] = []
    rows.append(_call(key))
    if not args.control:
        time.sleep(max(0, args.first_gap))
        rows.append(_call(key))
        print(f"within-TTL ({args.first_gap}s): hit={_hit(rows[-1])} {rows[-1]}")
    time.sleep(max(0, args.gap))
    rows.append(_call(key))
    print(f"after {args.gap}s: hit={_hit(rows[-1])} {rows[-1]}")
    if args.long_gap > 0:
        time.sleep(args.long_gap)
        rows.append(_call(key))
        print(f"after {args.long_gap}s: hit={_hit(rows[-1])} {rows[-1]}")

    after_gap = rows[2] if not args.control else rows[1]
    result = {
        "endpoint": ENDPOINT,
        "model": MODEL,
        "control": bool(args.control),
        "gap_seconds": args.gap,
        "long_gap_seconds": args.long_gap,
        "calls": rows,
        "interpretation": {
            "warm_hit": None if args.control else _hit(rows[1]),
            "after_gap_hit": _hit(after_gap),
            "long_gap_hit": _hit(rows[-1]) if args.long_gap > 0 else None,
        },
    }
    out = (
        Path(args.out)
        if args.out
        else Path(__file__).with_name(time.strftime("result-%Y%m%d-%H%M%S.json"))
    )
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
