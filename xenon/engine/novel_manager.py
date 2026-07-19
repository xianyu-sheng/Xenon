"""
NovelManager — 多小说项目管理器。

每本小说完全隔离：独立目录、独立上下文、独立记忆。
AI 对每本小说的理解随创作不断累积（context.md），
确保像真正的作家一样持续创作而不走偏。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """将中文/英文名称转为安全的目录 slug。"""
    import hashlib
    # 英文名直接用
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip()).strip("-").lower()
    # 中文名用 hash 前缀 + 原名
    has_chinese = bool(re.search(r"[一-鿿]", name))
    if has_chinese:
        short_hash = hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
        # 保留中文名作为可读部分，但用 hash 确保唯一
        slug = re.sub(r"[^\w一-鿿]+", "-", name.strip()).strip("-").lower()
        return f"{slug}-{short_hash}" if slug else f"novel-{short_hash}"
    return ascii_part or "unnamed"


class NovelProject:
    """单本小说的数据模型。"""

    def __init__(self, base_dir: Path, meta: dict[str, Any]) -> None:
        self.base_dir = base_dir
        self.meta = meta

    @property
    def slug(self) -> str:
        return self.meta.get("slug", "")

    @property
    def title(self) -> str:
        return self.meta.get("title", "未命名")

    @property
    def genre(self) -> str:
        return self.meta.get("genre", "")

    @property
    def created_at(self) -> str:
        return self.meta.get("created_at", "")

    @property
    def updated_at(self) -> str:
        return self.meta.get("updated_at", "")

    def characters_path(self) -> Path:
        return self.base_dir / "characters.json"

    def world_path(self) -> Path:
        return self.base_dir / "world.json"

    def outline_path(self) -> Path:
        return self.base_dir / "outline.md"

    def style_path(self) -> Path:
        return self.base_dir / "style.md"

    def summary_path(self) -> Path:
        return self.base_dir / "summary.md"

    def context_path(self) -> Path:
        return self.base_dir / "context.md"

    def chapters_dir(self) -> Path:
        return self.base_dir / "chapters"

    def chapter_count(self) -> int:
        d = self.chapters_dir()
        if not d.exists():
            return 0
        return len(list(d.glob("*.md")))

    def total_words(self) -> int:
        total = 0
        d = self.chapters_dir()
        if d.exists():
            for f in d.glob("*.md"):
                try:
                    total += len(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return total

    def get_all_context(self) -> str:
        """加载完整项目上下文，用于注入 LLM prompt。"""
        parts = []

        files = [
            (self.meta_path(), "小说基本信息"),
            (self.characters_path(), "角色卡"),
            (self.world_path(), "世界观设定"),
            (self.outline_path(), "故事大纲"),
            (self.style_path(), "风格指南"),
            (self.summary_path(), "内容摘要"),
            (self.context_path(), "创作记忆（AI 的累积理解）"),
        ]

        for filepath, label in files:
            if filepath.exists():
                try:
                    content = filepath.read_text(encoding="utf-8").strip()
                    if content:
                        # context.md 不截断（它是核心记忆）
                        if filepath == self.context_path():
                            parts.append(f"### {label}\n{content}")
                        elif len(content) > 2000:
                            parts.append(f"### {label}\n{content[:2000]}\n... (已截断)")
                        else:
                            parts.append(f"### {label}\n{content}")
                except Exception:
                    pass

        return "\n\n".join(parts) if parts else ""

    def meta_path(self) -> Path:
        return self.base_dir / "meta.json"


class NovelManager:
    """
    多小说项目管理器。

    职责：
    - 管理小说注册表（registry.json）
    - 创建/删除/切换小说项目
    - 自动识别用户指的是哪本小说
    - 每次操作后更新创作记忆（context.md）
    """

    def __init__(self, base_dir: str = ".novel") -> None:
        self.base_dir = Path(base_dir)
        self.projects_dir = self.base_dir / "projects"
        self.registry_path = self.base_dir / "registry.json"
        self._registry: dict[str, Any] = self._load_registry()
        self._current_slug: str | None = self._registry.get("active")

    # ── 注册表管理 ──────────────────────────────────────────

    def _load_registry(self) -> dict[str, Any]:
        if self.registry_path.exists():
            try:
                return json.loads(self.registry_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"读取 registry.json 失败: {e}")
        return {"active": None, "novels": {}}

    def _save_registry(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps(self._registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 小说 CRUD ───────────────────────────────────────────

    def create_novel(
        self,
        title: str,
        genre: str = "",
        description: str = "",
    ) -> NovelProject:
        """创建新小说项目，返回项目对象。"""
        slug = _slugify(title)

        # 检查重名
        if slug in self._registry.get("novels", {}):
            # 加数字后缀
            base_slug = slug
            i = 2
            while f"{base_slug}-{i}" in self._registry.get("novels", {}):
                i += 1
            slug = f"{base_slug}-{i}"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        project_dir = self.projects_dir / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "chapters").mkdir(exist_ok=True)

        meta = {
            "slug": slug,
            "title": title,
            "genre": genre,
            "description": description,
            "created_at": now,
            "updated_at": now,
        }

        # 写入 meta.json
        (project_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 初始化模板文件
        templates = {
            "characters.json": "[]",
            "world.json": "{}",
            "outline.md": f"# {title} — 大纲\n\n> 在这里规划你的故事结构\n",
            "style.md": f"# {title} — 风格指南\n\n> 在这里定义写作规范\n",
            "summary.md": f"# {title} — 内容摘要\n\n> 尚未开始创作\n",
            "context.md": f"""# {title} — 创作记忆

> 这是 AI 对这本小说的累积理解，随创作不断增长。
> 每次操作后会自动更新，确保创作连贯、不走偏。

## 基本信息
- **标题**: {title}
- **类型**: {genre or '待定'}
- **创建时间**: {now}
- **状态**: 刚刚创建，尚未开始创作

## 故事核心
（待填充）

## 角色状态
（待填充）

## 关键决策记录
（暂无）

## 待解决的问题
（暂无）
""",
        }
        for filename, content in templates.items():
            (project_dir / filename).write_text(content, encoding="utf-8")

        # 更新注册表
        self._registry.setdefault("novels", {})[slug] = {
            "title": title,
            "genre": genre,
            "created_at": now,
        }
        self._registry["active"] = slug
        self._current_slug = slug
        self._save_registry()

        logger.info(f"创建小说项目: {title} (slug={slug})")
        return NovelProject(project_dir, meta)

    def get_novel(self, slug: str) -> NovelProject | None:
        """获取指定小说项目。"""
        project_dir = self.projects_dir / slug
        meta_path = project_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return NovelProject(project_dir, meta)
        except Exception:
            return None

    def get_current(self) -> NovelProject | None:
        """获取当前活跃的小说项目。"""
        if self._current_slug:
            return self.get_novel(self._current_slug)
        return None

    def switch_novel(self, slug: str) -> NovelProject | None:
        """切换当前活跃的小说。"""
        project = self.get_novel(slug)
        if not project:
            return None
        self._current_slug = slug
        self._registry["active"] = slug
        self._save_registry()
        logger.info(f"切换到小说: {project.title} ({slug})")
        return project

    def list_novels(self) -> list[dict[str, Any]]:
        """列出所有小说，返回摘要列表。"""
        result = []
        for slug, info in self._registry.get("novels", {}).items():
            project = self.get_novel(slug)
            if project:
                result.append({
                    "slug": slug,
                    "title": info.get("title", slug),
                    "genre": info.get("genre", ""),
                    "chapters": project.chapter_count(),
                    "words": project.total_words(),
                    "updated_at": project.updated_at,
                    "is_active": slug == self._current_slug,
                })
        return result

    def delete_novel(self, slug: str) -> bool:
        """删除小说项目。"""
        if slug not in self._registry.get("novels", {}):
            return False

        project_dir = self.projects_dir / slug
        if project_dir.exists():
            shutil.rmtree(project_dir)

        del self._registry["novels"][slug]
        if self._current_slug == slug:
            self._current_slug = None
            self._registry["active"] = None
        self._save_registry()
        logger.info(f"删除小说: {slug}")
        return True

    # ── 自动识别 ────────────────────────────────────────────

    def detect_novel(self, user_input: str) -> NovelProject | None:
        """
        根据用户输入自动识别是哪本小说。

        规则（按优先级）：
        1. 显式指定："切换到xxx"、"打开xxx"、"关于xxx"
        2. 提到小说标题
        3. 只有一本小说时自动选择
        """
        novels = self._registry.get("novels", {})
        if not novels:
            return None

        # 规则 1: 显式切换指令
        switch_patterns = [
            r"(?:切换|打开|换到|去|回到|继续写|继续创作).{0,5}(.+)",
            r"(?:关于|对于|针对).{0,3}(.+?)(?:的|这个|那本)",
        ]
        for pattern in switch_patterns:
            match = re.search(pattern, user_input)
            if match:
                keyword = match.group(1).strip()
                # 在注册表中查找匹配
                for slug, info in novels.items():
                    title = info.get("title", "")
                    if keyword in title or keyword == slug:
                        return self.get_novel(slug)

        # 规则 2: 输入中包含小说标题
        for slug, info in novels.items():
            title = info.get("title", "")
            if len(title) >= 2 and title in user_input:
                return self.get_novel(slug)

        # 规则 3: 只有一本小说时自动选择
        if len(novels) == 1:
            slug = next(iter(novels))
            return self.get_novel(slug)

        # 规则 4: 使用当前活跃小说
        if self._current_slug:
            return self.get_novel(self._current_slug)

        return None

    # ── 上下文累积 ──────────────────────────────────────────

    def update_context(
        self,
        slug: str,
        operation: str,
        detail: str,
    ) -> None:
        """
        更新小说的创作记忆。

        每次操作后调用，将新的理解追加到 context.md。
        这确保 AI 的认知随创作不断深化。
        """
        project = self.get_novel(slug)
        if not project:
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        context_path = project.context_path()

        # 读取现有内容
        existing = ""
        if context_path.exists():
            try:
                existing = context_path.read_text(encoding="utf-8")
            except Exception:
                pass

        # 追加新记录
        entry = f"\n\n## [{now}] {operation}\n{detail}\n"
        context_path.write_text(existing + entry, encoding="utf-8")

        # 更新 meta 的 updated_at
        try:
            meta = json.loads(project.meta_path().read_text(encoding="utf-8"))
            meta["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            project.meta_path().write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

        # 更新 registry 的 updated_at
        if slug in self._registry.get("novels", {}):
            self._registry["novels"][slug]["updated_at"] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            self._save_registry()

        logger.info(f"更新创作记忆: {slug} — {operation}")

    def update_summary(self, slug: str, new_content: str) -> None:
        """更新小说的内容摘要（追加模式）。"""
        project = self.get_novel(slug)
        if not project:
            return

        summary_path = project.summary_path()
        existing = ""
        if summary_path.exists():
            try:
                existing = summary_path.read_text(encoding="utf-8")
            except Exception:
                pass

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n\n### [{now}]\n{new_content}\n"
        summary_path.write_text(existing + entry, encoding="utf-8")
        logger.info(f"更新内容摘要: {slug}")
