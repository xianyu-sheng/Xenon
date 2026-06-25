"""A2A AgentCard 协议 — 子 Agent 自描述能力名片。

每个子 Agent 类型通过 YAML 文件声明自己的能力和约束，
主 Agent 通过 discover_agents 工具动态发现可用类型。

设计原则：
- 新增子 Agent 类型 = 新增一个 YAML 文件，零代码改动
- 能力变化 = 编辑 YAML，重启即生效
- 不硬编码任何 Agent 类型
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CARDS_DIR = Path.home() / ".omniagent" / "agents"

# ── 默认种子名片 ─────────────────────────────────────────────

_SEED_CARDS: list[dict[str, Any]] = [
    {
        "name": "code-explorer",
        "display_name": "代码探索器",
        "description": "只读代码搜索与探索 — 搜索文件、grep 内容、分析代码结构。适合「帮我在项目中找到所有用到 X 的地方」这类任务。",
        "capabilities": {
            "tools": ["read_file", "list_files", "search_files", "grep", "glob", "code_index", "ast_analyze"],
            "models": ["inherit"],
            "read_only": True,
        },
        "constraints": {
            "max_iterations": 6,
            "timeout": 120,
        },
        "version": "1.0",
    },
    {
        "name": "file-writer",
        "display_name": "文件操作器",
        "description": "文件创建/修改/批量编辑 — 可以读写文件但不能执行命令。适合「帮我创建/修改这些文件」这类任务。",
        "capabilities": {
            "tools": ["write_file", "edit_file", "batch_write", "batch_edit", "create_directory", "read_file", "list_files"],
            "models": ["inherit"],
            "read_only": False,
        },
        "constraints": {
            "max_iterations": 10,
            "timeout": 300,
        },
        "version": "1.0",
    },
    {
        "name": "test-runner",
        "display_name": "测试执行器",
        "description": "运行测试/命令并报告结果 — 可以执行 shell 命令但不能修改文件。适合「帮我跑一下测试看看有没有问题」这类任务。",
        "capabilities": {
            "tools": ["command", "read_file", "search_files", "grep", "list_files"],
            "models": ["inherit"],
            "read_only": False,
        },
        "constraints": {
            "max_iterations": 5,
            "timeout": 180,
        },
        "version": "1.0",
    },
    {
        "name": "general-purpose",
        "display_name": "通用 Agent",
        "description": "完整工具集的通用子 Agent — 继承父 Agent 的全部能力。适合复杂多步骤子任务。",
        "capabilities": {
            "tools": [],  # 空列表 = 继承全部工具
            "models": ["inherit"],
            "read_only": False,
        },
        "constraints": {
            "max_iterations": 8,
            "timeout": 300,
        },
        "version": "1.0",
    },
]

# ── 数据结构 ─────────────────────────────────────────────────


@dataclass
class AgentCard:
    """子 Agent 的自描述名片。

    声明自己的名称、能力范围（可用工具、模型偏好、是否只读）、
    执行约束（最大迭代次数、超时时间）和版本。
    """

    name: str
    display_name: str
    description: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    version: str = "1.0"
    source: str = "local"  # "local" | "remote" | "custom"

    @property
    def is_read_only(self) -> bool:
        return bool(self.capabilities.get("read_only", False))

    @property
    def tool_list(self) -> list[str]:
        return list(self.capabilities.get("tools", []))

    @property
    def max_iterations(self) -> int:
        return int(self.constraints.get("max_iterations", 8))

    @property
    def timeout(self) -> int:
        return int(self.constraints.get("timeout", 300))

    def resolve_tools(self, all_available: dict[str, Any]) -> dict[str, Any]:
        """根据名片声明的能力裁剪工具集。

        Args:
            all_available: 全部可用工具 {name: tool_schema}

        Returns:
            裁剪后的工具集。若名片 tools 为空列表则返回全部。
        """
        requested = self.tool_list
        if not requested:
            return dict(all_available)  # 继承全部

        resolved: dict[str, Any] = {}
        for tool_name in requested:
            if tool_name in all_available:
                resolved[tool_name] = all_available[tool_name]
            else:
                logger.debug("名片 %s 请求的工具 '%s' 不可用，已跳过", self.name, tool_name)
        return resolved


# ── Registry ─────────────────────────────────────────────────


class AgentCardRegistry:
    """AgentCard 注册中心。

    从 ~/.omniagent/agents/*.yaml 加载所有名片，
    提供发现、查询、注册功能。
    """

    def __init__(self, cards_dir: Path | None = None) -> None:
        self.cards_dir = cards_dir or _CARDS_DIR
        self.cards: dict[str, AgentCard] = {}
        self._loaded = False

    # ── 加载 ──────────────────────────────────────────────

    def ensure_seeded(self) -> None:
        """确保默认名片存在（首次运行时写入）。"""
        if not self.cards_dir.exists():
            self.cards_dir.mkdir(parents=True, exist_ok=True)

        for seed in _SEED_CARDS:
            card_path = self.cards_dir / f"{seed['name']}.yaml"
            if not card_path.exists():
                try:
                    card_path.write_text(
                        yaml.dump(seed, allow_unicode=True, default_flow_style=False, sort_keys=False),
                        encoding="utf-8",
                    )
                    logger.info("已写入默认 AgentCard: %s", seed["name"])
                except Exception as e:
                    logger.warning("写入默认名片失败 %s: %s", seed["name"], e)

    def load(self) -> None:
        """从磁盘加载所有名片。"""
        if self._loaded:
            return

        self.cards.clear()

        if not self.cards_dir.exists():
            logger.debug("AgentCard 目录不存在: %s，将写入默认名片", self.cards_dir)
            self.ensure_seeded()

        if not self.cards_dir.exists():
            return

        for f in sorted(self.cards_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                if data and "name" in data:
                    card = AgentCard(
                        name=data["name"],
                        display_name=data.get("display_name", data["name"]),
                        description=data.get("description", ""),
                        capabilities=data.get("capabilities", {}),
                        constraints=data.get("constraints", {}),
                        version=str(data.get("version", "1.0")),
                        source=data.get("source", "local"),
                    )
                    self.cards[card.name] = card
                    logger.debug("已加载 AgentCard: %s (v%s)", card.name, card.version)
            except (yaml.YAMLError, KeyError) as e:
                logger.warning("AgentCard 解析失败 %s: %s", f.name, e)

        self._loaded = True
        logger.info("已加载 %d 张 AgentCard", len(self.cards))

    # ── 查询 ──────────────────────────────────────────────

    def discover(self) -> list[AgentCard]:
        """返回所有已注册的 AgentCard（按名称排序）。"""
        self._ensure_loaded()
        return sorted(self.cards.values(), key=lambda c: c.name)

    def get(self, name: str) -> AgentCard | None:
        """按名称获取名片。"""
        self._ensure_loaded()
        return self.cards.get(name)

    def list_names(self) -> list[str]:
        """列出所有名片名称。"""
        self._ensure_loaded()
        return sorted(self.cards.keys())

    # ── 注册 ──────────────────────────────────────────────

    def register(self, card: AgentCard, *, persist: bool = True) -> None:
        """注册一张名片（运行时或持久化）。

        Args:
            card: 名片对象
            persist: 是否持久化到磁盘 YAML
        """
        self.cards[card.name] = card

        if persist:
            self.cards_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "name": card.name,
                "display_name": card.display_name,
                "description": card.description,
                "capabilities": card.capabilities,
                "constraints": card.constraints,
                "version": card.version,
                "source": card.source,
            }
            card_path = self.cards_dir / f"{card.name}.yaml"
            card_path.write_text(
                yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        logger.info("已注册 AgentCard: %s (v%s)", card.name, card.version)

    def unregister(self, name: str) -> bool:
        """注销并删除一张名片。"""
        if name not in self.cards:
            return False
        del self.cards[name]
        card_path = self.cards_dir / f"{name}.yaml"
        if card_path.exists():
            card_path.unlink()
        return True

    # ── 工具裁剪 ──────────────────────────────────────────

    def resolve_tools(
        self,
        card_name: str,
        all_available: dict[str, Any],
    ) -> dict[str, Any]:
        """根据名片名称裁剪工具集。

        Args:
            card_name: 名片名称（如 "code-explorer"）
            all_available: 全部可用工具 {name: schema}

        Returns:
            裁剪后的工具集；若名片不存在则返回全部
        """
        card = self.get(card_name)
        if card is None:
            logger.debug("名片 %s 不存在，返回全部工具", card_name)
            return dict(all_available)
        return card.resolve_tools(all_available)

    # ── 内部 ──────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.ensure_seeded()
            self.load()

    def reload(self) -> None:
        """重新加载所有名片（手动编辑 YAML 后使用）。"""
        self._loaded = False
        self.load()


# ── 全局单例 ────────────────────────────────────────────────

_card_registry: AgentCardRegistry | None = None


def get_card_registry() -> AgentCardRegistry:
    """获取全局 AgentCardRegistry 单例。"""
    global _card_registry
    if _card_registry is None:
        _card_registry = AgentCardRegistry()
        _card_registry.ensure_seeded()
        _card_registry.load()
    return _card_registry
