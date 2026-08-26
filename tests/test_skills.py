"""
Skill Manager 测试。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xenon.repl.skill_manager import SkillManager, SkillStep


def _write_agent_skill(
    skills_root: Path,
    name: str,
    *,
    description: str = "A standard test skill",
    body: str = "# Instructions\nDo the test task.",
    version: str = "1.0.0",
) -> Path:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"version: {version}\n"
        "metadata:\n"
        "  requires:\n"
        "    bins: [python]\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


class TestSkillManager:
    """测试技能管理器。"""

    def test_create_and_list(self):
        """测试创建和列出技能。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create(
                "test",
                "测试技能",
                [
                    {"type": "echo", "prompt": "hello"},
                ],
            )

            assert len(manager.skills) == 1
            assert "test" in manager.skills

    def test_remove(self):
        """测试删除技能。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("test", "测试", [{"type": "echo", "prompt": "ok"}])

            assert manager.remove("test") is True
            assert len(manager.skills) == 0

    def test_remove_nonexistent(self):
        """测试删除不存在的技能。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            assert manager.remove("nonexistent") is False

    def test_persistence(self):
        """测试持久化。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            manager1 = SkillManager(skills_dir)
            manager1.create("persist", "持久化", [{"type": "echo", "prompt": "ok"}])

            manager2 = SkillManager(skills_dir)
            assert "persist" in manager2.skills
            assert manager2.skills["persist"].description == "持久化"

    def test_execute_echo(self):
        """测试执行 echo 步骤。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create(
                "hello",
                "打招呼",
                [
                    {"type": "echo", "prompt": "hello world"},
                ],
            )

            result = manager.execute("hello", "")
            assert "hello world" in result

    def test_execute_command(self):
        """测试执行 command 步骤。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create(
                "cmd",
                "命令测试",
                [
                    {"type": "command", "action": "echo test"},
                ],
            )

            result = manager.execute("cmd", "")
            assert "test" in result

    def test_execute_with_params(self):
        """测试带参数执行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create(
                "greet",
                "问候",
                [
                    {"type": "echo", "prompt": "hello {name}"},
                ],
                params=[{"name": "name", "default": "world"}],
            )

            result = manager.execute("greet", "Alice")
            assert "hello Alice" in result

    def test_execute_nonexistent(self):
        """测试执行不存在的技能。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            result = manager.execute("nonexistent", "")
            assert "不存在" in result

    def test_multiple_steps(self):
        """测试多步骤执行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create(
                "multi",
                "多步骤",
                [
                    {"type": "echo", "prompt": "step1", "output_var": "r1"},
                    {"type": "echo", "prompt": "step2"},
                ],
            )

            result = manager.execute("multi", "")
            assert "step1" in result
            assert "step2" in result

    def test_output_var(self):
        """测试输出变量。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create(
                "vars",
                "变量测试",
                [
                    {"type": "echo", "prompt": "hello", "output_var": "greeting"},
                    {"type": "echo", "prompt": "result: {greeting}"},
                ],
            )

            result = manager.execute("vars", "")
            assert "result: hello" in result

    def test_write_and_read_file(self):
        """测试文件读写步骤。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            test_file = Path(tmpdir) / "test.txt"

            manager.create(
                "filer",
                "文件操作",
                [
                    {
                        "type": "write_file",
                        "file_path": str(test_file),
                        "content": "file content",
                    },
                    {
                        "type": "read_file",
                        "file_path": str(test_file),
                        "output_var": "data",
                    },
                    {"type": "echo", "prompt": "read: {data}"},
                ],
            )

            result = manager.execute("filer", "")
            assert "file content" in result

    def test_list_all(self):
        """测试列出所有。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SkillManager(Path(tmpdir) / "skills")
            manager.create("a", "A", [{"type": "echo", "prompt": "a"}])
            manager.create("b", "B", [{"type": "echo", "prompt": "b"}])

            all_skills = manager.list_all()
            assert len(all_skills) == 2

    def test_skill_step_dataclass(self):
        """测试 SkillStep 数据类。"""
        step = SkillStep(type="llm", prompt="test", output_var="out")
        assert step.type == "llm"
        assert step.prompt == "test"
        assert step.output_var == "out"

    # ── v0.5.4: 新增功能测试 ──

    def test_fuzzy_match_subcommand(self):
        """测试子命令模糊匹配。"""
        from xenon.repl.commands import _fuzzy_match_subcommand

        assert _fuzzy_match_subcommand("creat") == "create"
        assert _fuzzy_match_subcommand("crate") == "create"
        assert _fuzzy_match_subcommand("lst") == "list"
        assert _fuzzy_match_subcommand("del") == "delete"
        assert _fuzzy_match_subcommand("rm") == "delete"
        assert _fuzzy_match_subcommand("exec") == "run"
        assert _fuzzy_match_subcommand("install") == "import"
        assert _fuzzy_match_subcommand("fetch") == "import"
        # 完全不匹配的应返回 None
        assert _fuzzy_match_subcommand("xyzabc123") is None

    def test_skill_group_preserves_legacy_command_exports(self):
        from xenon.repl.command_groups.skill import (
            _cmd_skill as grouped_cmd_skill,
            _extract_skill_name as grouped_extract_skill_name,
        )
        from xenon.repl.commands import _cmd_skill, _extract_skill_name

        assert _cmd_skill is grouped_cmd_skill
        assert _extract_skill_name is grouped_extract_skill_name

    def test_extract_skill_name_english(self):
        """测试从英文输入提取 skill 名称。"""
        from xenon.repl.commands import _extract_skill_name

        # sub_args 优先
        assert _extract_skill_name("creat", "frontend-design") == "frontend-design"
        # sub 是有效的英文名
        assert _extract_skill_name("my-skill", "a description") == "my-skill"
        # 从中文描述中提取英文
        result = _extract_skill_name("create", "my-cool-tool")
        assert result in ("my-cool-tool", "create")  # sub 不是 typo

    def test_extract_skill_name_chinese(self):
        """测试中文输入时生成稳定哈希名。"""
        from xenon.repl.commands import _extract_skill_name

        # 纯中文 → 应返回 skill-<hash> 而非 timestamp
        result = _extract_skill_name("创建", "帮我写一个自动化脚本")
        assert result.startswith("skill-")
        # 相同输入应产生相同 hash
        result2 = _extract_skill_name("创建", "帮我写一个自动化脚本")
        assert result == result2

    def test_extract_skill_name_known_typo(self):
        """测试已知 typo 不被当作 skill 名。"""
        from xenon.repl.commands import _extract_skill_name

        # 'creat' 是 typo，不是有效的 skill 名
        result = _extract_skill_name("creat", "")
        assert result.startswith("skill-")  # 应生成 hash 名

    def test_register_skill_handler(self):
        """测试动态注册 skill handler。"""
        import tempfile
        from pathlib import Path
        from xenon.repl.commands import _register_skill_handler, _HANDLERS

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SkillManager(Path(tmpdir))
            skill = mgr.create(
                "test-handler", "test", [{"type": "echo", "prompt": "hi"}]
            )
            cmd_name = f"/{skill.name}"

            _register_skill_handler(skill, mgr)

            # 注册后在 _HANDLERS
            assert cmd_name in _HANDLERS
            assert callable(_HANDLERS[cmd_name])

            # 清理
            del _HANDLERS[cmd_name]

    def test_create_persistence(self):
        """测试 _register_skill_handler 在 create 流程中被调用。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SkillManager(Path(tmpdir))
            mgr.create(
                "persist-test",
                "persistence check",
                [{"type": "echo", "prompt": "test"}],
            )

            # 验证磁盘文件存在
            yaml_path = Path(tmpdir) / "persist-test.yaml"
            assert yaml_path.exists()

            # 重新加载
            mgr2 = SkillManager(Path(tmpdir))
            loaded = mgr2.get("persist-test")
            assert loaded is not None
            assert loaded.name == "persist-test"
            assert loaded.description == "persistence check"
            assert len(loaded.steps) == 1

    # ── v0.8: standard Agent Skills compatibility ──

    def test_agent_skill_metadata_is_loaded_lazily(self, tmp_path):
        skills_root = tmp_path / "skills"
        skill_path = _write_agent_skill(
            skills_root,
            "review-code",
            description="Review code with project conventions",
            body="# Review\nRead references/checklist.md only when needed.",
        )

        manager = SkillManager(skills_root)
        skill = manager.get("review-code")

        assert skill is not None
        assert skill.is_agent_skill
        assert skill.path == skill_path
        assert skill.instructions is None
        assert skill.metadata["requires"]["bins"] == ["python"]
        assert manager.load_instructions("review-code").startswith("# Review")
        assert skill.instructions is not None

    def test_legacy_yaml_and_agent_skill_coexist(self, tmp_path):
        skills_root = tmp_path / "skills"
        manager = SkillManager(skills_root)
        manager.create("legacy", "Old recipe", [{"type": "echo", "prompt": "ok"}])
        _write_agent_skill(skills_root, "standard", description="New standard skill")

        manager.load()

        assert [skill.name for skill in manager.list_all()] == ["legacy", "standard"]
        assert manager.get("legacy").format == "xenon-yaml"
        assert manager.get("standard").format == "agent-skill"

    def test_legacy_unicode_skill_name_remains_compatible(self, tmp_path):
        manager = SkillManager(tmp_path / "skills")
        manager.create("代码审查", "旧版中文名", [{"type": "echo", "prompt": "ok"}])

        reloaded = SkillManager(tmp_path / "skills")

        assert reloaded.get("代码审查") is not None

    def test_layered_skill_precedence_is_project_xenon_first(self, tmp_path):
        shared = tmp_path / "home-shared"
        user = tmp_path / "home-xenon"
        project = tmp_path / "project"
        _write_agent_skill(shared, "same", description="shared user")
        _write_agent_skill(user, "same", description="xenon user")
        _write_agent_skill(
            project / ".agents" / "skills", "same", description="shared project"
        )
        winning = _write_agent_skill(
            project / ".xenon" / "skills", "same", description="xenon project"
        )

        manager = SkillManager(
            user,
            project_root=project,
            shared_skills_dir=shared,
        )

        assert manager.get("same").description == "xenon project"
        assert manager.get("same").source == "project"
        assert manager.get("same").path == winning

    def test_one_broken_skill_does_not_hide_healthy_skills(self, tmp_path):
        skills_root = tmp_path / "skills"
        _write_agent_skill(skills_root, "healthy")
        broken = skills_root / "broken" / "SKILL.md"
        broken.parent.mkdir(parents=True)
        broken.write_text("# no frontmatter", encoding="utf-8")

        manager = SkillManager(skills_root)

        assert manager.get("healthy") is not None
        assert manager.get("broken") is None
        assert len(manager.load_errors) == 1
        assert str(broken) in manager.load_errors[0]

    def test_agent_skill_resource_access_is_bounded(self, tmp_path):
        skills_root = tmp_path / "skills"
        _write_agent_skill(skills_root, "bounded")
        reference = skills_root / "bounded" / "references" / "guide.md"
        reference.parent.mkdir()
        reference.write_text("safe guide", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (skills_root / "bounded" / "references" / "escape.txt").symlink_to(outside)

        manager = SkillManager(skills_root)

        assert manager.list_resources("bounded") == ["references/guide.md"]
        assert manager.read_resource("bounded", "references/guide.md") == "safe guide"
        with pytest.raises(ValueError, match="边界"):
            manager.read_resource("bounded", "../outside.txt")
        with pytest.raises(ValueError, match="边界"):
            manager.read_resource("bounded", "references/escape.txt")

    def test_agent_prompt_keeps_unrequested_resources_lazy(self, tmp_path):
        skills_root = tmp_path / "skills"
        _write_agent_skill(
            skills_root,
            "lazy",
            body="Read references/details.md only when the task needs details.",
        )
        details = skills_root / "lazy" / "references" / "details.md"
        details.parent.mkdir()
        details.write_text("RESOURCE_SECRET_SHOULD_STAY_LAZY", encoding="utf-8")
        manager = SkillManager(skills_root)

        prompt = manager.build_agent_prompt("lazy", "check this repository")

        assert "check this repository" in prompt
        assert "references/details.md" in prompt
        assert "RESOURCE_SECRET_SHOULD_STAY_LAZY" not in prompt

    def test_remove_project_override_reveals_user_skill(self, tmp_path):
        user = tmp_path / "user"
        project = tmp_path / "project"
        _write_agent_skill(user, "layered", description="user copy")
        _write_agent_skill(
            project / ".xenon" / "skills",
            "layered",
            description="project copy",
        )
        manager = SkillManager(user, project_root=project)

        assert manager.get("layered").description == "project copy"
        assert manager.remove("layered") is True
        assert manager.get("layered").description == "user copy"

    def test_agent_skill_size_limit_is_isolated(self, tmp_path):
        skills_root = tmp_path / "skills"
        _write_agent_skill(skills_root, "healthy")
        oversized = _write_agent_skill(skills_root, "oversized")
        with oversized.open("a", encoding="utf-8") as stream:
            stream.write("x" * (257 * 1024))

        manager = SkillManager(skills_root)

        assert manager.get("healthy") is not None
        assert manager.get("oversized") is None
        assert any("256 KiB" in error for error in manager.load_errors)

    def test_metadata_scan_does_not_decode_a_truncated_body_prefix(self, tmp_path):
        skills_root = tmp_path / "skills"
        path = _write_agent_skill(skills_root, "unicode-boundary", body="placeholder")
        header = path.read_text(encoding="utf-8").split("---\n\n", 1)[0] + "---\n\n"
        padding = "a" * (64 * 1024 - len(header.encode("utf-8")) - 1)
        path.write_text(header + padding + "汉字正文", encoding="utf-8")

        manager = SkillManager(skills_root)

        assert manager.get("unicode-boundary") is not None
        assert manager.get("unicode-boundary").instructions is None

    def test_skill_diagnostics_counts_formats_and_errors(self, tmp_path):
        skills_root = tmp_path / "skills"
        manager = SkillManager(skills_root)
        manager.create("legacy", "Legacy", [{"type": "echo", "prompt": "ok"}])
        _write_agent_skill(skills_root, "standard")
        broken = skills_root / "broken" / "SKILL.md"
        broken.parent.mkdir(parents=True)
        broken.write_text("invalid", encoding="utf-8")
        manager.load()

        report = manager.diagnostics()

        assert report["skill_count"] == 2
        assert report["agent_skill_count"] == 1
        assert report["legacy_skill_count"] == 1
        assert len(report["errors"]) == 1

    def test_standard_skill_uses_repl_agent_loop_when_available(self, tmp_path):
        from xenon.repl.commands import _execute_installed_skill

        skills_root = tmp_path / "skills"
        _write_agent_skill(skills_root, "tool-aware", body="Use read_file when needed.")
        manager = SkillManager(skills_root)

        class FakeRegistry:
            @staticmethod
            def get_role_priority(_role):
                raise AssertionError("agent-loop path must not call the LLM fallback")

        class FakeRepl:
            prompts = []

            invocations = []

            def _handle_chat(self, prompt, **kwargs):
                self.prompts.append(prompt)
                self.invocations.append(kwargs)

        repl = FakeRepl()
        result = _execute_installed_skill(
            manager,
            "tool-aware",
            "inspect pyproject.toml",
            registry=FakeRegistry(),
            session_state={"_repl": repl},
        )

        assert result == ""
        assert len(repl.prompts) == 1
        assert "Use read_file when needed." in repl.prompts[0]
        assert "inspect pyproject.toml" in repl.prompts[0]
        assert repl.invocations == [
            {"skill_name": "tool-aware", "skill_args": "inspect pyproject.toml"}
        ]
