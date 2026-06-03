"""
Shortcut Manager — 自定义快捷指令管理器。

用户可以创建自己的 /xxx 命令，封装常用操作。
存储位置: ~/.omniagent/shortcuts.yaml
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_SHORTCUTS_PATH = Path.home() / ".omniagent" / "shortcuts.yaml"


@dataclass
class Shortcut:
    """一条快捷指令。"""
    name: str                    # 命令名，如 "deploy"
    description: str = ""        # 描述
    steps: list[str] = field(default_factory=list)  # 要执行的命令列表
    params: list[dict[str, str]] = field(default_factory=list)  # 参数定义
    cwd: str | None = None       # 工作目录


class ShortcutManager:
    """快捷指令管理器。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _SHORTCUTS_PATH
        self.shortcuts: dict[str, Shortcut] = {}
        self.load()

    def load(self) -> None:
        """从磁盘加载快捷指令。"""
        if not self.path.exists():
            self.shortcuts = {}
            return

        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            for item in data.get("shortcuts", []):
                sc = Shortcut(**item)
                self.shortcuts[sc.name] = sc
            logger.info(f"加载了 {len(self.shortcuts)} 个快捷指令")
        except Exception as e:
            logger.warning(f"加载快捷指令失败: {e}")
            self.shortcuts = {}

    def save(self) -> None:
        """保存到磁盘。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "shortcuts": [asdict(s) for s in self.shortcuts.values()],
        }
        self.path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

    def create(
        self,
        name: str,
        description: str,
        steps: list[str],
        params: list[dict[str, str]] | None = None,
        cwd: str | None = None,
    ) -> Shortcut:
        """创建一个快捷指令。"""
        # 清理名称
        name = name.lstrip("/").lower().replace(" ", "_")

        shortcut = Shortcut(
            name=name,
            description=description,
            steps=steps,
            params=params or [],
            cwd=cwd,
        )
        self.shortcuts[name] = shortcut
        self.save()
        logger.info(f"创建快捷指令: /{name}")
        return shortcut

    def remove(self, name: str) -> bool:
        """删除一个快捷指令。"""
        name = name.lstrip("/").lower()
        if name in self.shortcuts:
            del self.shortcuts[name]
            self.save()
            return True
        return False

    def list_all(self) -> list[Shortcut]:
        """列出所有快捷指令。"""
        return list(self.shortcuts.values())

    def get(self, name: str) -> Shortcut | None:
        """获取一个快捷指令。"""
        return self.shortcuts.get(name.lstrip("/").lower())

    def execute(self, name: str, args: str = "") -> str:
        """执行一个快捷指令。"""
        shortcut = self.get(name)
        if not shortcut:
            return f"❌ 快捷指令 /{name} 不存在"

        # 参数替换
        param_values = self._parse_args(args, shortcut.params)

        results = []
        for step in shortcut.steps:
            # 替换参数占位符 {param_name}
            cmd = step
            for key, value in param_values.items():
                cmd = cmd.replace(f"{{{key}}}", value)

            results.append(f"$ {cmd}")

            try:
                if sys.platform == "win32":
                    proc = subprocess.run(
                        ["powershell", "-Command", cmd],
                        capture_output=True, text=True, timeout=60,
                        cwd=shortcut.cwd,
                    )
                else:
                    proc = subprocess.run(
                        ["/bin/bash", "-c", cmd],
                        capture_output=True, text=True, timeout=60,
                        cwd=shortcut.cwd,
                    )

                if proc.stdout.strip():
                    results.append(proc.stdout.strip())
                if proc.stderr.strip():
                    results.append(f"[stderr] {proc.stderr.strip()}")
                if proc.returncode != 0:
                    results.append(f"[退出码: {proc.returncode}]")
                    break  # 命令失败时停止

            except subprocess.TimeoutExpired:
                results.append(f"[超时] {cmd}")
                break
            except Exception as e:
                results.append(f"[错误] {e}")
                break

        return "\n".join(results)

    def _parse_args(self, args: str, params: list[dict[str, str]]) -> dict[str, str]:
        """解析用户传入的参数。"""
        result = {}

        # 设置默认值
        for p in params:
            if "default" in p:
                result[p["name"]] = p["default"]

        # 解析用户输入
        if args.strip():
            parts = args.split()
            for i, p in enumerate(params):
                if i < len(parts):
                    result[p["name"]] = parts[i]

        return result
