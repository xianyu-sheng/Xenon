"""协议宿主的最小真实验证：不调模型，只确认帧流正确。

真实模型验证在 TS 前端那一步做（那里才有真正的人机回路）。这里锁三件事：

1. ``ready`` 帧在启动时发出；
2. 一轮任务产生 ``turn.start`` → ``action``… → ``turn.end``，且 ``turn.end``
   带内核给的 evidence；
3. ``approval.request`` 是一次真正的阻塞交换：没有回答就 fail-closed。

用法：
    python -m pytest tests/test_protocol_host.py -q
"""

from __future__ import annotations

import io
import json
import threading
import time
import types
from pathlib import Path

import pytest

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.frontend import host as host_module
from xenon.frontend.host import ProtocolCallback, ProtocolHost


class _Frames:
    """Collect the NDJSON frames a host writes."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    def read(self) -> list[dict]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]

    def types(self) -> list[str]:
        return [f.get("type", "") for f in self.read()]


def _host() -> tuple[ProtocolHost, _Frames]:
    frames = _Frames()
    host = ProtocolHost(["test/fake"], out=frames.stream)
    return host, frames


# ── frames ──────────────────────────────────────────────────────────


def test_a_frame_is_one_json_line_and_carries_the_version() -> None:
    host, frames = _host()
    host.frame(type="ready", models=["a"])

    raw = frames.stream.getvalue()
    assert raw.endswith("\n") and raw.count("\n") == 1
    assert frames.read()[0] == {"v": 1, "type": "ready", "models": ["a"]}


def test_the_turn_id_rides_on_every_frame_while_a_turn_runs() -> None:
    host, frames = _host()
    host.turn = "t-1"
    host.frame(type="action", kind="think", text="hi")
    host.turn = ""
    host.frame(type="ready")

    first, second = frames.read()
    assert first["turn"] == "t-1"
    assert "turn" not in second


# ── the callback maps kernel events to frames ───────────────────────


def test_every_callback_method_produces_a_typed_frame() -> None:
    host, frames = _host()
    cb = ProtocolCallback(host)

    cb.on_think("thinking")
    cb.on_act("bash", {"command": "ls"})
    cb.on_observe("observed")
    cb.on_error("boom")
    cb.on_warning("careful")
    cb.progress(1.5, 20)
    cb.provider({"kind": "retry", "attempt": 2})

    kinds = [f["type"] for f in frames.read()]
    assert kinds == [
        "action", "action", "action", "action", "action", "progress", "provider"
    ]
    assert frames.read()[5] == {"v": 1, "type": "progress", "elapsed_seconds": 1.5, "chars": 20}
    assert frames.read()[6]["kind"] == "retry"


def test_an_unencodable_value_is_stringified_not_dropped() -> None:
    """A frame that cannot be serialized would kill the stream mid-turn."""

    host, frames = _host()
    ProtocolCallback(host).on_act("tool", {"obj": object()})

    payload = frames.read()[0]
    assert isinstance(payload["input"]["obj"], str)


def test_finish_does_not_duplicate_the_answer() -> None:
    """``turn.end`` is the authoritative end; ``on_finish`` only carries text."""

    host, frames = _host()
    ProtocolCallback(host).on_finish("done")
    assert frames.read() == []
    assert host.final_answer == "done"


# ── approval: blocking, and fail-closed ─────────────────────────────


def test_an_answered_approval_returns_the_choice() -> None:
    host, frames = _host()
    result: dict[str, str] = {}

    def ask() -> None:
        result["choice"] = host.request_approval(
            capability="shell",
            tool="bash",
            targets=["ls"],
            reason="needs approval",
        )

    worker = threading.Thread(target=ask)
    worker.start()
    # The request is in flight; answer it like a front-end would.
    for _ in range(100):
        frames_now = frames.read()
        pending = [f for f in frames_now if f["type"] == "approval.request"]
        if pending:
            break
        threading.Event().wait(0.01)
    message = pending[0]
    assert message["options"] == ["once", "turn", "allow", "deny", "cancel"]
    host.resolve_approval(message["id"], "allow")
    worker.join(timeout=2)

    assert result["choice"] == "allow"


def test_an_unanswered_approval_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unattended process must not acquire permission by being unattended."""

    monkeypatch.setenv("XENON_PROTOCOL_APPROVAL_TIMEOUT", "0.05")
    host, _frames = _host()

    assert host.request_approval(
        capability="shell", tool="bash", targets=["rm -rf /"], reason="x"
    ) == "deny"


def test_answering_an_unknown_id_is_refused() -> None:
    host, _frames = _host()
    assert host.resolve_approval("a-does-not-exist", "allow") is False


# ── turn.end carries the kernel's evidence ─────────────────────────


def test_turn_end_fields_come_from_the_kernel_result() -> None:
    run = types.SimpleNamespace(
        result=types.SimpleNamespace(
            phase=types.SimpleNamespace(value="finalized"),
            accepted=True,
            # The real field is ``evidence_status``.  Asking for ``evidence``
            # returned None for every run, so the UI showed "证据 未知".
            evidence_status=types.SimpleNamespace(value="VERIFIED"),
        ),
        ledger_path="C:/x/ledger.jsonl",
    )

    fields = host_module._turn_end_fields(run, "the answer")

    assert fields["phase"] == "finalized"
    assert fields["accepted"] is True
    assert fields["evidence"] == {"status": "VERIFIED"}
    assert fields["ledger"].endswith("ledger.jsonl")
    assert fields["answer"] == "the answer"


def test_a_missing_evidence_status_is_omitted_not_invented() -> None:
    """A frame must not claim a status the kernel never reported."""

    run = types.SimpleNamespace(
        result=types.SimpleNamespace(
            phase=types.SimpleNamespace(value="failed"), accepted=False
        ),
        ledger_path=None,
    )

    fields = host_module._turn_end_fields(run, "")

    assert "evidence" not in fields
    assert "ledger" not in fields


# ── rich markup must not cross the process boundary ─────────────────


def test_command_markup_is_stripped_before_it_reaches_the_front_end() -> None:
    """Handlers build rich strings; there is no rich on the other side.

    Measured through the TS front-end: `/cost` rendered literally as
    ``[dim]暂无缓存数据…[/dim]`` because nothing stripped the markup.
    """

    assert host_module._plain_text("[dim]暂无缓存数据。[/dim]") == "暂无缓存数据。\n"


def test_nested_markup_is_stripped_but_the_text_survives() -> None:
    assert host_module._plain_text("[bold]状态[/bold]: [cyan]COLD[/cyan]") == "状态: COLD\n"


def test_plain_text_passes_through_unchanged() -> None:
    assert host_module._plain_text("普通文本") == "普通文本\n"


def test_markup_stripping_never_raises() -> None:
    """A rendering helper must not be able to fail a command result."""

    assert isinstance(host_module._plain_text("[unclosed"), str)
    assert isinstance(host_module._plain_text(""), str)


# ── the stdin loop must stay responsive while a turn blocks ───────


def test_main_reads_the_approval_response_while_the_turn_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn blocked on an approval must not freeze the inbound loop.

    The old dispatch ran ``run_turn`` synchronously inside ``for line in
    stdin``: the kernel's approval wait then blocked the only reader, so the
    front-end's ``approval.response`` sat unread until the 300 s timeout and
    every approving turn failed closed.  Found on a real ConPTY run.
    """

    # No real REPL/kernel: run_turn becomes a function that registers a
    # pending approval and blocks on it — the exact shape of the deadlock.
    def blocking_turn(self: ProtocolHost, objective: str, level: str = "auto-edit") -> None:
        message_id = self._next_id("a")
        pending = host_module._ApprovalRequest(
            message_id=message_id, settled=threading.Event()
        )
        with self._pending_lock:
            self._pending[message_id] = pending
        self.frame(type="approval.request", id=message_id)
        settled = pending.settled.wait(timeout=8)
        self.frame(
            type="turn.end",
            phase="answered" if settled else "timeout",
            answer=pending.choice,
        )

    monkeypatch.setattr(ProtocolHost, "start", lambda self: None)
    monkeypatch.setattr(ProtocolHost, "run_turn", blocking_turn)

    import os

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    stdin = os.fdopen(in_r, "r", encoding="utf-8", newline="\n")
    stdout = os.fdopen(out_w, "w", encoding="utf-8", newline="\n")
    monkeypatch.setattr(host_module.sys, "stdin", stdin)
    monkeypatch.setattr(host_module.sys, "stdout", stdout)

    def send(message: dict) -> None:
        os.write(in_w, (json.dumps(message) + "\n").encode("utf-8"))

    def next_frame() -> dict:
        # Line-buffered read with a deadline so a frozen loop fails the test
        # instead of hanging pytest forever.
        line: list[bytes] = []

        def reader() -> None:
            data = b""
            while not data.endswith(b"\n"):
                chunk = os.read(out_r, 1)
                if not chunk:
                    break
                data += chunk
            line.append(data)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=10)
        assert line, "no frame arrived (stdin loop frozen inside the turn?)"
        return json.loads(line[0].decode("utf-8"))

    worker = threading.Thread(
        target=host_module.main, args=(["--model", "test/fake"],), daemon=True
    )
    worker.start()
    try:
        send({"type": "turn.start", "objective": "x"})
        request = next_frame()
        assert request["type"] == "approval.request"
        # While the turn is blocked: a command is refused rather than run
        # concurrently against the same REPL.
        send({"type": "command", "name": "/cost"})
        refused = next_frame()
        assert refused["type"] == "error"
        assert "in progress" in refused["error"]
        send(
            {
                "type": "approval.response",
                "id": request["id"],
                "choice": "deny",
            }
        )
        end = next_frame()
        assert end["type"] == "turn.end"
        assert end["phase"] == "answered"
        send({"type": "shutdown"})
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        for fd in (in_w, out_r):
            try:
                os.close(fd)
            except OSError:
                pass


# ── new blocking exchanges: plan approval, prompts, cancel ──────────


def _wait_for(frames: _Frames, kind: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for frame in frames.read():
            if frame.get("type") == kind:
                return frame
        time.sleep(0.01)
    raise AssertionError(f"no {kind} frame: {frames.read()}")


def test_plan_approval_is_a_protocol_exchange() -> None:
    host, frames = _host()
    result: dict = {}

    worker = threading.Thread(
        target=lambda: result.update(host._confirm_plan("do the thing"))
    )
    worker.start()
    request = _wait_for(frames, "approval.request")

    assert request["kind"] == "plan"
    assert host.resolve_approval(request["id"], "allow")
    worker.join(timeout=2)
    assert result == {"approved": True, "feedback": ""}


def test_a_command_question_becomes_a_prompt_frame() -> None:
    host, frames = _host()
    answer: dict = {}

    def ask() -> None:
        answer["value"] = host._protocol_prompt(
            "pick", default="0", choices=["0", "1"]
        )

    worker = threading.Thread(target=ask)
    worker.start()
    request = _wait_for(frames, "prompt.request")

    assert request["choices"] == ["0", "1"]
    assert host.resolve_prompt(request["id"], "1")
    worker.join(timeout=2)
    assert answer == {"value": "1"}


def test_cancel_targets_the_active_runtime() -> None:
    host, _frames = _host()
    called: list[bool] = []
    host.repl = types.SimpleNamespace(
        _active_runtime=types.SimpleNamespace(
            request_cancel=lambda: called.append(True)
        )
    )

    assert host.cancel_turn() is True
    assert called == [True]

    host.repl = types.SimpleNamespace()
    assert host.cancel_turn() is False


def test_status_carries_context_and_elapsed() -> None:
    host, frames = _host()
    host.repl = types.SimpleNamespace(
        registry=types.SimpleNamespace(current_mode="react"),
        agent_context=types.SimpleNamespace(get=lambda key: 3),
        ctx_mgr=types.SimpleNamespace(stats=lambda: {"usage_ratio": "12.5%"}),
    )
    host.turn = "t-1"
    host.turn_started_at = time.time() - 5

    host.status()

    frame = frames.read()[-1]
    assert frame["context_used_pct"] == pytest.approx(0.125)
    assert frame["elapsed_seconds"] >= 4


def test_an_approval_carries_kernel_advisories() -> None:
    host, frames = _host()
    host.repl = types.SimpleNamespace(
        agent_context=types.SimpleNamespace(
            get_security_advisories=lambda: [
                {"advisory": {"rule_id": "prompt_injection.x"}}
            ]
        )
    )
    choice: dict = {}

    def approve() -> None:
        choice["value"] = host.request_approval(
            capability="c", tool="t", targets=[], reason="r"
        )

    worker = threading.Thread(target=approve)
    worker.start()
    request = _wait_for(frames, "approval.request")

    assert request["advisories"][0]["advisory"]["rule_id"] == "prompt_injection.x"
    host.resolve_approval(request["id"], "deny")
    worker.join(timeout=2)
    assert choice == {"value": "deny"}


def test_a_conversational_turn_answers_directly_and_records_a_ledger() -> None:
    """A greeting must not enter the kernel loop (it would be vetoed)."""

    from xenon.repl.direct_chat import DirectChatOutcome

    host, frames = _host()
    host.cwd = Path.cwd()
    host.repl = types.SimpleNamespace(
        _run_direct=lambda *args, **kwargs: DirectChatOutcome(
            answer="我在", model_id="test/fake"
        ),
        _session_events=types.SimpleNamespace(session_id="s1"),
    )
    contract = types.SimpleNamespace(
        to_execution_policy=lambda: types.SimpleNamespace(requires_tools=False)
    )

    host._run_chat("你好", contract)

    end = frames.read()[-1]
    assert end["type"] == "turn.end"
    assert end["answer"] == "我在"
    assert end["phase"] == "finalized"
    assert end["evidence"] == {"status": "unverified"}
    assert end["ledger"]


def test_the_permission_selector_works_over_the_protocol() -> None:
    """``/permissions`` used to print the menu and change nothing (no PTY)."""

    import xenon.repl.selector as selector_mod
    from xenon.repl.selector import permission_choices

    host, frames = _host()
    picked: dict = {}

    def choose() -> None:
        with host._protocol_prompts():
            picked["value"] = selector_mod.ask_choice(
                host.repl, title="权限", choices=permission_choices("")
            )

    worker = threading.Thread(target=choose)
    worker.start()
    request = _wait_for(frames, "prompt.request")
    assert any("bypass" in label for label in request["choices"])
    host.resolve_prompt(request["id"], "4")
    worker.join(timeout=2)

    assert picked == {"value": "bypass"}


def test_the_setup_key_entry_works_over_the_protocol() -> None:
    """``/setup`` read the API key from the raw terminal, which the UI owns."""

    import xenon.repl.setup_wizard as setup_wizard

    host, frames = _host()
    picked: dict = {}

    def type_key() -> None:
        with host._protocol_prompts():
            picked["value"] = setup_wizard._masked_input("请输入 Key")

    worker = threading.Thread(target=type_key)
    worker.start()
    request = _wait_for(frames, "prompt.request")
    assert request["secret"] is True
    host.resolve_prompt(request["id"], "sk-probe")
    worker.join(timeout=2)

    assert picked == {"value": "sk-probe"}


def test_command_output_includes_console_renders(monkeypatch) -> None:
    """Some handlers print instead of returning; both shapes must reach the UI."""

    host, frames = _host()
    host.repl = types.SimpleNamespace(
        registry=object(), ctx_mgr=object(), agent_context={}
    )
    import xenon.repl.command_registry as registry_mod

    def fake_dispatch(*args, **kwargs):
        print("[dim]line one[/dim]")
        return "[bold]returned[/bold]"

    monkeypatch.setattr(registry_mod, "dispatch_command", fake_dispatch)

    host.run_command("/demo", "")

    result = frames.read()[-1]
    assert result["type"] == "command.result"
    assert "line one" in result["output"]
    assert "returned" in result["output"]


def test_prompt_patches_are_restored_after_use() -> None:
    """A scoped bridge must not leak into the next command (or test)."""

    import rich.prompt as rich_prompt

    import xenon.repl.setup_wizard as setup_wizard

    host, _frames = _host()
    before = (
        rich_prompt.Prompt.ask,
        rich_prompt.Confirm.ask,
        rich_prompt.IntPrompt.ask,
        setup_wizard._masked_input,
    )

    with host._protocol_prompts():
        assert rich_prompt.Prompt.ask is not before[0]
        assert setup_wizard._masked_input is not before[3]

    after = (
        rich_prompt.Prompt.ask,
        rich_prompt.Confirm.ask,
        rich_prompt.IntPrompt.ask,
        setup_wizard._masked_input,
    )
    assert after == before


def test_session_select_dispatches_the_command_with_its_slash(monkeypatch) -> None:
    """``session.select`` runs ``/resume``; the registry is keyed by the slash.

    Passing ``"resume"`` produced "未知命令" and silently restored nothing —
    the same defect the ``--resume`` startup path hit.
    """

    import xenon.repl.command_registry as registry_mod

    host, frames = _host()
    host.repl = types.SimpleNamespace(
        agent_context=types.SimpleNamespace(),
        registry=None,
        ctx_mgr=None,
    )
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        registry_mod,
        "dispatch_command",
        lambda name, args, **_kwargs: seen.append((name, args)) or "",
    )

    host.select_session("saved-session")
    host.command_thread.join(timeout=5)

    assert seen == [("/resume", "saved-session")]
    result = frames.read()[-1]
    assert result["type"] == "command.result"
    assert result["name"] == "/resume"


def test_the_ready_frame_reports_a_first_run(monkeypatch) -> None:
    """No credentials/models is a fact the front-end needs; it renders /setup."""

    import xenon.repl.model_registry as registry_mod
    import xenon.repl.repl as repl_mod

    class _Registry:
        def load_from_file(self, _path):
            return None

        def load_from_credentials(self):
            return None

        def list_models(self):
            return []

    class _Repl:
        def __init__(self, **_kwargs):
            self.agent_context = types.SimpleNamespace(
                set_plan_callback=lambda _cb: None
            )

        def _check_first_run(self):
            return {"needs_setup": True}

    monkeypatch.setattr(registry_mod, "ModelRegistry", _Registry)
    monkeypatch.setattr(repl_mod, "REPL", _Repl)

    host, frames = _host()
    host.start()

    ready = frames.read()[0]
    assert ready["type"] == "ready"
    assert ready["setup_needed"] is True


def test_a_failing_first_run_check_never_blocks_startup() -> None:
    host, _frames = _host()

    def boom():
        raise RuntimeError("credential store unreadable")

    host.repl = types.SimpleNamespace(_check_first_run=boom)

    assert host._setup_needed() is False


def test_file_completion_lists_the_workspace_without_vendor_noise(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    host, _frames = _host()
    host.cwd = tmp_path

    assert host.list_files("main") == ["src/main.py"]
    assert host.list_files("") == ["README.md", "src/main.py"]


def test_file_completion_is_bounded(tmp_path) -> None:
    for index in range(20):
        (tmp_path / f"file{index}.txt").write_text("x", encoding="utf-8")
    host, _frames = _host()
    host.cwd = tmp_path

    assert len(host.list_files("", limit=5)) == 5
