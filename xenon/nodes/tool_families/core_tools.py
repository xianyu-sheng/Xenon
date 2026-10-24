"""Model-facing core tools.

These are thin, deterministic aliases over the existing handlers.  Keeping the
aliases separate means the core profile can shrink the model surface without
deleting the compatibility handlers or the structured test/git adapters.
"""

from __future__ import annotations

from typing import Any

from xenon.engine.context import AgentContext


class CoreToolsMixin:
    def _read(self, context: AgentContext) -> dict[str, Any]:
        return self._read_file(context)

    def _search_core(self, context: AgentContext) -> dict[str, Any]:
        if getattr(self, "search_pattern", ""):
            return self._search_files(context)
        return self._list_files(context)

    def _bash(self, context: AgentContext) -> dict[str, Any]:
        result = self._exec_command(context)
        result["action_type"] = "bash"
        # ``command`` may fall through to the shell path (for example a
        # .exe-prefixed pytest invocation).  Preserve the same structured test
        # counts that run_tests provides so Claim Gate sees real evidence.
        if result.get("tests_run") is None:
            from xenon.engine.evidence_contracts import parse_test_counts

            command = str(result.get("command") or "")
            if "pytest" in command.lower() or "unittest" in command.lower():
                output = (result.get("stdout") or "") + (result.get("stderr") or "")
                counts = parse_test_counts(output)
                result.update(
                    {
                        "tests_run": counts["tests_run"],
                        "tests_passed": counts["passed"],
                        "tests_failed": counts["failed"],
                        "tests_errors": counts["errors"],
                    }
                )
        return result

    def _apply_patch(self, context: AgentContext) -> dict[str, Any]:
        has_files = bool(getattr(self, "files", None))
        has_edits = bool(getattr(self, "edits", None))
        if has_files and has_edits:
            return {
                "action_type": "apply_patch",
                "success": False,
                "error": "apply_patch 一次只接受 files 或 edits 其中一种",
            }
        if has_files:
            result = self._batch_write(context)
        elif has_edits:
            result = self._batch_edit(context)
        else:
            return {
                "action_type": "apply_patch",
                "success": False,
                "error": "apply_patch 需要 files 或 edits 参数",
            }
        result["action_type"] = "apply_patch"
        return result
