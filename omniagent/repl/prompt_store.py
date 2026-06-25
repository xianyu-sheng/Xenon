"""渐进式系统提示词存储 — Self-Evolving Prompt Management.

三层提示词架构：
- Master:  核心身份 + 行为准则（始终加载，版本化管理）
- Domain:  按场景的领域知识（按相关性评分渐进式加载）
- Memory:  Agent 自主学习沉淀（通过 remember 工具或事后评估写入）

双路径存储，项目级覆盖用户级：
- .omniagent/prompts/     ← 项目级（优先）
- ~/.omniagent/prompts/   ← 用户级（兜底）
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── 路径常量 ─────────────────────────────────────────────────
_PROJECT_PROMPTS_DIR = Path(".omniagent") / "prompts"
_USER_PROMPTS_DIR = Path.home() / ".omniagent" / "prompts"

# ── Token 预算 ───────────────────────────────────────────────
_MAX_CONTEXT_TOKENS = 3000      # 补充提示词的总 token 预算
_MASTER_BUDGET = 500            # Master 最大 token
_DOMAIN_ENTRY_BUDGET = 800      # 单条 Domain 最大 token
_MEMORY_ENTRY_BUDGET = 600      # 单条 Memory 最大 token
_MAX_MEMORY_TOKENS = 2000       # 所有 memory 文件总 token 上限
_MAX_MEMORY_FILES = 30          # memory 文件数量上限（硬保护）

# ── 相关性评分权重 ──────────────────────────────────────────
_DOMAIN_NAME_MATCH = 10         # domain 名命中用户输入
_TAG_MATCH = 5                  # tag 命中
_KEYWORD_OVERLAP = 2           # 内容关键词重叠

# ── 默认种子提示词 ──────────────────────────────────────────

_DEFAULT_MASTER = """\
你是 OmniAgent-CLI，一个智能化的命令行 AI 编程助手。

## 核心原则
1. **诚实**: 不确定时明确说"不知道"，不编造答案
2. **精准**: 定位到具体文件和行号
3. **主动**: 发现潜在问题时主动提醒
4. **高效**: 一次性给出完整方案，避免多轮反复
5. **学习**: 记住用户的偏好和项目的约定，持续改进

## 回答风格
- 关键技术决策说明理由
- 代码修改给出完整上下文（文件路径 + 行号）
- 多个方案时列出优劣对比
- 遵循项目现有的代码风格和命名规范

## 工具使用
- 优先使用专用工具（Read/Write/Edit/Grep/Glob）而非 shell 命令
- 文件操作前先确认路径存在
- 批量独立操作并行执行
"""

_SEED_DOMAINS: dict[str, dict[str, Any]] = {
    "python": {
        "tags": ["python", "code-style", "typing", "pep8"],
        "priority": "high",
        "content": """\
# Python 编码规范

## 代码风格
- 遵循 PEP 8 规范
- 使用 Type Hints 标注函数签名
- 模块级 docstring 描述模块用途
- 类和方法使用简洁的 docstring

## 最佳实践
- 优先使用 pathlib.Path 而非 os.path
- 使用 dataclasses 定义数据结构
- 错误处理：具体异常 > 泛化 Exception
- 使用 logger 而非 print 输出日志
- from __future__ import annotations 放在文件顶部

## 测试
- 使用 pytest 框架
- 测试文件命名：test_<module>.py
- 测试类命名：Test<Component>
- 使用 tmp_path fixture 隔离文件 I/O""",
    },
    "debugging": {
        "tags": ["debug", "error", "traceback", "调试", "错误"],
        "priority": "high",
        "content": """\
# 调试策略

## 根因分析流程
1. **读错误信息**: 定位文件路径和行号
2. **读相关代码**: 理解上下文逻辑
3. **分析调用链**: 追踪数据流
4. **提出修复**: 给出具体修改方案

## 常见问题模式
- ImportError → 检查包安装和 sys.path
- AttributeError → 检查对象类型和属性名
- UnicodeEncodeError → 检查 surrogate 字符处理
- I/O operation on closed file → 检查文件生命周期

## 调试原则
- 不要猜测，先读代码确认
- 一次修改一个变量，便于验证
- 修复后解释根因和预防措施""",
    },
    "git": {
        "tags": ["git", "commit", "branch", "push", "merge"],
        "priority": "medium",
        "content": """\
# Git 工作流

## 提交规范
- commit message 使用英文，格式：type: description
- 类型: feat/fix/refactor/docs/test/chore
- 单次提交只包含相关变更
- 提交前确保测试通过

## 分支操作
- 新功能在独立分支开发
- 合并前先 rebase 到 main
- 不要 force push 到共享分支

## 安全
- 检查敏感信息不提交
- .gitignore 排除 .env, credentials, .omniagent/""",
    },
    "testing": {
        "tags": ["test", "pytest", "unittest", "测试", "mock"],
        "priority": "medium",
        "content": """\
# 测试策略

## 测试类型
- 单元测试: 测试单个函数/类
- 集成测试: 测试模块间交互
- 端到端测试: 测试完整流程

## 编写原则
- 每个公共函数至少一个正向测试
- 覆盖边界条件和错误路径
- 使用 tmp_path / tempfile 隔离文件 I/O
- 测试名称描述被测试的行为

## pytest 约定
- 文件: test_<name>.py
- 函数: test_<descriptive_name>
- 类: Test<Component>
- 使用 plain assert，不用 assertEqual""",
    },
}


def _estimate_tokens(text: str) -> int:
    """估算文本 token 数（与 ContextManager.estimate_tokens 相同启发式）。

    - 中文字符约 2 token/字
    - 英文约 1.3 token/word
    - 代码重度内容按字符密度估算
    - 不低于 len(text)/2
    """
    if not text:
        return 0

    cjk_count = sum(1 for c in text if '一' <= c <= '鿿')
    words = len(text.split())
    chars = len(text)

    code_chars = text.count('{') + text.count('}') + text.count(';') + text.count('=')
    is_code_heavy = code_chars > chars * 0.02

    char_based = max(chars // 2, 1)

    if is_code_heavy:
        return max(words * 2, int(chars * 0.4))
    elif cjk_count > chars * 0.3:
        return max(words, int(cjk_count * 2), char_based)
    else:
        return max(words, int(words * 1.3), char_based)


# ── 数据结构 ─────────────────────────────────────────────────


@dataclass
class PromptMetadata:
    """YAML frontmatter 元数据。"""

    version: int = 1
    created: str = field(default_factory=lambda: datetime.now().isoformat())
    updated: str = field(default_factory=lambda: datetime.now().isoformat())
    domain: str = "general"
    tags: list[str] = field(default_factory=list)
    priority: str = "medium"  # low | medium | high
    source: str = "system"  # system | user | agent


@dataclass
class PromptEntry:
    """一条解析后的提示词。"""

    path: str  # 相对路径（如 "domains/python.md"）
    metadata: PromptMetadata
    content: str  # Markdown body（已剥离 frontmatter）
    category: str  # "master" | "domain" | "memory"
    token_estimate: int = 0

    def __post_init__(self):
        if self.token_estimate == 0 and self.content:
            self.token_estimate = _estimate_tokens(self.content)


# ── PromptStore ──────────────────────────────────────────────


class PromptStore:
    """渐进式系统提示词管理器。

    管理三层提示词（Master / Domain / Memory），
    支持双路径存储、版本控制和按需渐进式加载。
    """

    def __init__(self, *, project_dir: Path | None = None, user_dir: Path | None = None) -> None:
        self._project_dir = project_dir or _PROJECT_PROMPTS_DIR
        self._user_dir = user_dir or _USER_PROMPTS_DIR
        self._entries: dict[str, PromptEntry] = {}  # keyed by path
        self._master: PromptEntry | None = None
        self._loaded = False

    # ═══════════════════════════════════════════════════════════
    # 初始化与加载
    # ═══════════════════════════════════════════════════════════

    @property
    def is_initialized(self) -> bool:
        """检查是否已初始化（至少有一个目录存在 system.md）。"""
        return (
            (self._project_dir / "system.md").exists()
            or (self._user_dir / "system.md").exists()
        )

    def ensure_initialized(self) -> None:
        """确保存储已初始化，若不存在则创建种子文件。"""
        if self.is_initialized:
            self._load_all()
            return

        # 首次运行：创建目录并写入种子提示词
        self._user_dir.mkdir(parents=True, exist_ok=True)
        (self._user_dir / "domains").mkdir(parents=True, exist_ok=True)
        (self._user_dir / "memories").mkdir(parents=True, exist_ok=True)
        (self._user_dir / "versions").mkdir(parents=True, exist_ok=True)

        # 写入 master
        self._write_atomic(
            self._user_dir / "system.md",
            _make_frontmatter(PromptMetadata(domain="master", priority="high", tags=["core"]))
            + "\n"
            + _DEFAULT_MASTER,
        )

        # 写入种子 domain
        for name, info in _SEED_DOMAINS.items():
            self._write_atomic(
                self._user_dir / "domains" / f"{name}.md",
                _make_frontmatter(
                    PromptMetadata(
                        domain=name,
                        tags=info.get("tags", []),
                        priority=info.get("priority", "medium"),
                    )
                )
                + "\n"
                + info["content"],
            )

        # 写入 manifest
        self._update_manifest()
        self._load_all()
        logger.info("PromptStore 初始化完成 — 已创建种子提示词")

    def _load_all(self) -> None:
        """加载所有提示词文件。"""
        if self._loaded:
            return

        self._entries.clear()
        self._master = None

        # 1. 加载用户级文件
        self._load_from_dir(self._user_dir)
        # 2. 加载项目级文件（覆盖同名条目）
        self._load_from_dir(self._project_dir)

        self._loaded = True
        domain_count = sum(1 for e in self._entries.values() if e.category == "domain")
        memory_count = sum(1 for e in self._entries.values() if e.category == "memory")
        logger.info(
            "PromptStore 加载完成: master=%s, domains=%d, memories=%d",
            "yes" if self._master else "no",
            domain_count,
            memory_count,
        )

    def _load_from_dir(self, base: Path) -> None:
        """从目录加载提示词文件，覆盖已存在的同名条目。"""
        if not base.exists():
            return

        # 加载 master
        master_path = base / "system.md"
        if master_path.exists():
            entry = self._parse_file(master_path, category="master")
            if entry:
                self._master = entry
                self._entries["system.md"] = entry

        # 加载 domains
        domains_dir = base / "domains"
        if domains_dir.exists():
            for f in sorted(domains_dir.glob("*.md")):
                rel = f"domains/{f.name}"
                entry = self._parse_file(f, category="domain")
                if entry:
                    self._entries[rel] = entry

        # 加载 memories
        memories_dir = base / "memories"
        if memories_dir.exists():
            for f in sorted(memories_dir.glob("*.md")):
                rel = f"memories/{f.name}"
                entry = self._parse_file(f, category="memory")
                if entry:
                    self._entries[rel] = entry

    # ═══════════════════════════════════════════════════════════
    # 公共 API
    # ═══════════════════════════════════════════════════════════

    def get_master(self) -> str:
        """返回主提示词内容。"""
        self._ensure_loaded()
        if self._master:
            return self._master.content
        return _DEFAULT_MASTER

    def update_master(self, content: str) -> PromptEntry:
        """更新主提示词，自动归档旧版本。

        仅在 master 实际内容变化时创建新版本，
        相同内容重复调用不会产生多余版本。
        """
        self._ensure_loaded()

        # 归档旧版本
        if self._master and self._master.content.strip() != content.strip():
            self._archive_version(self._master)

        # 更新元数据
        if self._master:
            metadata = self._master.metadata
            metadata.version += 1
            metadata.updated = datetime.now().isoformat()
        else:
            metadata = PromptMetadata(domain="master", priority="high", tags=["core"])

        # 原子写入
        target = self._project_dir / "system.md"
        if not target.parent.exists():
            target = self._user_dir / "system.md"
        self._write_atomic(target, _make_frontmatter(metadata) + "\n" + content)

        entry = PromptEntry(
            path="system.md",
            metadata=metadata,
            content=content,
            category="master",
        )
        self._master = entry
        self._entries["system.md"] = entry
        self._update_manifest()
        return entry

    def list_domains(self) -> list[str]:
        """列出所有可用的 domain 名。"""
        self._ensure_loaded()
        return sorted(
            e.metadata.domain
            for e in self._entries.values()
            if e.category == "domain"
        )

    def get_domain(self, domain: str) -> PromptEntry | None:
        """按名称获取 domain 条目。"""
        self._ensure_loaded()
        for e in self._entries.values():
            if e.category == "domain" and e.metadata.domain == domain:
                return e
        return None

    def add_memory(
        self,
        name: str,
        content: str,
        tags: list[str] | None = None,
        *,
        priority: str = "medium",
    ) -> PromptEntry:
        """添加一条 memory 提示词。

        Memory 写入项目级目录（.omniagent/prompts/memories/），
        确保项目相关的学习不污染全局。
        """
        self._ensure_loaded()

        safe_name = name.replace(" ", "-").replace("/", "-").replace("\\", "-")
        if not safe_name.endswith(".md"):
            safe_name += ".md"

        target_dir = self._project_dir / "memories"
        target_dir.mkdir(parents=True, exist_ok=True)

        metadata = PromptMetadata(
            domain=safe_name.replace(".md", ""),
            tags=tags or [],
            priority=priority,
            source="agent",
        )

        target = target_dir / safe_name
        existing = self._entries.get(f"memories/{safe_name}")
        if existing:
            metadata.version = existing.metadata.version + 1
            metadata.created = existing.metadata.created

        self._write_atomic(target, _make_frontmatter(metadata) + "\n" + content)

        entry = PromptEntry(
            path=f"memories/{safe_name}",
            metadata=metadata,
            content=content,
            category="memory",
        )
        self._entries[entry.path] = entry

        # 检查 token 容量
        self._enforce_memory_budget()

        self._update_manifest()
        logger.info("已记忆: %s (%d tokens)", safe_name, entry.token_estimate)
        return entry

    def list_memories(self) -> list[PromptEntry]:
        """列出所有 memory 条目。"""
        self._ensure_loaded()
        return sorted(
            [e for e in self._entries.values() if e.category == "memory"],
            key=lambda e: e.metadata.updated,
            reverse=True,
        )

    def delete_memory(self, name: str) -> bool:
        """删除一条 memory。"""
        self._ensure_loaded()
        if not name.endswith(".md"):
            name += ".md"

        key = f"memories/{name}"
        if key not in self._entries:
            return False

        entry = self._entries.pop(key)

        # 删除文件（检查两个目录）
        for base in (self._project_dir, self._user_dir):
            f = base / key
            if f.exists():
                f.unlink()
                break

        self._update_manifest()
        logger.info("已删除记忆: %s", name)
        return True

    # ═══════════════════════════════════════════════════════════
    # 渐进式加载
    # ═══════════════════════════════════════════════════════════

    def load_relevant_prompts(
        self,
        user_input: str,
        token_budget: int = _MAX_CONTEXT_TOKENS,
    ) -> list[PromptEntry]:
        """按相关性渐进式加载提示词。

        策略：
        1. Master 始终第一位（最高优先级）
        2. Domain + Memory 按评分排序
        3. 贪心纳入：累计 token ≤ budget

        Args:
            user_input: 用户输入文本，用于相关性评分
            token_budget: 总 token 预算

        Returns:
            按优先级排序的条目列表
        """
        self._ensure_loaded()

        selected: list[PromptEntry] = []
        used_tokens = 0

        # 1. Master 始终第一
        if self._master:
            master_tokens = min(self._master.token_estimate, _MASTER_BUDGET)
            selected.append(self._master)
            used_tokens += master_tokens

        # 2. 对所有 domain + memory 评分
        candidates = [
            e for e in self._entries.values()
            if e.category in ("domain", "memory")
        ]

        scored: list[tuple[float, PromptEntry]] = []
        for entry in candidates:
            score = self._score_relevance(entry, user_input)
            if score > 0:
                scored.append((score, entry))

        # 按评分降序
        scored.sort(key=lambda x: -x[0])

        # 3. 贪心纳入
        for score, entry in scored:
            entry_budget = (
                _DOMAIN_ENTRY_BUDGET if entry.category == "domain" else _MEMORY_ENTRY_BUDGET
            )
            entry_tokens = min(entry.token_estimate, entry_budget)
            if used_tokens + entry_tokens <= token_budget:
                selected.append(entry)
                used_tokens += entry_tokens

        if len(selected) > 1:
            names = [
                e.metadata.domain
                for e in selected
                if e.category != "master"
            ]
            logger.debug("渐进式加载: %s (tokens=%d/%d)", ", ".join(names), used_tokens, token_budget)

        return selected

    def _score_relevance(self, entry: PromptEntry, user_input: str) -> float:
        """计算条目与用户输入的相关性评分。"""
        score: float = 0.0
        input_lower = user_input.lower()
        meta = entry.metadata
        # 用关键词提取做词边界匹配，避免 "git" 匹配 "digitization"
        input_keywords = set(_extract_keywords(user_input))

        # 1. Domain 名命中（作为完整词匹配）
        if meta.domain.lower() in input_keywords:
            score += _DOMAIN_NAME_MATCH

        # 2. Tag 命中（每个 tag 独立匹配）
        for tag in meta.tags:
            if tag.lower() in input_keywords:
                score += _TAG_MATCH

        # 3. 内容关键词重叠
        content_words = set(_extract_keywords(entry.content))
        overlap = input_keywords & content_words
        score += len(overlap) * _KEYWORD_OVERLAP

        # 4. 优先级系数
        if meta.priority == "high":
            score *= 1.5
        elif meta.priority == "low":
            score *= 0.5

        return score

    # ═══════════════════════════════════════════════════════════
    # 格式化
    # ═══════════════════════════════════════════════════════════

    def format_for_context(self, entries: list[PromptEntry]) -> str:
        """将条目列表格式化为可注入 LLM 上下文的文本。

        格式：
        [系统提示词 - 领域知识]
        <domain content>

        [系统提示词 - 长期记忆]
        <memory content>
        """
        domains = [e for e in entries if e.category == "domain"]
        memories = [e for e in entries if e.category == "memory"]

        parts: list[str] = []

        if domains:
            domain_text = "\n\n".join(e.content for e in domains)
            parts.append(f"[系统提示词 - 领域知识]\n{domain_text}")

        if memories:
            memory_text = "\n\n".join(e.content for e in memories)
            parts.append(f"[系统提示词 - 长期记忆]\n{memory_text}")

        return "\n\n".join(parts) if parts else ""

    # ═══════════════════════════════════════════════════════════
    # 版本管理
    # ═══════════════════════════════════════════════════════════

    def list_versions(self) -> list[dict[str, Any]]:
        """列出主提示词的归档版本。"""
        self._ensure_loaded()
        versions: list[dict[str, Any]] = []

        for base in (self._project_dir, self._user_dir):
            vdir = base / "versions"
            if not vdir.exists():
                continue
            for f in sorted(vdir.glob("system-v*.md"), reverse=True):
                stat = f.stat()
                versions.append({
                    "path": str(f),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })

        return versions

    def rebuild_index(self) -> None:
        """重建 manifest.json。"""
        self._update_manifest()

    # ═══════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            if self.is_initialized:
                self._load_all()
            else:
                self.ensure_initialized()

    def _parse_file(self, filepath: Path, *, category: str) -> PromptEntry | None:
        """解析单个 .md 文件。"""
        try:
            text = filepath.read_text(encoding="utf-8")
            metadata_dict, body = _parse_frontmatter(text)
            if metadata_dict:
                # 过滤多余键，防止拼写错误（如 domian:）或自定义字段导致 TypeError
                from dataclasses import fields
                known = {f.name for f in fields(PromptMetadata)}
                filtered = {k: v for k, v in metadata_dict.items() if k in known}
                metadata = PromptMetadata(**filtered)
            else:
                metadata = PromptMetadata()
            # 用文件名推断 domain（仅当元数据中未明确设置时）
            if not metadata_dict or "domain" not in metadata_dict:
                stem = filepath.stem
                if category == "domain":
                    metadata.domain = stem
                elif category == "memory":
                    metadata.domain = stem
            return PromptEntry(
                path=str(filepath.relative_to(filepath.parent.parent)),
                metadata=metadata,
                content=body.strip(),
                category=category,
            )
        except Exception as e:
            logger.warning("解析提示词文件失败 %s: %s", filepath, e)
            return None

    def _archive_version(self, entry: PromptEntry) -> None:
        """归档当前 master 到 versions/ 目录。"""
        target_dir = self._project_dir / "versions"
        if not target_dir.parent.exists():
            target_dir = self._user_dir / "versions"
        target_dir.mkdir(parents=True, exist_ok=True)

        ver = entry.metadata.version
        archive_path = target_dir / f"system-v{ver}.md"
        full_text = _make_frontmatter(entry.metadata) + "\n" + entry.content
        self._write_atomic(archive_path, full_text)
        logger.info("已归档主提示词版本 v%d → %s", ver, archive_path)

    def _update_manifest(self) -> None:
        """更新 manifest.json 索引文件。"""
        manifest: dict[str, Any] = {
            "updated": datetime.now().isoformat(),
            "master_version": self._master.metadata.version if self._master else 0,
            "domains": [],
            "memories": [],
        }

        for entry in self._entries.values():
            item = {
                "path": entry.path,
                "domain": entry.metadata.domain,
                "tags": entry.metadata.tags,
                "priority": entry.metadata.priority,
                "tokens": entry.token_estimate,
                "updated": entry.metadata.updated,
            }
            if entry.category == "domain":
                manifest["domains"].append(item)
            elif entry.category == "memory":
                manifest["memories"].append(item)

        # 同时写入两个目录（确保一致性）
        for base in (self._user_dir, self._project_dir):
            if base.exists():
                try:
                    self._write_atomic(
                        base / "manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2),
                    )
                except Exception as e:
                    logger.warning("manifest.json 写入失败 (%s): %s", base, e)

    def _write_atomic(self, path: Path, content: str) -> None:
        """原子写入：写临时文件 → 关闭 → os.replace() → 目标路径。

        使用 try/finally 确保 fd 只关闭一次，
        避免 os.replace 失败后在已关闭的 fd 上再次 close 覆盖原始错误。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            suffix=".tmp",
            prefix=path.stem + "-",
            dir=str(path.parent),
        )
        try:
            try:
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)  # 无论写入成功与否都关闭一次
            os.replace(tmp, str(path))  # 原子替换（不在 try/finally 内，异常直接上抛）
        except Exception:
            # os.replace 失败 → 清理临时文件
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _enforce_memory_budget(self) -> None:
        """执行 memory token 容量限制，超出时驱逐低优先级旧条目。"""
        memories = [
            e for e in self._entries.values()
            if e.category == "memory"
        ]
        # 按 priority（低→高）和 updated（旧→新）排序
        priority_order = {"low": 0, "medium": 1, "high": 2}
        memories.sort(
            key=lambda e: (priority_order.get(e.metadata.priority, 1), e.metadata.updated)
        )

        # Token 预算驱逐
        total_tokens = sum(e.token_estimate for e in memories)
        while total_tokens > _MAX_MEMORY_TOKENS and len(memories) > 1:
            victim = memories.pop(0)
            self._entries.pop(victim.path, None)
            total_tokens -= victim.token_estimate
            # 删除文件
            for base in (self._project_dir, self._user_dir):
                f = base / victim.path
                if f.exists():
                    f.unlink()
                    break
            logger.info("Memory 容量驱逐: %s (tokens=%d)", victim.metadata.domain, victim.token_estimate)

        # 数量上限保护
        if len(memories) > _MAX_MEMORY_FILES:
            excess = memories[:len(memories) - _MAX_MEMORY_FILES]
            for victim in excess:
                self._entries.pop(victim.path, None)
                for base in (self._project_dir, self._user_dir):
                    f = base / victim.path
                    if f.exists():
                        f.unlink()
                        break
                logger.info("Memory 数量驱逐: %s", victim.metadata.domain)


# ═══════════════════════════════════════════════════════════════
# YAML Frontmatter 工具函数
# ═══════════════════════════════════════════════════════════════

_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter，返回 (metadata_dict, body_text)。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    try:
        metadata = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        logger.warning("YAML frontmatter 解析失败: %s", e)
        return {}, text

    body = text[m.end():].strip()
    return metadata, body


def _make_frontmatter(metadata: PromptMetadata) -> str:
    """将 PromptMetadata 序列化为 YAML frontmatter 字符串。"""
    data = {
        "version": metadata.version,
        "created": metadata.created,
        "updated": metadata.updated,
        "domain": metadata.domain,
        "tags": metadata.tags,
        "priority": metadata.priority,
        "source": metadata.source,
    }
    yaml_str = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return f"---\n{yaml_str}---"


# ═══════════════════════════════════════════════════════════════
# 关键词提取
# ═══════════════════════════════════════════════════════════════

def _extract_keywords(text: str) -> list[str]:
    """提取文本关键词用于相关性匹配。

    支持中英文混合：
    - 英文：按空白分词，过滤长度 < 2 的词
    - 中文：提取连续中文字符段，生成 2-4 gram
    """
    words: list[str] = []

    # 英文/数字关键词
    for word in text.lower().split():
        word = word.strip(".,!?;:()[]{}\"'，。！？；：（）【】「」#*")
        if len(word) >= 2:
            words.append(word)

    # 中文 n-gram
    chinese_segments = re.findall(r'[一-鿿]+', text)
    for seg in chinese_segments:
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                words.append(seg[i:i + n])

    return words
