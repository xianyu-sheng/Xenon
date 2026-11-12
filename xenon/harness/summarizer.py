"""The LLM half of the dual-summary fold (design doc §13 decision 1).

The kernel's deterministic fold (``kernel/compaction.py``) keeps the fact
backbone — seq, status, tool, the decision chain.  It cannot tell the model
*what those facts meant together*, which is most of what a continuation needs.
This module supplies that connective narrative, with one hard rule:

    The narrative compresses.  It never asserts a fact the ledger does not.

The deterministic digest stays in the view right beside this narrative, so
even a hallucinated summary is contradicted by pinned label events rather
than becoming the only record.  The summary is written to the ledger as a
``CONTEXT_SUMMARY`` event — appended at the tail, hash-chained — so a replay
is byte-identical and the ``system + tools`` prefix cache is never disturbed.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

__all__ = ["LLMSummarizer", "folded_lines"]

#: Bound the summariser's own output: a continuation aid, not a second log.
_SUMMARY_MAX_TOKENS = 1024
#: Bound the input: how many folded events one narrative call sees.
_FOLDED_LIMIT = 80

_PROTOCOL = """\
You are compacting an earlier slice of one coding task so work can continue.
Write a faithful continuation brief of the events given, in the language the
objective is written in. Preserve, concretely:

- every decision and why it was taken;
- every failed command or tool, with its seq and error;
- the current state of files that were created, changed or deleted;
- which obligations are still open and what would satisfy them;
- any plan steps already closed.

Rules:
- State ONLY facts present in the events. Do not infer, explain away a failure,
  or report a task as done when an obligation is still open. If something is
  unclear, say it is unclear.
- Be dense, not narrative: short lines grouped by topic, not prose paragraphs.
- Output the brief directly. No preamble, no JSON, no markdown heading.
"""


def folded_lines(folded: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render the folded summary items as compact, readable lines.

    Inputs are the same summary dicts ``fold_dropped`` consumes; the model
    reads a structural rendering of them, not raw event JSON.
    """

    lines: list[str] = []
    for item in folded:
        seq = item.get("seq")
        kind = str(item.get("kind") or "event")
        pieces: list[str] = []
        for key in ("tool", "status", "step_id"):
            value = item.get(key)
            if value not in (None, ""):
                pieces.append(f"{key}={value}")
        target = item.get("target")
        if target:
            pieces.append(f"target={target}")
        reason = item.get("reason")
        if reason:
            pieces.append(str(reason))
        text = item.get("text")
        if text:
            pieces.append(str(text))
        head = f"seq={seq} {kind}"
        if pieces:
            head += " " + " ".join(pieces)
        effects = item.get("effects")
        if isinstance(effects, Sequence) and not isinstance(effects, str):
            for effect in effects:
                if isinstance(effect, Mapping):
                    head += " | " + " ".join(
                        f"{key}={value}"
                        for key, value in effect.items()
                        if value not in (None, "", ())
                    )
        error = item.get("error")
        if error:
            head += f" | error={error}"
        lines.append(head)
    return lines


class LLMSummarizer:
    """Produce one continuation narrative for a fold, via the turn's model."""

    def __init__(
        self,
        config: Any,
        *,
        client: Callable[..., str] | None = None,
    ) -> None:
        # The summariser spends from the turn's own model config (model,
        # credentials, accounting hooks), so no second model ever needs
        # configuring and the spend lands on the same per-task totals.
        from dataclasses import replace

        self.config = replace(config, max_tokens=_SUMMARY_MAX_TOKENS)
        self._client = client

    def summarize(
        self,
        *,
        objective: str,
        seqs: Sequence[int],
        folded: Sequence[Mapping[str, Any]],
    ) -> str:
        if not folded:
            return ""
        # Bound the input even if a caller passes more: the narrative covers
        # the most recent folded events, and an unbounded prompt would grow
        # with the history it is supposed to compress.
        folded = tuple(folded)[-_FOLDED_LIMIT:]
        try:
            from xenon.harness.llm import _call_model

            body = (
                f"Objective: {objective}\n"
                f"Events compacted (seqs {seqs[0]}..{seqs[-1]}, "
                f"{len(folded)} total):\n"
                + "\n".join(folded_lines(folded))
                + "\n\nWrite the continuation brief now."
            )
            narrative = _call_model(
                self.config,
                self._client,
                [
                    {"role": "system", "content": _PROTOCOL},
                    {"role": "user", "content": body},
                ],
            )
        except Exception:  # noqa: BLE001 — compaction is advisory, never fatal
            return ""
        text = str(narrative or "").strip()
        # A model that returns only the universal refusal marker gave us no
        # narrative; the deterministic digest alone is still a correct fold.
        return text
