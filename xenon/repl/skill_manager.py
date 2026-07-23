"""
Skill Manager — legacy Xenon recipes and standard ``SKILL.md`` skills.

Discovery is layered from broad to specific:

1. ``~/.agents/skills/<name>/SKILL.md`` (shared Agent Skills)
2. ``~/.xenon/skills`` (Xenon user skills and legacy ``*.yaml`` recipes)
3. ``<project>/.agents/skills/<name>/SKILL.md``
4. ``<project>/.xenon/skills``

Later layers override an earlier skill with the same name.  Standard skill
bodies and resources are loaded only when the skill is invoked, keeping the
normal prompt small even when many skills are installed.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _shell_quote(value) -> str:
    """Quote a template replacement before inserting it into a shell command."""
    text = str(value)
    if sys.platform == "win32":
        return "'" + text.replace("'", "''") + "'"
    return shlex.quote(text)


_SKILLS_DIR = Path.home() / ".xenon" / "skills"
_SHARED_SKILLS_DIR = Path.home() / ".agents" / "skills"
_PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "CMakeLists.txt",
)
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LEGACY_SKILL_NAME_RE = re.compile(r"^[\w-]{1,64}$", re.UNICODE)
_FRONTMATTER_BYTES = 64 * 1024
_SKILL_MD_BYTES = 256 * 1024
_RESOURCE_BYTES = 128 * 1024
_RESOURCE_COUNT = 500
_INSTALL_MAX_FILES = 2_000
_INSTALL_MAX_BYTES = 50 * 1024 * 1024


class SkillFormatError(ValueError):
    """A skill is present on disk but does not satisfy its format contract."""


@dataclass
class SkillStep:
    """One executable step in a legacy Xenon YAML recipe."""

    type: str
    prompt: str = ""
    action: str = ""
    file_path: str = ""
    content: str = ""
    output_var: str = ""


@dataclass
class Skill:
    """A discovered skill.

    ``instructions`` deliberately remains ``None`` during discovery for
    standard Agent Skills.  :meth:`SkillManager.load_instructions` fills it on
    first use.
    """

    name: str
    description: str = ""
    system_prompt: str = ""
    steps: list[SkillStep] = field(default_factory=list)
    params: list[dict[str, str]] = field(default_factory=list)
    format: str = "xenon-yaml"
    source: str = "user"
    path: Path | None = None
    root: Path | None = None
    version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    instructions: str | None = field(default=None, repr=False)

    @property
    def is_agent_skill(self) -> bool:
        return self.format == "agent-skill"


@dataclass(frozen=True)
class SkillInstallResult:
    """Receipt for a completed local Agent Skill installation."""

    name: str
    destination: Path
    scope: str
    file_count: int
    total_bytes: int
    replaced: bool


class SkillManager:
    """Discover, inspect, and execute legacy and standard Agent Skills."""

    def __init__(
        self,
        skills_dir: Path | None = None,
        *,
        project_root: Path | None = None,
        shared_skills_dir: Path | None = None,
    ) -> None:
        custom_user_root = skills_dir is not None
        self.skills_dir = Path(skills_dir) if skills_dir is not None else _SKILLS_DIR
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else (None if custom_user_root else self._detect_project_root())
        )
        self.shared_skills_dir = (
            Path(shared_skills_dir)
            if shared_skills_dir is not None
            else (None if custom_user_root else _SHARED_SKILLS_DIR)
        )
        self.skills: dict[str, Skill] = {}
        self.load_errors: list[str] = []
        self._roots = self._build_roots()
        self.load()

    @staticmethod
    def _detect_project_root(start: Path | None = None) -> Path | None:
        """Find a bounded project root without scanning project contents."""
        configured = os.environ.get("XENON_PROJECT_ROOT", "").strip()
        if configured:
            candidate = Path(configured).expanduser().resolve()
            return candidate if candidate.is_dir() else None

        current = (start or Path.cwd()).resolve()
        home = Path.home().resolve()
        if current in {home, Path(current.anchor)}:
            return None

        fallback = current
        for _ in range(6):
            if any((current / marker).exists() for marker in _PROJECT_MARKERS):
                return current
            parent = current.parent
            if parent == current or parent == home:
                break
            current = parent
        return fallback

    def _build_roots(self) -> list[tuple[str, Path]]:
        roots: list[tuple[str, Path]] = []
        if self.shared_skills_dir is not None:
            roots.append(("user-shared", self.shared_skills_dir))
        roots.append(("user", self.skills_dir))
        if self.project_root is not None:
            roots.extend([
                ("project-shared", self.project_root / ".agents" / "skills"),
                ("project", self.project_root / ".xenon" / "skills"),
            ])

        unique: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        for source, root in roots:
            try:
                key = root.expanduser().resolve()
            except OSError:
                key = root.expanduser().absolute()
            if key in seen:
                continue
            seen.add(key)
            unique.append((source, root.expanduser()))
        return unique

    def load(self) -> None:
        """Reload all roots; one malformed skill cannot hide healthy skills."""
        self.skills = {}
        self.load_errors = []
        for source, root in self._roots:
            if not root.is_dir():
                continue
            for path in sorted((*root.glob("*.yaml"), *root.glob("*.yml"))):
                self._load_one(path, source, root, self._load_legacy_skill)
            for path in sorted(root.glob("*/SKILL.md")):
                self._load_one(path, source, root, self._load_agent_skill)
        logger.info("加载了 %d 个技能（%d 个错误）", len(self.skills), len(self.load_errors))

    def _load_one(self, path: Path, source: str, root: Path, loader) -> None:
        try:
            skill = loader(path, source, root)
            previous = self.skills.get(skill.name)
            if previous is not None:
                logger.info("技能 %s 由 %s 覆盖 %s", skill.name, path, previous.path)
            self.skills[skill.name] = skill
        except Exception as exc:
            message = f"{path}: {exc}"
            self.load_errors.append(message)
            logger.warning("加载技能失败: %s", message)

    @staticmethod
    def _validate_name(raw: Any) -> str:
        name = str(raw or "").strip().lower()
        if not _SKILL_NAME_RE.fullmatch(name):
            raise SkillFormatError(
                "name 必须为 1-64 位小写字母、数字、连字符或下划线"
            )
        return name

    @staticmethod
    def _validate_legacy_name(raw: Any) -> str:
        name = str(raw or "").strip().lower().replace(" ", "_")
        if not _LEGACY_SKILL_NAME_RE.fullmatch(name) or name.startswith("."):
            raise SkillFormatError("旧技能 name 必须为 1-64 位文字、数字、连字符或下划线")
        return name

    def _load_legacy_skill(self, path: Path, source: str, root: Path) -> Skill:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SkillFormatError("YAML 顶层必须是对象")
        name = self._validate_legacy_name(data.get("name"))
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            raise SkillFormatError("steps 必须是数组")
        steps = [SkillStep(**step) for step in raw_steps]
        return Skill(
            name=name,
            description=str(data.get("description", "")),
            system_prompt=str(data.get("system_prompt", "")),
            steps=steps,
            params=data.get("params", []) if isinstance(data.get("params", []), list) else [],
            source=source,
            path=path,
            root=root,
        )

    def _load_agent_skill(self, path: Path, source: str, root: Path) -> Skill:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise SkillFormatError(f"无法读取: {exc}") from exc
        if size > _SKILL_MD_BYTES:
            raise SkillFormatError(f"SKILL.md 超过 {_SKILL_MD_BYTES // 1024} KiB 限制")

        frontmatter, _ = self._read_agent_document(path, metadata_only=True)
        name = self._validate_name(frontmatter.get("name"))
        description = str(frontmatter.get("description", "")).strip()
        if not description:
            raise SkillFormatError("description 不能为空")
        metadata = frontmatter.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise SkillFormatError("metadata 必须是对象")
        return Skill(
            name=name,
            description=description,
            format="agent-skill",
            source=source,
            path=path,
            root=path.parent,
            version=str(frontmatter.get("version", "")),
            metadata=metadata,
        )

    @staticmethod
    def _read_agent_document(path: Path, *, metadata_only: bool) -> tuple[dict[str, Any], str]:
        if metadata_only:
            header_lines: list[bytes] = []
            consumed = 0
            with path.open("rb") as stream:
                first = stream.readline()
                consumed += len(first)
                if first.removeprefix(b"\xef\xbb\xbf").strip() != b"---":
                    raise SkillFormatError("缺少 YAML frontmatter 起始分隔符 ---")
                for raw_line in stream:
                    consumed += len(raw_line)
                    if consumed > _FRONTMATTER_BYTES:
                        raise SkillFormatError(
                            f"frontmatter 超过 {_FRONTMATTER_BYTES // 1024} KiB 限制"
                        )
                    if raw_line.strip() == b"---":
                        break
                    header_lines.append(raw_line)
                else:
                    raise SkillFormatError("缺少 YAML frontmatter 结束分隔符 ---")
            raw_header = b"".join(header_lines)
            try:
                header_text = raw_header.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SkillFormatError("SKILL.md 必须使用 UTF-8 编码") from exc
            try:
                frontmatter = yaml.safe_load(header_text) or {}
            except yaml.YAMLError as exc:
                raise SkillFormatError(f"frontmatter YAML 无效: {exc}") from exc
            if not isinstance(frontmatter, dict):
                raise SkillFormatError("frontmatter 必须是对象")
            return frontmatter, ""
        else:
            raw = path.read_bytes()
            if len(raw) > _SKILL_MD_BYTES:
                raise SkillFormatError(f"SKILL.md 超过 {_SKILL_MD_BYTES // 1024} KiB 限制")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SkillFormatError("SKILL.md 必须使用 UTF-8 编码") from exc

        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillFormatError("缺少 YAML frontmatter 起始分隔符 ---")
        closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if closing is None:
            raise SkillFormatError("缺少 YAML frontmatter 结束分隔符 ---")
        try:
            frontmatter = yaml.safe_load("\n".join(lines[1:closing])) or {}
        except yaml.YAMLError as exc:
            raise SkillFormatError(f"frontmatter YAML 无效: {exc}") from exc
        if not isinstance(frontmatter, dict):
            raise SkillFormatError("frontmatter 必须是对象")
        body = "" if metadata_only else "\n".join(lines[closing + 1:]).strip()
        return frontmatter, body

    def save(self) -> None:
        """Persist legacy recipes; standard Agent Skills remain source-owned."""
        for skill in self.skills.values():
            if not skill.is_agent_skill:
                self.save_one(skill)

    def save_one(self, skill: Skill) -> None:
        """Save one legacy Xenon recipe without serializing runtime metadata."""
        if skill.is_agent_skill:
            raise ValueError("标准 Agent Skill 由 SKILL.md 管理，不能保存为旧 YAML")
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "name": skill.name,
            "description": skill.description,
            "system_prompt": skill.system_prompt,
            "steps": [
                {
                    "type": step.type,
                    "prompt": step.prompt,
                    "action": step.action,
                    "file_path": step.file_path,
                    "content": step.content,
                    "output_var": step.output_var,
                }
                for step in skill.steps
            ],
            "params": skill.params,
        }
        path = self.skills_dir / f"{skill.name}.yaml"
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        skill.path = path
        skill.root = self.skills_dir

    def create(
        self,
        name: str,
        description: str,
        steps: list[dict[str, str]],
        system_prompt: str = "",
        params: list[dict[str, str]] | None = None,
    ) -> Skill:
        """Create a backwards-compatible Xenon YAML recipe."""
        normalized = self._validate_legacy_name(name.lstrip("/"))
        skill = Skill(
            name=normalized,
            description=description,
            system_prompt=system_prompt,
            steps=[SkillStep(**step) for step in steps],
            params=params or [],
            source="user",
            root=self.skills_dir,
        )
        self.skills[normalized] = skill
        self.save_one(skill)
        logger.info("创建技能: /%s", normalized)
        return skill

    def remove(self, name: str) -> bool:
        """Remove the active skill definition and reveal any lower layer."""
        skill = self.get(name)
        if skill is None or skill.path is None:
            return False
        if skill.is_agent_skill:
            directory = skill.path.parent
            configured_parent = next(
                (root for _, root in self._roots if directory.parent.absolute() == root.absolute()),
                None,
            )
            if configured_parent is None:
                raise ValueError("拒绝删除技能根目录之外的路径")
            if directory.is_symlink():
                directory.unlink()
            else:
                shutil.rmtree(directory)
        else:
            skill.path.unlink(missing_ok=True)
        self.load()
        return True

    def list_all(self) -> list[Skill]:
        """Return skills in stable name order without loading Markdown bodies."""
        return [self.skills[name] for name in sorted(self.skills)]

    def diagnostics(self) -> dict[str, Any]:
        """Return a deterministic, secret-free discovery report."""
        return {
            "skill_count": len(self.skills),
            "agent_skill_count": sum(skill.is_agent_skill for skill in self.skills.values()),
            "legacy_skill_count": sum(not skill.is_agent_skill for skill in self.skills.values()),
            "roots": [
                {"source": source, "path": str(root), "exists": root.is_dir()}
                for source, root in self._roots
            ],
            "errors": list(self.load_errors),
        }

    def install(
        self,
        source: str | Path,
        *,
        scope: str = "user",
        force: bool = False,
    ) -> SkillInstallResult:
        """Validate and atomically install a local ``SKILL.md`` directory."""
        source_path = Path(source).expanduser().resolve()
        if source_path.is_dir():
            source_dir = source_path
            document = source_dir / "SKILL.md"
        else:
            document = source_path
            source_dir = document.parent
        if not document.is_file() or document.name != "SKILL.md":
            raise SkillFormatError("安装源必须是技能目录或其中的 SKILL.md")

        skill = self._load_agent_skill(document, "install-source", source_dir.parent)
        target_root = self._scope_root(scope)
        target_root.mkdir(parents=True, exist_ok=True)
        destination = target_root / skill.name
        if destination.resolve() == source_dir:
            raise ValueError("安装源已位于目标目录")
        if (destination.exists() or destination.is_symlink()) and not force:
            raise FileExistsError(f"技能已存在: {destination}；使用 --force 明确替换")

        file_count, total_bytes = self._validate_install_tree(source_dir)
        temporary = Path(tempfile.mkdtemp(prefix=f".{skill.name}.install-", dir=target_root))
        backup: Path | None = None
        try:
            shutil.rmtree(temporary)
            shutil.copytree(source_dir, temporary, symlinks=True)
            # Validate the copied document as the final artifact, not only the source.
            self._load_agent_skill(temporary / "SKILL.md", "install-target", target_root)
            replaced = destination.exists() or destination.is_symlink()
            if replaced:
                backup = target_root / f".{skill.name}.backup-{uuid.uuid4().hex}"
                destination.rename(backup)
            try:
                temporary.rename(destination)
            except BaseException:
                if backup is not None and not destination.exists():
                    backup.rename(destination)
                raise
            if backup is not None:
                if backup.is_symlink():
                    backup.unlink()
                else:
                    shutil.rmtree(backup)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

        self.load()
        return SkillInstallResult(
            name=skill.name,
            destination=destination,
            scope=scope,
            file_count=file_count,
            total_bytes=total_bytes,
            replaced=replaced,
        )

    def _scope_root(self, scope: str) -> Path:
        mapping: dict[str, Path | None] = {
            "user": self.skills_dir,
            "shared-user": self.shared_skills_dir,
            "project": (
                self.project_root / ".xenon" / "skills"
                if self.project_root is not None else None
            ),
            "shared-project": (
                self.project_root / ".agents" / "skills"
                if self.project_root is not None else None
            ),
        }
        if scope not in mapping:
            raise ValueError("scope 必须是 user/shared-user/project/shared-project")
        target = mapping[scope]
        if target is None:
            raise ValueError(f"作用域 {scope} 当前不可用（未检测到对应目录边界）")
        return target

    @staticmethod
    def _validate_install_tree(source_dir: Path) -> tuple[int, int]:
        root = source_dir.resolve()
        file_count = 0
        total_bytes = 0
        for path in source_dir.rglob("*"):
            if path.is_symlink():
                if Path(os.readlink(path)).is_absolute():
                    raise ValueError(f"安装源包含绝对符号链接: {path}")
                try:
                    path.resolve(strict=True).relative_to(root)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"安装源包含越界符号链接: {path}") from exc
                continue
            if not path.is_file():
                continue
            file_count += 1
            total_bytes += path.stat().st_size
            if file_count > _INSTALL_MAX_FILES:
                raise ValueError(f"技能文件数超过 {_INSTALL_MAX_FILES} 个限制")
            if total_bytes > _INSTALL_MAX_BYTES:
                raise ValueError(
                    f"技能总大小超过 {_INSTALL_MAX_BYTES // (1024 * 1024)} MiB 限制"
                )
        return file_count, total_bytes

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name.lstrip("/").lower())

    def load_instructions(self, name: str) -> str:
        """Load and cache a standard skill body only when it is selected."""
        skill = self.get(name)
        if skill is None:
            raise KeyError(name)
        if not skill.is_agent_skill or skill.path is None:
            return skill.system_prompt
        if skill.instructions is None:
            _, skill.instructions = self._read_agent_document(skill.path, metadata_only=False)
        return skill.instructions

    def list_resources(self, name: str) -> list[str]:
        """List root-confined resource files without reading their contents."""
        skill = self._require_agent_skill(name)
        root = skill.root.resolve()
        resources: list[str] = []
        for path in sorted(skill.root.rglob("*")):
            if len(resources) >= _RESOURCE_COUNT:
                break
            if not path.is_file() or path == skill.path:
                continue
            try:
                resolved = path.resolve()
                relative = resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            resources.append(relative.as_posix())
        return resources

    def read_resource(self, name: str, relative_path: str, *, max_bytes: int = _RESOURCE_BYTES) -> str:
        """Read one UTF-8 text resource with traversal, symlink, and size guards."""
        skill = self._require_agent_skill(name)
        requested = Path(relative_path)
        if requested.is_absolute():
            raise ValueError("资源路径必须相对于技能目录")
        root = skill.root.resolve()
        try:
            target = (root / requested).resolve(strict=True)
            target.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("资源路径越过技能目录边界") from exc
        if not target.is_file():
            raise ValueError("资源不是普通文件")
        size = target.stat().st_size
        if size > max_bytes:
            raise ValueError(f"资源超过 {max_bytes // 1024} KiB 限制")
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("资源不是 UTF-8 文本；二进制资源不能注入 Prompt") from exc

    def _require_agent_skill(self, name: str) -> Skill:
        skill = self.get(name)
        if skill is None:
            raise KeyError(name)
        if not skill.is_agent_skill or skill.root is None:
            raise ValueError(f"/{skill.name} 是旧 YAML 技能，没有 Agent Skill 资源目录")
        return skill

    def build_agent_prompt(self, name: str, args: str) -> str:
        """Build an invocation prompt while leaving referenced resources lazy."""
        skill = self._require_agent_skill(name)
        instructions = self.load_instructions(name)
        request = args.strip() or "按照技能说明完成当前任务。"
        return (
            f"## 已安装 Agent Skill: {skill.name}\n"
            f"技能目录: {skill.root.resolve()}\n"
            "以下内容来自用户已安装的 SKILL.md。严格遵循其中的工作流；"
            "仅在需要时使用工具读取该目录中的 references、scripts 或 assets，"
            "不要预先加载全部资源。\n\n"
            f"<skill_instructions>\n{instructions}\n</skill_instructions>\n\n"
            f"## 用户调用参数\n{request}"
        )

    def execute(self, name: str, args: str, model_priority: list[str] | None = None) -> str:
        """Execute a legacy recipe or an LLM-only standard skill fallback."""
        skill = self.get(name)
        if not skill:
            return f"❌ 技能 /{name} 不存在"
        if skill.is_agent_skill:
            if not model_priority:
                return "错误: 未配置模型"
            from xenon.utils.llm_client import chat_completion

            prompt = self.build_agent_prompt(skill.name, args)
            last_error = None
            for model_id in model_priority:
                try:
                    return chat_completion(
                        model_id,
                        [{"role": "user", "content": prompt}],
                        max_tokens=4096,
                    )
                except Exception as exc:
                    last_error = exc
            return f"LLM 调用失败: {last_error}"

        param_values = self._parse_args(args, skill.params)
        context: dict[str, str] = dict(param_values)
        results = []
        for index, step in enumerate(skill.steps):
            try:
                if step.type == "llm":
                    output = self._execute_llm_step(step, context, skill.system_prompt, model_priority)
                elif step.type == "command":
                    output = self._execute_command_step(step, context)
                elif step.type == "echo":
                    output = self._resolve_template(step.prompt or step.action, context)
                elif step.type == "write_file":
                    output = self._execute_write_file(step, context)
                elif step.type == "read_file":
                    output = self._execute_read_file(step, context)
                else:
                    output = f"未知步骤类型: {step.type}"
                if step.output_var:
                    context[step.output_var] = output
                results.append(f"[步骤 {index + 1}] {output[:500]}")
            except Exception as exc:
                results.append(f"[步骤 {index + 1} 错误] {exc}")
                break
        return "\n\n".join(results)

    def _execute_llm_step(
        self,
        step: SkillStep,
        context: dict,
        system_prompt: str,
        model_priority: list[str] | None,
    ) -> str:
        from xenon.utils.llm_client import chat_completion

        prompt = self._resolve_template(step.prompt, context)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        if not model_priority:
            return "错误: 未配置模型"
        last_error = None
        for model_id in model_priority:
            try:
                return chat_completion(model_id, messages, max_tokens=4096)
            except Exception as exc:
                last_error = exc
        return f"LLM 调用失败: {last_error}"

    def _execute_command_step(self, step: SkillStep, context: dict) -> str:
        import subprocess

        cmd = self._resolve_template(step.action, context, quote=True)
        try:
            if sys.platform == "win32":
                proc = subprocess.run(
                    ["powershell", "-Command", cmd], capture_output=True, text=True, timeout=60,
                )
            else:
                proc = subprocess.run(
                    ["/bin/bash", "-c", cmd], capture_output=True, text=True, timeout=60,
                )
            output = proc.stdout.strip()
            if proc.stderr.strip():
                output += f"\n[stderr] {proc.stderr.strip()}"
            return output or "(无输出)"
        except subprocess.TimeoutExpired:
            return f"命令超时: {cmd}"
        except Exception as exc:
            return f"命令执行失败: {exc}"

    def _execute_write_file(self, step: SkillStep, context: dict) -> str:
        path = self._resolve_template(step.file_path, context)
        content = self._resolve_template(step.content, context)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入文件: {path}"

    def _execute_read_file(self, step: SkillStep, context: dict) -> str:
        path = self._resolve_template(step.file_path, context)
        target = Path(path)
        if not target.exists():
            return f"文件不存在: {path}"
        return target.read_text(encoding="utf-8")[:5000]

    @staticmethod
    def _parse_args(args: str, params: list[dict[str, str]]) -> dict[str, str]:
        result = {param["name"]: param["default"] for param in params if "default" in param}
        if args.strip():
            parts = args.split()
            for index, param in enumerate(params):
                if index < len(parts):
                    result[param["name"]] = parts[index]
        return result

    @staticmethod
    def _resolve_template(template: str, context: dict, *, quote: bool = False) -> str:
        def _replace(match: re.Match) -> str:
            key = match.group(1)
            value = context.get(key)
            if value is None:
                return match.group(0)
            return _shell_quote(value) if quote else str(value)

        return re.sub(r"\{(\w+)\}", _replace, template)
