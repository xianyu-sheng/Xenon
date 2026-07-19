"""原子文件写入工具（A9 / A10）。

提供 atomic_write_text：写临时文件 + os.replace，防止写入中途崩溃损坏原文件；
可选 backup 备份原文件到 <path>.bak；可选 mode 对写入文件 chmod（用于凭据/会话文件 0600）。
供 code_editor / refactor / model_registry / session / memory 等统一调用。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    backup: bool = False,
    mode: int | None = None,
) -> None:
    """原子写入文本文件。

    Args:
        path: 目标文件路径。
        content: 要写入的文本内容。
        backup: True 时写入前把原文件备份到 <path>.bak（LLM 编辑等场景回滚）。
        mode: 非 None 时写入后对目标文件 chmod（如 0o600 用于凭据/会话文件，A10）。

    临时文件与目标同目录，保证 os.replace 在同一文件系统内为原子操作；
    若写入或替换失败，清理临时文件且原文件不被触碰。
    """
    path = Path(path)
    if backup and path.exists():
        path.with_name(path.name + ".bak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
