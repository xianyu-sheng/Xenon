"""Core 配置模块。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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


def get_core_config() -> CoreConfig:
    """获取默认配置。"""
    return CoreConfig()
