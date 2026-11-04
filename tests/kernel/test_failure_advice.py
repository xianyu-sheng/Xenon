"""#417: a failure reaches the model as a next step, not just an error string.

The point is usability, not verification: Xenon records the failure either way.
Without advice the agent repeated the same call until the loop detector killed
the turn (real dogfood: four identical ``read_file 需要 file_path`` calls).
"""

from __future__ import annotations

from xenon.kernel.failure_advice import advice_for_failure


def test_a_missing_parameter_names_the_fix() -> None:
    advice = advice_for_failure(
        {"status": "failed", "error": "[exec_read] read_file 需要 file_path"}
    )

    assert "required parameter" in advice
    assert "finish" in advice, "offer the action that fits the intent"


def test_a_missing_path_points_at_search() -> None:
    advice = advice_for_failure({"status": "failed", "error": "文件不存在: a/b.py"})

    assert "does not exist" in advice
    assert "search" in advice.lower()


def test_a_directory_says_so() -> None:
    advice = advice_for_failure(
        {"status": "failed", "error": "这是一个目录，不是文件: D:/x"}
    )

    assert "directory" in advice


def test_an_os_permission_error_is_not_an_approval_problem() -> None:
    """The dogfood run read a directory, got Errno 13, and concluded that the
    sandbox had denied it — then asked the user for permission."""
    advice = advice_for_failure(
        {"status": "failed", "error": "PermissionError: [Errno 13] Permission denied"}
    )

    assert "not an approval issue" in advice


def test_a_workspace_fence_refusal_says_it_is_a_decision() -> None:
    advice = advice_for_failure(
        {"status": "failed", "error": "路径越界: D:/other/a.py 不在允许的目录"}
    )

    assert "ask the operator" in advice


def test_a_successful_receipt_gets_no_advice() -> None:
    assert advice_for_failure({"status": "ok"}) == ""


def test_an_unknown_failure_still_says_change_one_thing() -> None:
    advice = advice_for_failure({"status": "failed", "error": "weird internal thing"})

    assert "change one thing" in advice.lower()


def test_advice_reaches_the_next_view_and_blocks_nothing() -> None:
    """The note is offered; the run continues; the failure is still recorded."""
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import Finish, TaskSpec
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("advice")
    runtime = _runtime(ledger)

    class _FailingGateway:
        def execute(self, call, services):
            from xenon.kernel.contracts import ReceiptStatus, ToolReceipt

            return ToolReceipt(
                receipt_id="r1",
                call_id="c1",
                task_id="advice",
                tool=call.spec_name,
                status=ReceiptStatus.FAILED,
                effects=(),
                declared_effects=(),
                error="[exec_read] read_file 需要 file_path",
            )

    runtime._gateway = _FailingGateway()
    from xenon.kernel.contracts import CallTool, Capability, ToolIntent

    result = runtime.run(
        TaskSpec(task_id="advice", objective="inspect", workspace="."),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                Finish("answered anyway"),
            ]
        ),
    )

    assert any("required parameter" in note for note in runtime._loop_notes), (
        "the next view must carry the advice"
    )
    # Advice does not stop the run: the model gets another turn and proposes
    # `Finish`.  (Whether that Finish is *delivered* is the verification
    # layer's decision — with no evidence, blocking it is correct.)
    assert result.metrics["steps"] >= 2, "the model must get a turn after a failure"


def test_refusals_are_counted_for_the_protocol_report() -> None:
    """#417: a pile of pre-dispatch refusals is a catalog problem, not a model
    problem, so it has to be countable."""
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from xenon.kernel.contracts import (
        CallTool,
        Capability,
        Finish,
        TaskSpec,
        ToolIntent,
    )
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("refusals")
    runtime = _runtime(ledger)
    runtime._catalog = type(
        "NoCatalog",
        (),
        {
            "specs": lambda self, profile="core": (),
            "bind": lambda self, intent: __import__(
                "xenon.kernel.contracts", fromlist=["ToolBinding"]
            ).ToolBinding(
                status=__import__(
                    "xenon.kernel.contracts", fromlist=["BindingStatus"]
                ).BindingStatus.UNRESOLVED,
                reason="no tool exposes capability read",
            ),
        },
    )()

    result = runtime.run(
        TaskSpec(task_id="refusals", objective="inspect", workspace="."),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                Finish("done"),
            ]
        ),
    )

    assert result.metrics["refusals"] >= 1
    assert runtime.status_snapshot()["refusal_total"] >= 1
    assert runtime.status_snapshot()["refusals"]
