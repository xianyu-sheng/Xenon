"""Turn a stuck signal into a next step the model can act on.

A loop used to reach the model as ``"stuck: read called with identical
arguments 4 times"`` and nothing else.  That names the symptom and says
"change the approach", which is not an instruction — a real run read the same
two files seven times, never edited either, and died on the budget.  The model
knew it was repeating; it did not know what to do instead.

Same design rules as ``failure_advice``:

* **advice only** — never executes, never blocks, never mutates a receipt;
* **derived, not guessed** — keyed off the signal's own ``kind``, ``tool`` and
  ``detail``, so the same signal always gets the same advice (replay-safe);
* **bounded** — at most one line, and the note list is already capped.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["advice_for_loop"]


def advice_for_loop(signal: Mapping[str, Any]) -> str:
    """One actionable sentence for a stuck signal, or "" when none applies."""

    kind = str(signal.get("kind") or "")
    tool = str(signal.get("tool") or "") or "that tool"

    if kind == "identical_call":
        # The commonest dead end, and the one measured: reading is not work.
        if tool in {"read", "search", "tool_result_read"}:
            return (
                f"You have already read what {tool} returns; reading it again "
                "cannot change it.  Stop calling it and act on what you have: "
                "call `edit`/`apply_patch` for the change, or `finish` if the "
                "work is already done.  If you genuinely need a different file, "
                "name a different target."
            )
        return (
            f"Repeating {tool} with the same arguments cannot produce a new "
            "result.  Change one of: the target, the parameters, or the tool "
            "itself — then call again."
        )

    if kind == "repeated_failure":
        return (
            f"{tool} keeps failing with the same error.  Do not retry it "
            "unchanged: read the error in the receipt, change the input it "
            "complains about, and only then call again."
        )

    if kind == "capability_cycle":
        return (
            f"The same sequence of tools keeps repeating ({tool} is the latest). "
            "The cycle produces no new information.  Break it by doing the "
            "thing you have been circling: make the edit, or finish with what "
            "the receipts already prove."
        )

    return ""
