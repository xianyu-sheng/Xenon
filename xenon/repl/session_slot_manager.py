"""
Session Slot Manager — 多槽位会话管理，防止 /resume 覆盖。

设计：
- 每个会话分配独立槽位：_auto_<timestamp>_<random>.json
- 惰性分配：首次 auto_save 时才创建槽位
- 清理策略：保留最近 3 个 + 3 天内
- /resume 后重置槽位，下次保存到新槽位
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SESSIONS_DIR = Path.home() / ".xenon" / "sessions"
SLOT_KEEP_RECENT = 3
SLOT_KEEP_DAYS = 3


@dataclass
class SessionSlot:
    """会话槽位元数据"""
    session_id: str
    filepath: Path
    created_at: float
    last_saved_at: float


class SessionSlotManager:
    """
    多槽位会话管理器

    职责：
    1. 惰性分配槽位（首次保存时）
    2. 保存到当前槽位
    3. 清理旧槽位
    4. 重置槽位（/resume 后）
    """

    def __init__(self, sessions_dir: Path = SESSIONS_DIR):
        self.sessions_dir = sessions_dir
        self.current_slot_id: Optional[str] = None
        self.current_slot_path: Optional[Path] = None
        self.created_at: Optional[float] = None

    def ensure_slot(self) -> Path:
        """
        确保当前槽位存在（惰性分配）

        如果槽位已存在，返回现有路径。
        如果槽位不存在，分配新槽位。

        并发安全：如果槽位文件已存在（极低概率），重新生成。

        Returns:
            当前槽位的文件路径
        """
        if self.current_slot_path is not None:
            return self.current_slot_path

        # 分配新槽位（带冲突检测）
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        max_attempts = 10
        for _ in range(max_attempts):
            timestamp = int(time.time())
            random_suffix = secrets.token_hex(2)  # 4 个十六进制字符
            slot_id = f"{timestamp}_{random_suffix}"
            slot_path = self.sessions_dir / f"_auto_{slot_id}.json"

            # 检查文件是否已存在（并发安全）
            if not slot_path.exists():
                self.current_slot_id = slot_id
                self.current_slot_path = slot_path
                self.created_at = time.time()
                return self.current_slot_path

        # 极端情况：10 次都冲突，使用更长的随机后缀
        timestamp = int(time.time())
        random_suffix = secrets.token_hex(4)  # 8 个十六进制字符
        self.current_slot_id = f"{timestamp}_{random_suffix}"
        self.current_slot_path = self.sessions_dir / f"_auto_{self.current_slot_id}.json"
        self.created_at = time.time()

        return self.current_slot_path

    def reset_slot(self):
        """
        重置当前槽位

        用于 /resume 后，下次 auto_save 会分配新槽位。
        """
        self.current_slot_id = None
        self.current_slot_path = None
        self.created_at = None

    def get_current_slot_id(self) -> Optional[str]:
        """获取当前槽位 ID（用于显示）"""
        return self.current_slot_id

    def cleanup_old_slots(
        self,
        keep_recent: int = SLOT_KEEP_RECENT,
        keep_days: int = SLOT_KEEP_DAYS,
    ) -> tuple[int, int]:
        """
        清理旧的自动保存槽位

        策略：
        1. 保留最近 N 个槽位（按修改时间排序）
        2. 保留 M 天内的槽位
        3. 删除其他槽位

        Args:
            keep_recent: 保留最近 N 个槽位
            keep_days: 保留 M 天内的槽位

        Returns:
            (删除数量, 保留数量)
        """
        if not self.sessions_dir.exists():
            return 0, 0

        threshold_ts = time.time() - (keep_days * 86400)

        # 收集所有自动保存槽位（_auto_*.json 格式）
        slots = []
        for filepath in self.sessions_dir.glob("_auto_*.json"):
            # 跳过旧的单槽位文件 _auto.json（向后兼容）
            if filepath.name == "_auto.json":
                continue

            try:
                stat = filepath.stat()
                slots.append({
                    "filepath": filepath,
                    "mtime": stat.st_mtime,
                })
            except OSError:
                # 文件损坏或不可访问，加入清理列表
                slots.append({
                    "filepath": filepath,
                    "mtime": 0,
                })

        # 按修改时间排序（最新的在前）
        slots.sort(key=lambda s: s["mtime"], reverse=True)

        deleted = 0
        kept = 0

        for i, slot in enumerate(slots):
            filepath = slot["filepath"]
            mtime = slot["mtime"]

            # 保留条件
            should_keep = (
                i < keep_recent or  # 最近 N 个
                mtime >= threshold_ts or  # M 天内
                mtime == 0  # 损坏的文件（稍后再判断）
            )

            if should_keep:
                kept += 1
            else:
                try:
                    filepath.unlink()
                    deleted += 1
                except OSError:
                    # 删除失败，保留
                    kept += 1

        return deleted, kept

    def list_auto_slots(self) -> list[dict]:
        """
        列出所有自动保存槽位（用于调试/状态显示）

        Returns:
            槽位信息列表，按时间倒序
        """
        if not self.sessions_dir.exists():
            return []

        slots = []
        for filepath in self.sessions_dir.glob("_auto_*.json"):
            if filepath.name == "_auto.json":
                continue

            try:
                stat = filepath.stat()
                # 从文件名提取 session_id
                # 格式：_auto_<timestamp>_<random>.json
                name_parts = filepath.stem.split("_", 2)  # ['', 'auto', '<timestamp>_<random>']
                session_id = name_parts[2] if len(name_parts) > 2 else "unknown"

                slots.append({
                    "session_id": session_id,
                    "filepath": filepath,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                })
            except (OSError, IndexError):
                continue

        slots.sort(key=lambda s: s["mtime"], reverse=True)
        return slots
