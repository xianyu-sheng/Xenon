"""Memory v2: governed writes, layered storage, retrieval, and UX contracts."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO

import pytest
from rich.console import Console

from xenon.memory import (
    MemoryBackendRegistry,
    MemoryCandidateDetector,
    MemoryContextCompiler,
    MemoryKind,
    MemoryPolicy,
    MemoryRecord,
    MemoryScope,
    MemoryService,
    MemoryStatus,
)


def make_service(tmp_path, *, policy: MemoryPolicy | None = None) -> MemoryService:
    project = tmp_path / "project"
    project.mkdir()
    return MemoryService(
        MemoryBackendRegistry(
            project,
            user_data_root=tmp_path / "user-memory",
            user_config_root=tmp_path / "user-config",
        ),
        policy=policy,
    )


def test_project_local_write_has_json_markdown_receipt_and_private_mode(tmp_path):
    service = make_service(tmp_path)

    receipt = service.remember(
        "项目默认使用 Python 3.12",
        scope=MemoryScope.PROJECT_LOCAL,
        kind=MemoryKind.FACT,
    )

    destination = service.registry.project_root / ".xenon/memory/local/project.md"
    metadata = destination.parent / "metadata.json"
    assert receipt.destination == str(destination)
    assert receipt.created is True
    assert receipt.record.id in destination.read_text(encoding="utf-8")
    assert json.loads(metadata.read_text(encoding="utf-8"))["items"][0]["content"] == receipt.record.content
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata.parent.stat().st_mode) == 0o700
    assert "@.xenon/memory/local/INDEX.md" in (
        service.registry.project_root / "XENON.local.md"
    ).read_text(encoding="utf-8")


def test_project_local_write_adds_git_info_excludes(tmp_path):
    service = make_service(tmp_path)
    git_info = service.registry.project_root / ".git/info"
    git_info.mkdir(parents=True)

    service.remember("本机使用 uv", scope=MemoryScope.PROJECT_LOCAL)

    exclude = (git_info / "exclude").read_text(encoding="utf-8")
    assert "/XENON.local.md" in exclude
    assert "/.xenon/memory/local/" in exclude


def test_shared_memory_is_explicit_scope_and_not_private(tmp_path):
    service = make_service(tmp_path)
    receipt = service.remember(
        "合并前必须运行完整测试",
        scope=MemoryScope.PROJECT_SHARED,
        kind=MemoryKind.CONSTRAINT,
    )

    path = service.registry.project_root / ".xenon/memory/shared/conventions.md"
    assert receipt.destination == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert "@.xenon/memory/shared/INDEX.md" in (
        service.registry.project_root / "XENON.md"
    ).read_text(encoding="utf-8")


def test_exact_duplicate_updates_one_record(tmp_path):
    service = make_service(tmp_path)
    first = service.remember("默认使用 Ruff", tags=["lint"])
    second = service.remember("  默认使用   ruff ", tags=["python"])

    assert second.created is False
    assert second.record.id == first.record.id
    assert second.record.tags == ["lint", "python"]
    assert len(service.list_records(scope=MemoryScope.PROJECT_LOCAL)) == 1


def test_threshold_archives_low_value_old_record_but_protects_new_write(tmp_path):
    service = make_service(
        tmp_path,
        policy=MemoryPolicy(max_item_tokens=100, max_leaf_tokens=18, max_active_tokens=100),
    )
    old = service.remember("旧的项目背景信息一二三四五六", importance=0.1)
    new = service.remember("新的项目背景信息七八九十十一", importance=0.9)

    all_records = service.list_records(
        scope=MemoryScope.PROJECT_LOCAL,
        include_archived=True,
    )
    assert next(item for item in all_records if item.id == old.record.id).status == MemoryStatus.ARCHIVED
    assert next(item for item in all_records if item.id == new.record.id).status == MemoryStatus.ACTIVE
    assert old.record.id in new.archived_ids
    assert (service.registry.get(MemoryScope.PROJECT_LOCAL).root / "archive.jsonl").exists()


def test_pinned_memory_is_never_automatically_archived(tmp_path):
    service = make_service(
        tmp_path,
        policy=MemoryPolicy(max_item_tokens=100, max_leaf_tokens=10, max_active_tokens=100),
    )
    pinned = service.remember("必须保留的固定约束", pinned=True)
    receipt = service.remember("本轮新增的重要事实")

    assert pinned.record.status == MemoryStatus.ACTIVE
    assert service.list_records(scope=MemoryScope.PROJECT_LOCAL)
    assert receipt.warning is not None


def test_shared_scope_warns_instead_of_auto_archiving(tmp_path):
    service = make_service(
        tmp_path,
        policy=MemoryPolicy(max_item_tokens=100, max_leaf_tokens=10, max_active_tokens=100),
    )
    service.remember("团队共享约束第一条", scope=MemoryScope.PROJECT_SHARED)
    receipt = service.remember("团队共享约束第二条", scope=MemoryScope.PROJECT_SHARED)

    assert len(service.list_records(scope=MemoryScope.PROJECT_SHARED)) == 2
    assert "不会被自动归档" in (receipt.warning or "")


def test_archive_is_reversible(tmp_path):
    service = make_service(tmp_path)
    memory_id = service.remember("可恢复的记忆").record.id

    assert service.archive(memory_id) is True
    assert service.list_records(scope=MemoryScope.PROJECT_LOCAL) == []
    assert service.restore(memory_id) is True
    assert service.list_records(scope=MemoryScope.PROJECT_LOCAL)[0].id == memory_id


def test_retrieval_is_cross_scope_bounded_and_persists_access_count(tmp_path):
    service = make_service(tmp_path)
    local = service.remember("数据库固定使用 PostgreSQL", scope=MemoryScope.PROJECT_LOCAL)
    service.remember("我偏好简洁回答", scope=MemoryScope.USER, kind=MemoryKind.PREFERENCE)

    selected = service.retrieve("帮我设计 PostgreSQL 数据库", token_budget=100, limit=3)
    reloaded = service.list_records(scope=MemoryScope.PROJECT_LOCAL)[0]

    assert [item.id for item in selected] == [local.record.id]
    assert reloaded.retrieval_count == 1
    assert reloaded.last_retrieved_at is not None
    assert "project-local" in service.format_for_context(selected, context_window=1000)


def test_session_memory_never_writes_disk(tmp_path):
    service = make_service(tmp_path)
    receipt = service.remember("只在当前会话使用", scope=MemoryScope.SESSION)

    assert receipt.destination == "当前会话（不写入磁盘）"
    assert service.list_records(scope=MemoryScope.SESSION)[0].content == "只在当前会话使用"
    assert not (tmp_path / "user-memory").exists()


def test_user_memory_creates_transparent_global_index_pointer(tmp_path):
    service = make_service(tmp_path)
    receipt = service.remember("我偏好简洁回答", scope=MemoryScope.USER)

    entrypoint = tmp_path / "user-config/XENON.md"
    assert str((tmp_path / "user-memory/INDEX.md")) in entrypoint.read_text(encoding="utf-8")
    assert receipt.destination.endswith("project.md")
    assert stat.S_IMODE(entrypoint.stat().st_mode) == 0o600


def test_secret_is_rejected_by_policy(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="密钥"):
        service.remember("api_key = sk-abcdefghijklmnop")
    with pytest.raises(ValueError, match="密钥"):
        service.remember("部署密码：super-secret-value")


def test_corrupt_metadata_is_not_silently_overwritten(tmp_path):
    service = make_service(tmp_path)
    metadata = service.registry.get(MemoryScope.PROJECT_LOCAL).root / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="无法读取记忆元数据"):
        service.remember("不能覆盖损坏文件")

    assert metadata.read_text(encoding="utf-8") == "{broken"


def test_auxiliary_entrypoint_failure_returns_warning_after_successful_write(
    monkeypatch, tmp_path
):
    service = make_service(tmp_path)

    def fail_entrypoint(scope):
        raise OSError("read-only instructions")

    monkeypatch.setattr(service, "_ensure_scope_entrypoint", fail_entrypoint)
    receipt = service.remember("数据文件仍应成功写入")

    assert "顶层索引入口更新失败" in (receipt.warning or "")
    assert service.list_records()[0].content == "数据文件仍应成功写入"


@pytest.mark.parametrize(
    ("text", "scope", "kind", "content"),
    [
        ("把“项目默认使用 Python 3.12”存入我的项目本地记忆", MemoryScope.PROJECT_LOCAL, MemoryKind.FACT, "项目默认使用 Python 3.12"),
        ("请记住：我偏好简洁输出", MemoryScope.PROJECT_LOCAL, MemoryKind.PREFERENCE, "我偏好简洁输出"),
        ("将团队必须跑 pytest 写入项目共享记忆", MemoryScope.PROJECT_SHARED, MemoryKind.CONSTRAINT, "团队必须跑 pytest"),
        ("remember this project uses uv", MemoryScope.PROJECT_LOCAL, MemoryKind.FACT, "this project uses uv"),
    ],
)
def test_explicit_request_parser(text, scope, kind, content):
    proposal = MemoryCandidateDetector().parse_explicit(text)
    assert proposal is not None
    assert proposal.explicit is True
    assert (proposal.scope, proposal.kind, proposal.content) == (scope, kind, content)


def test_explicit_previous_turn_reference_parser():
    proposal = MemoryCandidateDetector().parse_reference(
        "这一条帮我存入我的项目本地记忆"
    )
    assert proposal is not None
    assert proposal.scope == MemoryScope.PROJECT_LOCAL
    assert proposal.content == ""


def test_automatic_candidate_defaults_local_and_never_persists(tmp_path):
    detector = MemoryCandidateDetector()
    proposal = detector.propose("这个项目以后始终使用 uv 管理依赖")

    assert proposal is not None
    assert proposal.explicit is False
    assert proposal.scope == MemoryScope.PROJECT_LOCAL
    assert not (tmp_path / "anything").exists()


def test_automatic_candidate_rejects_questions_and_secrets():
    detector = MemoryCandidateDetector()
    assert detector.propose("这个项目以后始终使用 uv 吗？") is None
    assert detector.propose("以后始终使用 api_key=sk-abcdefghijklmnop") is None


def test_context_compiler_enforces_budget_and_exposes_provenance():
    records = [
        MemoryRecord(content=f"项目事实 {index} " + "很长" * 10)
        for index in range(5)
    ]
    output = MemoryContextCompiler().compile(records, token_budget=80)

    assert "id=" in output
    assert output.count("项目事实") < 5


def test_shared_constraint_is_retrieved_as_deterministic_rule(tmp_path):
    service = make_service(tmp_path)
    shared = service.remember(
        "提交前必须通过 pytest",
        scope=MemoryScope.PROJECT_SHARED,
        kind=MemoryKind.CONSTRAINT,
    )

    selected = service.retrieve("请修改 README", limit=3)

    assert shared.record.id in [item.id for item in selected]


def test_repl_explicit_memory_request_writes_and_shows_receipt(monkeypatch, tmp_path):
    from xenon.repl import repl as repl_module

    service = make_service(tmp_path)
    output = StringIO()
    monkeypatch.setattr(repl_module, "console", Console(file=output, force_terminal=False))
    repl = repl_module.REPL.__new__(repl_module.REPL)
    repl._memory_detector = MemoryCandidateDetector()
    repl._memory_service = service

    handled = repl._handle_explicit_memory_request("请记住：我偏好简洁输出")

    assert handled is True
    assert service.list_records()[0].content == "我偏好简洁输出"
    rendered = output.getvalue()
    assert "记忆回执" in rendered
    assert "project-local" in rendered
    assert "preferences.md" in rendered


def test_repl_can_store_the_previous_turn_by_reference(monkeypatch, tmp_path):
    from xenon.repl import repl as repl_module
    from xenon.repl.context_manager import ContextManager

    service = make_service(tmp_path)
    output = StringIO()
    monkeypatch.setattr(repl_module, "console", Console(file=output, force_terminal=False))
    repl = repl_module.REPL.__new__(repl_module.REPL)
    repl._memory_detector = MemoryCandidateDetector()
    repl._memory_service = service
    repl.ctx_mgr = ContextManager()
    repl.ctx_mgr.add_assistant_message("项目架构采用接口驱动加注册表模式")

    handled = repl._handle_explicit_memory_request(
        "这一条帮我存入我的项目本地记忆"
    )

    assert handled is True
    assert service.list_records()[0].content == "项目架构采用接口驱动加注册表模式"


def test_repl_automatic_candidate_requires_real_prompt_even_with_assume_yes(
    monkeypatch, tmp_path
):
    from xenon.repl import repl as repl_module

    class TTY:
        @staticmethod
        def isatty():
            return True

    service = make_service(tmp_path)
    repl = repl_module.REPL.__new__(repl_module.REPL)
    repl._memory_detector = MemoryCandidateDetector()
    repl._memory_service = service
    monkeypatch.setattr(sys, "stdin", TTY())
    monkeypatch.setenv("XENON_ASSUME_YES", "1")
    monkeypatch.setattr(repl_module.Prompt, "ask", lambda *args, **kwargs: "n")

    repl._maybe_suggest_memory("这个项目以后始终使用 uv 管理依赖")

    assert service.list_records() == []

    monkeypatch.setattr(repl_module.Prompt, "ask", lambda *args, **kwargs: "s")
    repl._maybe_suggest_memory("这个项目以后始终使用 uv 管理依赖")
    assert service.list_records()[0].content == "这个项目以后始终使用 uv 管理依赖"


def test_memory_command_uses_v2_archive_and_restore(tmp_path):
    from xenon.repl.commands import _cmd_memory

    service = make_service(tmp_path)

    class FakeRepl:
        @staticmethod
        def _get_memory_service():
            return service

    session = {"_repl": FakeRepl()}
    status = _cmd_memory(args="status", session_state=session)
    added = _cmd_memory(
        args="add 默认使用 uv --kind preference --scope project-local",
        session_state=session,
    )
    memory_id = service.list_records()[0].id
    archived = _cmd_memory(args=f"archive {memory_id}", session_state=session)
    restored = _cmd_memory(args=f"restore {memory_id}", session_state=session)

    assert "metadata.json" in status
    assert str(tmp_path / "user-memory") in status
    assert "已添加" in added
    assert "已归档" in archived
    assert "已恢复" in restored


def test_memory_command_migrates_v1_without_deleting_source(monkeypatch, tmp_path):
    from xenon.repl import memory as legacy_module
    from xenon.repl.commands import _cmd_memory
    from xenon.repl.memory import MemoryStore

    legacy_path = tmp_path / "legacy-memory.json"
    monkeypatch.setattr(legacy_module, "_MEMORY_PATH", legacy_path)
    MemoryStore().add("旧版用户偏好", type="preference")
    service = make_service(tmp_path)

    class FakeRepl:
        @staticmethod
        def _get_memory_service():
            return service

    result = _cmd_memory(
        args="migrate --scope user",
        session_state={"_repl": FakeRepl()},
    )

    assert "新增 1" in result
    assert service.list_records(scope=MemoryScope.USER)[0].content == "旧版用户偏好"
    assert legacy_path.exists()


def test_potential_conflict_is_reported_without_silent_overwrite(tmp_path):
    service = make_service(tmp_path)
    old = service.remember("项目默认使用 Python 3.12")
    new = service.remember("项目默认使用 Python 3.13")

    assert new.conflict_ids == [old.record.id]
    active = service.list_records(scope=MemoryScope.PROJECT_LOCAL)
    assert {item.id for item in active} == {old.record.id, new.record.id}
    assert all(item.status == MemoryStatus.ACTIVE for item in active)


def test_explicit_replace_and_rollback_preserve_version_chain(tmp_path):
    service = make_service(tmp_path)
    old = service.remember("项目默认使用 Python 3.12")

    replacement = service.replace(old.record.id, "项目默认使用 Python 3.13")
    records = service.list_records(
        scope=MemoryScope.PROJECT_LOCAL,
        include_archived=True,
    )
    previous = next(item for item in records if item.id == old.record.id)
    current = next(item for item in records if item.id == replacement.record.id)
    assert previous.status == MemoryStatus.SUPERSEDED
    assert current.status == MemoryStatus.ACTIVE
    assert current.supersedes == previous.id

    assert service.rollback(current.id) is True
    records = service.list_records(
        scope=MemoryScope.PROJECT_LOCAL,
        include_archived=True,
    )
    assert next(item for item in records if item.id == previous.id).status == MemoryStatus.ACTIVE
    assert next(item for item in records if item.id == current.id).status == MemoryStatus.ARCHIVED


def test_explain_retrieval_does_not_touch_but_retrieve_does(tmp_path):
    service = make_service(tmp_path)
    receipt = service.remember("数据库固定使用 PostgreSQL")

    matches = service.explain_retrieval("设计 PostgreSQL 数据库")
    assert matches[0].record.id == receipt.record.id
    assert matches[0].score > 0
    assert any("关键词" in reason for reason in matches[0].reasons)
    assert service.get(receipt.record.id).retrieval_count == 0

    service.retrieve("设计 PostgreSQL 数据库")
    assert service.get(receipt.record.id).retrieval_count == 1


def test_use_count_is_committed_separately_from_retrieval(tmp_path):
    service = make_service(tmp_path)
    receipt = service.remember("数据库固定使用 PostgreSQL")
    selected = service.retrieve("PostgreSQL 数据库")

    assert service.get(receipt.record.id).use_count == 0
    assert service.mark_used([item.id for item in selected]) == 1
    record = service.get(receipt.record.id)
    assert record.use_count == 1
    assert record.last_used_at is not None


def test_concurrent_services_do_not_lose_json_updates(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    services = [
        MemoryService(
            MemoryBackendRegistry(
                project,
                user_data_root=tmp_path / "user-memory",
                user_config_root=tmp_path / "user-config",
            )
        )
        for _ in range(8)
    ]
    barrier = threading.Barrier(len(services))

    def write(index):
        barrier.wait()
        return services[index].remember(f"并发写入的独立事实 {index}").record.id

    with ThreadPoolExecutor(max_workers=len(services)) as executor:
        ids = list(executor.map(write, range(len(services))))

    records = services[0].list_records(scope=MemoryScope.PROJECT_LOCAL)
    assert len(records) == len(services)
    assert {item.id for item in records} == set(ids)


def test_concurrent_processes_do_not_lose_json_updates(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    code = """
import sys
from pathlib import Path
from xenon.memory import MemoryBackendRegistry, MemoryService

project, user_root, config_root, index = map(Path, sys.argv[1:])
service = MemoryService(MemoryBackendRegistry(
    project,
    user_data_root=user_root,
    user_config_root=config_root,
))
service.remember(f"跨进程写入的独立事实 {index.name}")
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(project),
                str(tmp_path / "user-memory"),
                str(tmp_path / "user-config"),
                str(tmp_path / str(index)),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(6)
    ]
    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        if process.returncode:
            failures.append((process.returncode, stdout, stderr))

    assert failures == []
    service = MemoryService(
        MemoryBackendRegistry(
            project,
            user_data_root=tmp_path / "user-memory",
            user_config_root=tmp_path / "user-config",
        )
    )
    assert len(service.list_records(scope=MemoryScope.PROJECT_LOCAL)) == 6


def test_doctor_reports_checksum_corruption_without_overwriting(tmp_path):
    service = make_service(tmp_path)
    receipt = service.remember("需要校验的项目事实")
    backend = service.registry.get(MemoryScope.PROJECT_LOCAL)
    payload = json.loads(backend.metadata_path.read_text(encoding="utf-8"))
    payload["items"][0]["checksum"] = "broken-checksum"
    backend.metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    report = service.diagnose()

    assert report.healthy is False
    assert any(
        issue.memory_id == receipt.record.id and "校验和" in issue.message
        for issue in report.issues
    )
    assert "broken-checksum" in backend.metadata_path.read_text(encoding="utf-8")


def test_doctor_contains_malformed_scope_as_error_instead_of_crashing(tmp_path):
    service = make_service(tmp_path)
    backend = service.registry.get(MemoryScope.USER)
    backend.root.mkdir(parents=True)
    backend.metadata_path.write_text("{broken", encoding="utf-8")

    report = service.diagnose()

    assert report.healthy is False
    assert any(
        issue.scope == MemoryScope.USER and issue.severity == "error"
        for issue in report.issues
    )


def test_retrieval_degrades_per_scope_when_an_unrelated_scope_is_corrupt(tmp_path):
    service = make_service(tmp_path)
    local = service.remember("数据库固定使用 PostgreSQL")
    user_backend = service.registry.get(MemoryScope.USER)
    user_backend.root.mkdir(parents=True)
    user_backend.metadata_path.write_text("{broken", encoding="utf-8")

    selected = service.retrieve("PostgreSQL 数据库")

    assert [record.id for record in selected] == [local.record.id]


def test_restore_after_rollback_keeps_only_one_active_version(tmp_path):
    service = make_service(tmp_path)
    previous = service.remember("项目默认使用 Python 3.12")
    current = service.replace(previous.record.id, "项目默认使用 Python 3.13")
    assert service.rollback(current.record.id) is True

    assert service.restore(current.record.id) is True
    records = service.list_records(
        scope=MemoryScope.PROJECT_LOCAL,
        include_archived=True,
    )
    assert next(item for item in records if item.id == previous.record.id).status == MemoryStatus.SUPERSEDED
    assert next(item for item in records if item.id == current.record.id).status == MemoryStatus.ACTIVE


def test_memory_inspector_replace_pin_and_doctor_commands(tmp_path):
    from xenon.repl.commands import _cmd_memory

    service = make_service(tmp_path)

    class FakeRepl:
        @staticmethod
        def _get_memory_service():
            return service

    session = {"_repl": FakeRepl()}
    added = _cmd_memory(
        args="add 项目默认使用 Python 3.12",
        session_state=session,
    )
    memory_id = service.list_records()[0].id
    inspected = _cmd_memory(args=f"inspect {memory_id}", session_state=session)
    pinned = _cmd_memory(args=f"pin {memory_id}", session_state=session)
    replaced = _cmd_memory(
        args=f"replace {memory_id} 项目默认使用 Python 3.13",
        session_state=session,
    )
    replacement_id = service.list_records()[0].id
    rolled_back = _cmd_memory(
        args=f"rollback {replacement_id}", session_state=session
    )
    doctor = _cmd_memory(args="doctor", session_state=session)

    assert "已添加" in added
    assert "最近检索" in inspected and "校验和" in inspected
    assert "已固定" in pinned
    assert "已用" in replaced and "撤销替代" in replaced
    assert "前一版本已恢复" in rolled_back
    assert "Memory doctor" in doctor


def test_repl_commits_use_only_after_successful_answer(tmp_path):
    from xenon.repl import repl as repl_module
    from xenon.repl.context_manager import ContextManager

    service = make_service(tmp_path)
    memory_id = service.remember("数据库固定使用 PostgreSQL").record.id
    repl = repl_module.REPL.__new__(repl_module.REPL)
    repl._memory_service = service
    repl.ctx_mgr = ContextManager()
    repl.ctx_mgr.add_user_message("数据库是什么")
    repl.ctx_mgr.add_assistant_message("使用 PostgreSQL")
    repl._pending_memory_use_ids = [memory_id]

    repl._commit_memory_usage()
    assert service.get(memory_id).use_count == 1

    repl.ctx_mgr.add_user_message("再试一次")
    repl.ctx_mgr.add_assistant_message("[错误] 调用失败")
    repl._pending_memory_use_ids = [memory_id]
    repl._commit_memory_usage()
    assert service.get(memory_id).use_count == 1
