"""Core 配置模块 — 统一配置入口。

OmniAgentConfig 是所有子系统的单一配置来源。
凭证由 provider_registry 统一加载，路径在此集中管理。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CoreConfig:
    """Core daemon 配置。

    可通过环境变量覆盖默认值。
    """

    # IPC 配置
    host: str = field(default_factory=lambda: os.environ.get("OMNIAGENT_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("OMNIAGENT_PORT", "9501")))
    max_connections: int = 10

    # 数据目录
    data_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("OMNIAGENT_DATA_DIR", ".omniagent")
    ))
    sessions_dir: Path = field(default_factory=lambda: Path(".omniagent") / "sessions")
    runs_dir: Path = field(default_factory=lambda: Path(".omniagent") / "runs")

    # 运行时限制
    max_steps: int = 50
    tool_timeout_s: int = 60
    permission_timeout_s: float = 60.0

    # 日志
    log_level: str = field(default_factory=lambda: os.environ.get("OMNIAGENT_LOG_LEVEL", "INFO"))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)


@dataclass
class OmniAgentConfig:
    """统一配置 — 所有子系统的单一配置来源。

    启动时一次加载，通过调用链传递，替代各模块独立加载配置。
    """

    # 凭证（由 provider_registry 统一加载）
    credentials: dict[str, str] = field(default_factory=dict)

    # 核心路径
    memory_path: Path = field(default_factory=lambda: Path.home() / ".omniagent" / "memory.json")
    prompts_dir: Path = field(default_factory=lambda: Path.home() / ".omniagent" / "prompts")
    credentials_path: Path = field(default_factory=lambda: Path.home() / ".omniagent" / "credentials.yaml")

    @classmethod
    def load(cls) -> OmniAgentConfig:
        """从所有来源加载统一配置。"""
        config = cls()

        # 加载凭证
        try:
            from omniagent.repl.provider_registry import load_credentials
            config.credentials = load_credentials()
        except Exception:
            pass

        return config


def get_core_config() -> CoreConfig:
    """获取默认 CoreConfig。"""
    return CoreConfig()

