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


def test_a_blocked_turn_still_says_why_in_its_answer() -> None:
    """A stopped run must not end with an empty answer.

    Measured run: the refusal cap blocked the turn and ``turn.end`` carried
    ``answer: ""`` — the screen showed a turn that ended with nothing, and the
    reason was only in a ledger the terminal could not read.
    """

    from xenon.frontend.host import _turn_end_fields
    from xenon.kernel.contracts import EvidenceStatus, TaskPhase, TaskResult

    result = TaskResult(
        task_id="blocked-1",
        accepted=False,
        phase=TaskPhase.BLOCKED,
        answer="",
        evidence_status=EvidenceStatus.ABSTAINED,
    )

    fields = _turn_end_fields(types.SimpleNamespace(result=result, ledger_path=None), "")

    assert fields["phase"] == "blocked"
    assert fields["answer"].strip(), "a blocked turn must explain itself on screen"
    assert "blocked" in fields["answer"]


def test_a_cancelled_turn_keeps_its_silence() -> None:
    """The operator said stop; a generated explanation would be noise."""

    from xenon.frontend.host import _turn_end_fields
    from xenon.kernel.contracts import EvidenceStatus, TaskPhase, TaskResult

    result = TaskResult(
        task_id="cancelled-1",
        accepted=False,
        phase=TaskPhase.ABORTED,
        answer="",
        evidence_status=EvidenceStatus.UNVERIFIED,
        stop_signal="cancelled",
    )

    fields = _turn_end_fields(types.SimpleNamespace(result=result, ledger_path=None), "")

    assert fields["answer"] == ""
    assert fields.get("cancelled") is True


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


def test_literal_bracket_identifiers_survive_the_markup_boundary() -> None:
    """Rich silently swallows any ``[word]`` it cannot resolve as a style.

    Measured through the real TS front-end: ``/memory lessons`` printed all
    three lessons without their ids, because the report line starts with
    ``[lsn-…]`` and rich parsed the id as an unknown style tag.  Every
    bracketed identifier (lesson, fact, claim) and markdown link hits the
    same path, so the boundary must escape what rich does not own.
    """

    assert (
        host_module._plain_text("  [lsn-3ffd045bb4383d75] preference/active")
        == "  [lsn-3ffd045bb4383d75] preference/active\n"
    )
    assert (
        host_module._plain_text("[fact-40-workspace] ok")
        == "[fact-40-workspace] ok\n"
    )
    assert (
        host_module._plain_text("[docs](https://example.com)")
        == "[docs](https://example.com)\n"
    )


def test_real_tags_still_strip_alongside_literal_identifiers() -> None:
    text = host_module._plain_text(
        "[bold]状态[/bold]: [lsn-abc] [error]失败[/error]"
    )
    assert text == "状态: [lsn-abc] 失败\n"


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


def test_main_refuses_a_turn_while_a_command_waits_on_a_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A command blocked on a question owns the REPL, so a turn must wait.

    Measured on the dashboard: ``/set_model`` printed its picker and waited on
    ``Prompt.ask``; the person typed ``11`` into the page's input box and the
    kernel started a *turn* on top of the still-running command.  The "11" ran
    against the old model (HTTP 401) while the picker stayed open for its full
    timeout.  One exchange at a time, in both directions.
    """

    release = threading.Event()

    def blocking_command(self: ProtocolHost, name: str, args: str) -> None:
        message_id = self._next_id("p")
        self.frame(
            type="prompt.request",
            id=message_id,
            message="选择模型 #",
            choices=[],
            default="",
            secret=False,
        )
        release.wait(timeout=8)
        self.frame(type="command.result", name=name, output="done")

    monkeypatch.setattr(ProtocolHost, "start", lambda self: None)
    monkeypatch.setattr(ProtocolHost, "run_command", blocking_command)

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
        assert line, "no frame arrived (stdin loop frozen inside the command?)"
        return json.loads(line[0].decode("utf-8"))

    worker = threading.Thread(
        target=host_module.main, args=(["--model", "test/fake"],), daemon=True
    )
    worker.start()
    try:
        send({"type": "command", "name": "/set_model"})
        request = next_frame()
        assert request["type"] == "prompt.request"

        send({"type": "turn.start", "objective": "11"})
        refused = next_frame()
        assert refused["type"] == "error"
        assert "command is in progress" in refused["error"]

        release.set()
        send({"type": "shutdown"})
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        release.set()
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


def test_cancelling_an_approval_stops_the_whole_task() -> None:
    """The panel's "cancel the whole task" answer must stop the run.

    The kernel only saw a refused tool call and kept planning, so the prompt
    promised something the run did not do.  The runtime is the object both the
    panel and the loop share; cancelling it reaches the loop's limit check.
    """

    host, _frames = _host()
    cancelled: list[bool] = []
    host.repl = types.SimpleNamespace(
        _active_runtime=types.SimpleNamespace(
            request_cancel=lambda: cancelled.append(True)
        ),
    )
    host.request_approval = lambda **kwargs: "cancel"

    approved, reason = host._confirm_tool(
        "command", {"command": "echo hi"}, "EXECUTE"
    )

    assert approved is False
    assert reason == "用户取消任务"
    assert cancelled == [True]


def test_allowing_through_the_protocol_remembers_the_grant() -> None:
    """``allow`` is a durable §9 answer, not a one-run flag.

    Only the interactive bridge wrote the session ``GrantBook``; an "allow"
    given through the front-end reached the running policy and vanished with
    the run, so the next turn asked again and ``/permissions`` listed nothing.
    """

    from xenon.kernel.contracts import Capability
    from xenon.kernel.session_grants import GrantBook

    book = GrantBook("/repo")
    host, _frames = _host()
    host.repl = types.SimpleNamespace(_grant_book=book, agent_context={})
    host.request_approval = lambda **kwargs: "allow"

    approved, _reason = host._confirm_tool(
        "command", {"command": "git status"}, "EXECUTE"
    )

    assert approved is True
    assert host.repl._last_approval_scope == "session"
    assert book.active("/repo", Capability.SHELL, ["git status"])


def test_denying_an_approval_only_refuses_that_call() -> None:
    host, _frames = _host()
    cancelled: list[bool] = []
    host.repl = types.SimpleNamespace(
        _active_runtime=types.SimpleNamespace(
            request_cancel=lambda: cancelled.append(True)
        ),
    )
    host.request_approval = lambda **kwargs: "deny"

    approved, reason = host._confirm_tool(
        "command", {"command": "echo hi"}, "EXECUTE"
    )

    assert approved is False
    assert reason == "用户拒绝"
    assert cancelled == []


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


def test_an_explicit_memory_request_is_stored_before_the_model_answers() -> None:
    """Measured through the real front-end: "请记住：…" was answered "已记住"
    by the model while the memory service was never touched.

    The interactive REPL persists explicit requests inside ``turn_flow``; the
    host's chat path dispatched straight to ``_run_direct`` and skipped the
    gate.  The receipt the gate prints is the turn's answer.
    """

    from rich.console import Console

    host, frames = _host()
    host.cwd = Path.cwd()
    seen: list[str] = []

    def remember(text: str) -> bool:
        seen.append(text)
        Console(force_terminal=False, width=100).print(
            "已写入 · ID: mem-1\n位置: memories/facts.md"
        )
        return True

    def direct(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a memory request must not reach the model")

    host.repl = types.SimpleNamespace(
        _handle_explicit_memory_request=remember,
        _run_direct=direct,
        _session_events=types.SimpleNamespace(session_id="s1"),
    )
    contract = types.SimpleNamespace(
        to_execution_policy=lambda: types.SimpleNamespace(requires_tools=False)
    )

    host._run_chat("请记住：项目代号是青鸟-7391", contract)

    end = frames.read()[-1]
    assert end["type"] == "turn.end"
    assert end["phase"] == "finalized"
    assert "mem-1" in end["answer"]
    assert end["evidence"] == {"status": "verified"}
    assert seen == ["请记住：项目代号是青鸟-7391"]


class _RecordingContext:
    """The conversation store the host must keep in step with the REPL."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.answers: list[str] = []

    def add_request_message(self, content: str, **kwargs: object) -> str:
        self.requests.append((content, dict(kwargs)))
        return f"[snapshot]\n{content}"

    def add_assistant_message(self, content: str, **kwargs: object) -> None:
        self.answers.append(content)


def test_a_memory_gated_turn_records_both_sides_of_the_exchange() -> None:
    """The receipt is the answer, so the conversation must hold it.

    Measured through the real front-end: "请记住：…42" was persisted into the
    memory service but left ``ctx_mgr.history`` empty, so ``/save`` wrote a
    session with no messages. ``list_sessions`` drops empty sessions, and
    ``xenon --resume <name>`` then answered "会话 '…' 不存在".
    """

    from rich.console import Console

    host, _frames = _host()
    host.cwd = Path.cwd()
    context = _RecordingContext()

    def remember(text: str) -> bool:
        Console(force_terminal=False, width=100).print("已写入 · ID: mem-2")
        return True

    host.repl = types.SimpleNamespace(
        _handle_explicit_memory_request=remember,
        _run_direct=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("a memory request must not reach the model")
        ),
        _session_events=types.SimpleNamespace(session_id="s1"),
        ctx_mgr=context,
    )
    contract = types.SimpleNamespace(
        to_execution_policy=lambda: types.SimpleNamespace(requires_tools=False)
    )

    host._run_chat("请记住：项目代号是蓝鲸-8888", contract)

    assert [content for content, _ in context.requests] == [
        "请记住：项目代号是蓝鲸-8888"
    ]
    assert len(context.answers) == 1
    assert "mem-2" in context.answers[0]


def test_a_kernel_turn_records_the_request_before_the_answer(monkeypatch) -> None:
    """``/save`` after a real tool task used to persist an answer-only history.

    The interactive REPL records the request in ``turn_flow``; the protocol
    host called ``run_kernel_turn`` directly and skipped that step, so a saved
    session could not be replayed by ``/load`` and showed up as "不存在" to
    ``--resume``.
    """

    contract = _write_contract("在 e2e_scratch/plan_probe.txt 写入一行 hello")
    host, _frames = _host()
    host.repl, _captured = _capturing_repl(contract)
    context = _RecordingContext()
    host.repl.ctx_mgr = context
    seen = _fake_kernel(monkeypatch)

    host.run_turn("在 e2e_scratch/plan_probe.txt 写入一行 hello")

    assert len(context.requests) == 1
    content, kwargs = context.requests[0]
    assert "在 e2e_scratch/plan_probe.txt 写入一行 hello" in content
    assert kwargs["metadata"]["original_user_input"] == (
        "在 e2e_scratch/plan_probe.txt 写入一行 hello"
    )
    # The kernel receives the same frozen envelope the interactive path sends.
    assert seen["objective"].startswith("[snapshot]")


def test_a_chat_turn_records_its_request_exactly_once() -> None:
    from xenon.repl.direct_chat import DirectChatOutcome

    host, _frames = _host()
    host.cwd = Path.cwd()
    context = _RecordingContext()
    seen: list[str] = []

    def direct(prompt, *_args, **_kwargs):
        seen.append(prompt)
        return DirectChatOutcome(answer="我在", model_id="test/fake")

    host.repl = types.SimpleNamespace(
        _run_direct=direct,
        _session_events=types.SimpleNamespace(session_id="s1"),
        ctx_mgr=context,
    )
    contract = types.SimpleNamespace(
        to_execution_policy=lambda: types.SimpleNamespace(requires_tools=False)
    )

    host._run_chat("你好", contract)

    assert len(context.requests) == 1
    assert seen == ["[snapshot]\n你好"]


def test_a_cancelled_chat_turn_ends_aborted_not_failed() -> None:
    """Ctrl+C during a plain conversation must be reported as a stop.

    Measured through the real front-end: the reply kept streaming to the end
    and the turn finished as if nothing had happened, because the host's
    cancel signal was bound to the kernel runtime and direct chat has none.
    """

    from xenon.repl.direct_chat import DirectChatOutcome

    host, frames = _host()
    host.cwd = Path.cwd()
    host.repl = types.SimpleNamespace(
        _run_direct=lambda *a, **k: DirectChatOutcome(cancelled=True),
        _session_events=types.SimpleNamespace(session_id="s1"),
    )
    contract = types.SimpleNamespace(
        to_execution_policy=lambda: types.SimpleNamespace(requires_tools=False)
    )

    host._run_chat("写一段两千字的散文，主题是秋天。", contract)

    end = frames.read()[-1]
    assert end["type"] == "turn.end"
    assert end["phase"] == "aborted"
    assert end["cancelled"] is True
    assert end["answer"] == ""
    assert end["ledger"]


def test_a_cancel_is_accepted_for_a_chat_turn_that_has_no_runtime() -> None:
    """The host owns the cancel flag; the runtime is only one consumer."""

    host, _frames = _host()
    host.repl = types.SimpleNamespace()

    assert host.cancel_turn() is False  # nothing running: a no-op

    worker = threading.Thread(target=lambda: time.sleep(0.15), daemon=True)
    worker.start()
    host.turn_thread = worker

    assert host.cancel_turn() is True
    assert host._turn_cancel.is_set()
    worker.join()


def test_a_delegated_chat_turn_reports_the_kernel_run_that_did_the_work() -> None:
    """The direct path escalating to the kernel must not fake a refusal.

    Measured through the real front-end: a chat turn the model answered with a
    tool attempt delegated to the kernel, the kernel delivered an answer in its
    ledger, and the user was shown "模型尝试执行工具；请把这一轮改写成工具任务".
    """

    from xenon.repl.direct_chat import DirectChatOutcome

    host, frames = _host()
    host.cwd = Path.cwd()
    run = types.SimpleNamespace(
        result=types.SimpleNamespace(
            phase=types.SimpleNamespace(value="finalized"),
            answer="42",
            accepted=True,
            evidence_status=types.SimpleNamespace(value="verified"),
        ),
        ledger_path=Path("ledger.jsonl"),
    )
    host.repl = types.SimpleNamespace(
        _run_direct=lambda *args, **kwargs: DirectChatOutcome(
            delegated=True, answer="42", run=run
        ),
        _session_events=types.SimpleNamespace(session_id="s1"),
    )
    contract = types.SimpleNamespace(
        to_execution_policy=lambda: types.SimpleNamespace(requires_tools=False)
    )

    host._run_chat("我刚才让你记住的数字是多少？", contract)

    end = frames.read()[-1]
    assert end["type"] == "turn.end"
    assert end["answer"] == "42"
    assert end["phase"] == "finalized"
    assert end["evidence"] == {"status": "verified"}
    assert end["ledger"] == "ledger.jsonl"
    assert "模型尝试执行工具" not in end.get("error", "")


def test_a_delegated_turn_without_a_run_reports_the_real_cause() -> None:
    from xenon.repl.direct_chat import DirectChatOutcome

    host, frames = _host()
    host.cwd = Path.cwd()
    host.repl = types.SimpleNamespace(
        _run_direct=lambda *args, **kwargs: DirectChatOutcome(delegated=True),
        _last_kernel_error=ConnectionError("connection refused"),
        _session_events=types.SimpleNamespace(session_id="s1"),
    )
    contract = types.SimpleNamespace(
        to_execution_policy=lambda: types.SimpleNamespace(requires_tools=False)
    )

    host._run_chat("你好", contract)

    end = frames.read()[-1]
    assert end["type"] == "turn.end"
    assert end["phase"] == "failed"
    assert end["error_kind"] == "network"
    assert end["retryable"] is True


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


def test_turn_end_uses_the_kernel_answer_when_no_callback_set_one() -> None:
    """The kernel path never calls ``on_finish``; the answer lives on the result.

    Without the fallback the transcript block showed tool steps and evidence but
    no answer, even though the ledger recorded one.
    """

    from xenon.frontend.host import _turn_end_fields
    from xenon.kernel.contracts import EvidenceStatus, TaskPhase

    result = types.SimpleNamespace(
        phase=TaskPhase.FINALIZED,
        answer="README 的第一行是 # ui check",
        accepted=True,
        evidence_status=EvidenceStatus.PARTIAL,
    )
    fields = _turn_end_fields(types.SimpleNamespace(result=result, ledger_path=None), "")
    assert fields["answer"] == "README 的第一行是 # ui check"
    assert fields["phase"] == "finalized"


def test_output_printed_before_a_question_is_flushed_first(monkeypatch) -> None:
    """``/set_model`` printed the table, then asked — the table arrived last."""

    import rich.prompt as rich_prompt

    host, frames = _host()
    host.repl = types.SimpleNamespace(
        registry=object(), ctx_mgr=object(), agent_context={}
    )
    import xenon.repl.command_registry as registry_mod

    def fake_dispatch(*args, **kwargs):
        print("1  anthropic")
        rich_prompt.Prompt.ask("输入模型编号", choices=["1", "2"], default="1")
        return "✅ 已切换"

    monkeypatch.setattr(registry_mod, "dispatch_command", fake_dispatch)

    worker = threading.Thread(target=host.run_command, args=("/set_model", ""))
    worker.start()
    request = _wait_for(frames, "prompt.request")
    kinds = [frame["type"] for frame in frames.read()]
    assert "command.output" in kinds and "prompt.request" in kinds
    assert kinds.index("command.output") < kinds.index("prompt.request")
    flushed = next(f for f in frames.read() if f["type"] == "command.output")
    assert "anthropic" in flushed["output"]
    host.resolve_prompt(request["id"], "1")
    worker.join(timeout=2)


def test_a_model_switch_applies_to_the_next_turn() -> None:
    """``/set_model`` updates ``repl._preferred_model_ids``; the host used the
    startup list forever, so the status bar and the calls stayed on the old
    model."""

    host, _frames = _host()
    host.repl = types.SimpleNamespace(_preferred_model_ids=["anthropic/deepseek-v4.1-flash"])
    assert host._current_models() == ["anthropic/deepseek-v4.1-flash"]

    host.repl._preferred_model_ids = []
    assert host._current_models() == ["test/fake"]


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


def test_restart_command_actually_restarts(monkeypatch) -> None:
    """``/restart`` only set a flag the interactive main loop reads.

    Over the protocol there is no such loop: the command said "正在准备重启"
    and nothing happened.  The host now performs the in-process reload and
    reports its outcome in the same command result.
    """

    class _Coordinator:
        def __init__(self) -> None:
            self.requested = False
            self.cleared = False
            self.preserve = True

        def request_restart(self, preserve: bool = True) -> None:
            self.requested = True
            self.preserve = preserve

        def should_restart(self):
            return self.requested, self.preserve

        def clear(self) -> None:
            self.cleared = True
            self.requested = False

    class _Manager:
        def __init__(self) -> None:
            self.coordinator = _Coordinator()
            self.calls: list[bool] = []

        def perform_restart(self, preserve: bool):
            self.calls.append(preserve)
            return types.SimpleNamespace(message="✅ 重启成功")

    host, frames = _host()
    manager = _Manager()
    host.repl = types.SimpleNamespace(
        registry=object(),
        ctx_mgr=object(),
        agent_context={},
        _restart_manager=manager,
    )

    import xenon.repl.command_registry as registry_mod

    def fake_dispatch(*args, **kwargs):
        kwargs["session_state"]["_repl"]._restart_manager.coordinator.request_restart(
            True
        )
        return "🔄 正在准备重启（保存会话）..."

    monkeypatch.setattr(registry_mod, "dispatch_command", fake_dispatch)

    host.run_command("/restart", "")

    result = frames.read()[-1]
    assert "正在准备重启" in result["output"]
    assert "✅ 重启成功" in result["output"]
    assert manager.calls == [True]
    assert manager.coordinator.cleared


def test_a_command_that_runs_the_kernel_updates_the_panel_view(monkeypatch) -> None:
    """``/plan approve`` executes a kernel run on the command thread.

    The host only recorded the run a *turn* returned, so the dashboard kept
    describing the plan-only run (evidence ``unverified``) minutes after the
    approved plan had delivered a verified commit.  Both entries now publish
    through the REPL's single ``_last_kernel_run`` pointer.
    """

    host, _frames = _host()
    old = types.SimpleNamespace(
        ledger_path=Path("/ledger/old.jsonl"), task_id="old"
    )
    new = types.SimpleNamespace(
        ledger_path=Path("/ledger/new.jsonl"), task_id="new"
    )
    host.last_run = old
    host.last_ledger = old.ledger_path
    host.repl = types.SimpleNamespace(
        registry=object(),
        ctx_mgr=object(),
        agent_context={},
        _last_kernel_run=old,
    )

    import xenon.repl.command_registry as registry_mod

    def fake_dispatch(*args, **kwargs):
        host.repl._last_kernel_run = new
        return "✅ 计划已批准并执行。"

    monkeypatch.setattr(registry_mod, "dispatch_command", fake_dispatch)

    host.run_command("/plan", "approve")

    assert host.last_run is new
    assert host.last_ledger == new.ledger_path


def test_a_resumed_session_ledger_reaches_the_panels(tmp_path) -> None:
    """``/resume`` restores the REPL's ledger pointer, not a kernel run.

    The dashboard kept saying "没有可读的账本" for a session whose terminal
    ``/evidence`` worked.  The restored pointer is the same fact, so the host
    adopts it and drops a run that belongs to a different ledger.
    """

    host, _frames = _host()
    restored = tmp_path / "restored.jsonl"
    restored.write_text("", encoding="utf-8")
    stale = types.SimpleNamespace(
        ledger_path=tmp_path / "old.jsonl", task_id="old", result=object()
    )
    host.last_run = stale
    host.last_ledger = stale.ledger_path
    host.repl = types.SimpleNamespace(_last_kernel_ledger=str(restored))

    host._sync_kernel_view(stale)

    assert host.last_ledger == restored
    assert host.last_run is None


def test_a_restored_pointer_to_the_same_ledger_keeps_the_run(tmp_path) -> None:
    host, _frames = _host()
    ledger = tmp_path / "current.jsonl"
    ledger.write_text("", encoding="utf-8")
    run = types.SimpleNamespace(ledger_path=ledger, task_id="current")
    host.last_run = run
    host.last_ledger = ledger
    host.repl = types.SimpleNamespace(
        _last_kernel_run=run, _last_kernel_ledger=str(ledger)
    )

    host._sync_kernel_view(run)

    assert host.last_run is run
    assert host.last_ledger == ledger


def test_a_command_without_a_new_run_keeps_the_panel_view(monkeypatch) -> None:
    """A plain command must not resurrect a run the last turn did not finish."""

    host, _frames = _host()
    old = types.SimpleNamespace(
        ledger_path=Path("/ledger/old.jsonl"), task_id="old"
    )
    host.last_run = None
    host.last_ledger = None
    host.repl = types.SimpleNamespace(
        registry=object(),
        ctx_mgr=object(),
        agent_context={},
        _last_kernel_run=old,
    )

    import xenon.repl.command_registry as registry_mod

    monkeypatch.setattr(
        registry_mod, "dispatch_command", lambda *a, **k: "状态正常"
    )

    host.run_command("/status", "")

    assert host.last_run is None
    assert host.last_ledger is None


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


# ── turn authorisation: one resolver, and plan mode is a constraint ──


def _write_contract(text: str):
    from xenon.repl.turn_contract import build_turn_contract

    return build_turn_contract(text)


def _capturing_repl(
    contract, *, plan_mode: bool = False, permission_mode: str = ""
):
    import xenon.repl.repl as repl_mod

    captured: dict[str, int] = {}
    modes = {"_permission_mode": permission_mode}
    agent_context = types.SimpleNamespace(
        set=lambda key, value: captured.__setitem__(key, value),
        get=lambda key: modes.get(key),
    )
    repl = types.SimpleNamespace(
        _preferred_model_ids=["test/fake"],
        _protocol_callback=None,
        agent_context=agent_context,
        _plan_mode_active=plan_mode,
        _plan_mode_contract=repl_mod.REPL._plan_mode_contract,
        _resolve_turn_contract=lambda objective: (
            contract,
            contract.to_execution_policy(),
            "write_code",
            "llm",
        ),
        _session_events=types.SimpleNamespace(session_id="s1"),
    )
    return repl, captured


def _fake_kernel(monkeypatch: pytest.MonkeyPatch) -> dict:
    import xenon.repl.runtime_adapter as adapter_mod

    seen: dict = {}

    def fake(repl, **kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(result=None)

    monkeypatch.setattr(adapter_mod, "run_kernel_turn", fake)
    return seen


def test_run_turn_uses_the_single_contract_resolver(monkeypatch) -> None:
    """The protocol must not classify a second time with weaker signals.

    Measured: "在 x.txt 写入一行 hello" has its write verb *after* the path,
    so the regex-only fallback said read-only while the same text in the
    Python TUI (which consults the LLM classifier) was authorised.  Every
    write through the front-end was therefore vetoed.
    """

    contract = _write_contract("在 e2e_scratch/plan_probe.txt 写入一行 hello")
    assert contract.level.name == "WRITE"
    host, _frames = _host()
    host.repl, captured = _capturing_repl(contract)
    seen = _fake_kernel(monkeypatch)

    host.run_turn("在 e2e_scratch/plan_probe.txt 写入一行 hello")

    assert seen["execution_policy"].allows_write
    assert seen["plan_only"] is False
    assert seen["intent"] == "write_code"
    assert captured["_execution_level"] == int(contract.level)


def test_run_turn_honours_plan_mode_and_reports_it_to_the_kernel(
    monkeypatch,
) -> None:
    """``/plan on`` is a constraint the protocol host used to drop entirely.

    Through the front-end the kernel started in TaskMode.AUTO with every
    tool available; the same command in the Python TUI produced a PLANNED
    turn.  The host now converts the contract and sets ``plan_only``.
    """

    from xenon.repl.execution_policy import ExecutionLevel

    contract = _write_contract("在 e2e_scratch/plan_probe.txt 写入一行 hello")
    host, _frames = _host()
    host.repl, captured = _capturing_repl(contract, plan_mode=True)
    seen = _fake_kernel(monkeypatch)

    host.run_turn("在 e2e_scratch/plan_probe.txt 写入一行 hello")

    assert seen["plan_only"] is True
    assert seen["execution_policy"].level is ExecutionLevel.READ_ONLY
    # The plan may still *describe* the write; the session keeps the real level.
    assert captured["_execution_level"] == int(ExecutionLevel.WRITE)
    assert "计划模式（kernel plan-only）" in seen["objective"]
    assert seen["user_input"] == "在 e2e_scratch/plan_probe.txt 写入一行 hello"


def test_run_turn_clamps_to_the_permission_mode_ceiling(monkeypatch) -> None:
    """``/permissions plan`` is a durable ceiling, not an interactive-only one.

    ``run_turn`` recomputed the contract from the objective every turn, so the
    mode stored by ``/permissions`` was overwritten before it could constrain
    anything: through the front-end the same write the mode should refuse was
    authorised.  The ceiling is now applied at this boundary too.
    """

    from xenon.repl.execution_policy import ExecutionLevel

    contract = _write_contract("在 e2e_scratch/plan_probe.txt 写入一行 hello")
    assert contract.level is ExecutionLevel.WRITE

    host, _frames = _host()
    host.repl, captured = _capturing_repl(contract, permission_mode="plan")
    seen = _fake_kernel(monkeypatch)

    host.run_turn("在 e2e_scratch/plan_probe.txt 写入一行 hello")

    assert seen["execution_policy"].level is ExecutionLevel.READ_ONLY
    assert not seen["execution_policy"].allows_write
    assert captured["_execution_level"] == int(ExecutionLevel.READ_ONLY)


def test_the_permission_ceiling_only_lowers(monkeypatch) -> None:
    """``bypass`` does not raise an answer-only turn; it only lifts the cap."""

    from xenon.repl.execution_policy import ExecutionLevel

    contract = _write_contract("在 e2e_scratch/plan_probe.txt 写入一行 hello")
    assert contract.level is ExecutionLevel.WRITE

    host, _frames = _host()
    host.repl, _captured = _capturing_repl(contract, permission_mode="bypass")
    seen = _fake_kernel(monkeypatch)

    host.run_turn("在 e2e_scratch/plan_probe.txt 写入一行 hello")

    assert seen["execution_policy"].level is ExecutionLevel.WRITE


def test_a_failed_kernel_turn_reports_its_real_cause(monkeypatch) -> None:
    """A generic "kernel turn failed" hides every actionable cause.

    ``run_kernel_turn`` prints the exception to the console and returns
    ``None``; through the protocol host that console is not the user's stream,
    so a measured MCP run failed on a dead network and said nothing about it.
    The cause now travels back on the REPL and is classified at the boundary.
    """

    import xenon.repl.runtime_adapter as adapter_mod

    contract = _write_contract("在 e2e_scratch/plan_probe.txt 写入一行 hello")
    host, frames = _host()
    host.repl, _captured = _capturing_repl(contract)

    def fake(repl, **kwargs):
        repl._last_kernel_error = ConnectionError(
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
        )
        return None

    monkeypatch.setattr(adapter_mod, "run_kernel_turn", fake)

    host.run_turn("在 e2e_scratch/plan_probe.txt 写入一行 hello")

    end = [f for f in frames.read() if f.get("type") == "turn.end"][-1]
    assert end["phase"] == "failed"
    assert "UNEXPECTED_EOF" in end["error"]
    assert end["error_kind"] == "network"
    assert end["retryable"] is True


def test_a_failed_kernel_turn_without_a_cause_stays_honest(monkeypatch) -> None:
    import xenon.repl.runtime_adapter as adapter_mod

    contract = _write_contract("在 e2e_scratch/plan_probe.txt 写入一行 hello")
    host, frames = _host()
    host.repl, _captured = _capturing_repl(contract)
    host.final_answer = ""

    monkeypatch.setattr(
        adapter_mod, "run_kernel_turn", lambda repl, **kwargs: None
    )

    host.run_turn("在 e2e_scratch/plan_probe.txt 写入一行 hello")

    end = [f for f in frames.read() if f.get("type") == "turn.end"][-1]
    assert end["phase"] == "failed"
    assert end["error"] == "kernel turn failed"
    assert end["error_kind"] == "internal"


def test_the_host_runs_the_same_extension_bootstrap_as_the_repl() -> None:
    """Skipping it left persisted MCP servers and dynamic commands invisible.

    ``REPL.run`` loads dynamic commands and registers saved MCP servers before
    the loop; the protocol host built the REPL but never ran that step, so a
    restart lost every MCP tool and ``/mcp list`` was empty.
    """

    host, _frames = _host()
    calls: list[str] = []
    host.repl = types.SimpleNamespace(
        bootstrap_extensions=lambda: calls.append("boot")
    )

    host._bootstrap_extensions()

    assert calls == ["boot"]


def test_a_failing_extension_bootstrap_never_blocks_boot() -> None:
    host, _frames = _host()

    def boom() -> None:
        raise RuntimeError("bad extension")

    host.repl = types.SimpleNamespace(bootstrap_extensions=boom)

    host._bootstrap_extensions()  # must not raise
