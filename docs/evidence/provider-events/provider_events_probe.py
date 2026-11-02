"""Real probe: do provider-phase events actually fire on the kernel path? (#320)

Three real scenarios:
  1. a normal kernel turn — start/end must bracket the call;
  2. a wrong API key — the failure must be reported with its attempt number;
  3. an unreachable host — the network retry must be visible, not silent.

Before #320 these produced nothing: the only emitter lived in the deleted engine
layer, so the hooks were declared but dead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from dataclasses import replace  # noqa: E402

from xenon.harness.llm import _call_model, resolve_model_config  # noqa: E402
from xenon.utils.llm_client import chat_completion  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"
MESSAGES = [{"role": "user", "content": "Reply with exactly: ok"}]


def show(label: str, events: list[dict]) -> None:
    print(f"\n=== {label} ===")
    if not events:
        print("  (no events — the bug)")
        return
    for event in events:
        if event["kind"] == "start":
            print(
                f"  start phase={event['phase']} attempt={event['attempt']}"
                f"/{event['provider_attempts']} timeout={event['timeout']:.0f}s"
                f" streamed={event['streamed']}"
            )
        else:
            print(
                f"  end   success={event['success']} attempt={event['attempt']}"
                f" retrying={event['retrying']} reason={event['reason']!r}"
            )


def main() -> None:
    # 1) real model through the kernel funnel
    events: list[dict] = []
    config = resolve_model_config([MODEL])
    reply = _call_model(
        replace(config, request_sink=events.append),
        None,
        MESSAGES,
    )
    show("real kernel call", events)
    print(f"  reply: {reply.strip()[:60]}")

    # 2) bad credentials: a failure must be attributable
    events = []
    config = resolve_model_config([MODEL])
    try:
        _call_model(
            replace(
                config, api_key="sk-invalid-for-probe", request_sink=events.append
            ),
            None,
            MESSAGES,
        )
    except Exception as exc:  # noqa: BLE001 — the failure is the subject
        print(f"\n  raised {type(exc).__name__}")
    show("bad api key", events)

    # 3) unreachable host: the retry loop must be visible
    events = []
    try:
        chat_completion(
            "deepseek/x",
            MESSAGES,
            base_url="http://127.0.0.1:9/v1",  # discard port: refuses instantly
            credentials={"deepseek": "sk-test"},
            max_retries=2,
            request_sink=events.append,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n  raised {type(exc).__name__}")
    show("unreachable host (2 attempts)", events)


if __name__ == "__main__":
    main()
