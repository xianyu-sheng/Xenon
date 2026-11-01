"""Cancellation as a first-class outcome.

Cancelling is not a failure and not a budget block: the operator stopped the
run.  Those three states must stay distinguishable in the ledger, otherwise a
post-mortem cannot tell "the model gave up" from "the human said stop".

The exception is deliberately **not** a ``ValueError``: the harness catches
``ValueError`` to soften malformed model output, and swallowing a cancel there
would look exactly like the operator had no effect.
"""

from __future__ import annotations

__all__ = ["ModelCancelled", "POLL_SECONDS"]

# How often an in-flight call re-checks the cancel flag.  Small enough to feel
# instant, large enough not to spin.
POLL_SECONDS = 0.15


class ModelCancelled(RuntimeError):
    """The operator cancelled while a model call was in flight."""
