"""
OmniAgent 库管理器 — 实时 MCP 注册中心 + Skill 云端库。

MCP 发现: Smithery 注册中心（7000+ 服务器）为主，GitHub YAML 精品补充。
Skill 发现: GitHub 云端 YAML。
每次 /mcp discover 实时查询注册中心，始终获取最新列表。
本地缓存用于离线兜底。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ── Smithery 注册中心 API ───────────────────────────────

_SMITHERY_API = "https://registry.smithery.ai/servers"
_SMITHERY_PAGE_SIZE = 100  # 每页拉取数量

# ── GitHub 云端库（精品补充 / 非 Smithery 覆盖）─────────

_GITHUB_MCP_URL = (
    "https://raw.githubusercontent.com/xianyu-sheng/Omniagent/main/"
    "library/mcp_library.yaml"
)
_GITHUB_SKILL_URL = (
    "https://raw.githubusercontent.com/xianyu-sheng/Omniagent/main/"
    "library/skill_library.yaml"
)

# ── 本地缓存 ────────────────────────────────────────────

_USER_DATA = Path.home() / ".omniagent"
_CACHE_MCP = _USER_DATA / "mcp_library.cache.json"      # 缓存 Smithery 合并结果
_CACHE_SKILL = _USER_DATA / "skill_library.cache.yaml"   # 缓存 Skill YAML
_CACHE_TTL = 1800  # 30 分钟缓存有效期


# ── 数据模型 ────────────────────────────────────────────

@dataclass
class MCPServerEntry:
    """MCP 库中的一个条目。"""
    name: str              # 唯一标识（qualifiedName）
    display_name: str = "" # 显示名
    description: str = ""
    command: str = ""      # 本地 npx 命令
    args: list[str] = field(default_factory=list)
    url: str = ""          # 远程 SSE URL（Smithery remote）
    env: dict[str, str] = field(default_factory=dict)
    category: str = ""
    homepage: str = ""
    note: str = ""
    source: str = ""       # "smithery" | "github"


@dataclass
class SkillEntry:
    name: str
    description: str
    category: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    params: list[dict[str, str]] = field(default_factory=list)
    system_prompt: str = ""


# ── HTTP 拉取 ───────────────────────────────────────────

def _http_fetch(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """从 URL 拉取文本。返回 (成功, 内容或错误信息)。"""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "OmniAgent-CLI")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"网络不可达"
    except Exception as e:
        return False, str(e)


def _http_fetch_json(url: str, timeout: float = 10.0) -> tuple[bool, Any]:
    """拉取并解析 JSON。"""
    ok, text = _http_fetch(url, timeout)
    if not ok:
        return False, text
    try:
        return True, json.loads(text)
    except json.JSONDecodeError as e:
        return False, f"JSON 解析失败: {e}"


def fetch_smithery_detail(qualified_name: str) -> tuple[bool, dict | str]:
    """查询 Smithery 服务器详情（含 connections/deploymentUrl）。"""
    url = f"https://registry.smithery.ai/servers/{qualified_name}"
    ok, data = _http_fetch_json(url, timeout=8.0)
    if not ok:
        return False, data
    if not isinstance(data, dict):
        return False, "非预期的响应格式"
    return True, data


# ── 缓存 ────────────────────────────────────────────────

def _cache_valid(cache_path: Path) -> bool:
    try:
        if not cache_path.exists():
            return False
        return (time.time() - cache_path.stat().st_mtime) < _CACHE_TTL
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
# MCP 库 — Smithery + GitHub 双源
# ══════════════════════════════════════════════════════════

class MCPLibrary:
    """MCP 服务器库：实时查询 Smithery 注册中心，GitHub YAML 补充。"""

    def __init__(self) -> None:
        self._entries: list[MCPServerEntry] = []
        self._by_name: dict[str, MCPServerEntry] = {}
        self._source: str = "未加载"
        self._error: str = ""

    # ── 加载 ──────────────────────────────────────────

    def load(self, force_refresh: bool = False) -> None:
        """按优先级: Smithery 实时 → 本地缓存 → GitHub 离线。"""
        self._entries.clear()
        self._by_name.clear()
        self._error = ""

        # 缓存有效且不强制刷新 → 直接用缓存
        if not force_refresh and _cache_valid(_CACHE_MCP):
            if self._load_from_cache():
                return

        # 1. 实时查询 Smithery
        smithery_ok = self._fetch_smithery()

        # 2. GitHub YAML 补充（始终拉取，覆盖 + 补充非 Smithery 条目）
        self._merge_github_yaml()

        if self._entries:
            # 写入缓存
            self._save_cache()
            if smithery_ok:
                self._source = "cloud"
            else:
                self._source = "github"
            return

        # 3. Smithery + GitHub 都不可达 → 缓存兜底
        if self._load_from_cache():
            return

        # 4. 内置离线兜底
        self._load_fallback()
        self._source = "fallback"

    def _fetch_smithery(self) -> bool:
        """从 Smithery API 拉取最新 MCP 列表。返回是否成功。"""
        all_servers: list[dict] = []
        page = 1

        try:
            # 拉最多 5 页（500 个服务器），过多会影响加载速度
            for _ in range(5):
                url = f"{_SMITHERY_API}?pageSize={_SMITHERY_PAGE_SIZE}&page={page}"
                ok, data = _http_fetch_json(url, timeout=10.0)
                if not ok:
                    if page == 1:
                        self._error = str(data)
                        return False
                    break  # 后续页失败也接受已有数据

                servers = data.get("servers", [])
                if not servers:
                    break

                for raw in servers:
                    qn = raw.get("qualifiedName", "")
                    if not qn:
                        continue

                    conns = raw.get("connections", [])
                    first_conn = conns[0] if conns else {}
                    is_remote = raw.get("remote", False)

                    entry = MCPServerEntry(
                        name=qn,
                        display_name=raw.get("displayName", qn),
                        description=raw.get("description", ""),
                        url=first_conn.get("deploymentUrl", "") if is_remote else "",
                        homepage=raw.get("homepage", "") or f"https://smithery.ai/server/{qn}",
                        source="smithery",
                    )
                    all_servers.append(entry)

                # 检查是否还有下一页
                pagination = data.get("pagination", {})
                if page >= pagination.get("totalPages", 1):
                    break
                page += 1

            # 合并到主列表（Smithery 条目优先）
            for e in all_servers:
                if e.name not in self._by_name:
                    self._entries.append(e)
                    self._by_name[e.name] = e

            logger.info(f"Smithery: 拉取 {len(all_servers)} 个 MCP 服务器")
            return True

        except Exception as e:
            self._error = str(e)
            logger.debug(f"Smithery 拉取失败: {e}")
            return False

    def _merge_github_yaml(self) -> None:
        """从 GitHub 拉取精品 MCP 列表，覆盖 / 补充 Smithery 没有的。"""
        ok, text = _http_fetch(_GITHUB_MCP_URL, timeout=8.0)
        if not ok:
            logger.debug(f"GitHub MCP YAML 拉取失败: {text}")
            return

        try:
            data = yaml.safe_load(text) or {}
        except Exception as e:
            logger.debug(f"GitHub MCP YAML 解析失败: {e}")
            return

        for raw in data.get("servers", []):
            name = raw.get("name", "")
            if not name:
                continue

            entry = MCPServerEntry(
                name=name,
                display_name=raw.get("displayName", name),
                description=raw.get("description", ""),
                command=raw.get("command", ""),
                args=raw.get("args", []),
                url=raw.get("url", ""),
                env=raw.get("env", {}),
                category=raw.get("category", ""),
                homepage=raw.get("homepage", ""),
                note=raw.get("note", ""),
                source="github",
            )

            if name in self._by_name:
                # GitHub 条目覆盖 Smithery（我们有更精准的安装配置）
                old = self._by_name[name]
                idx = self._entries.index(old)
                self._entries[idx] = entry
                self._by_name[name] = entry
            else:
                self._entries.append(entry)
                self._by_name[name] = entry

        logger.info(f"GitHub MCP: 合并 {len(data.get('servers', []))} 个条目")

    # ── 缓存 ──────────────────────────────────────────

    def _save_cache(self) -> None:
        try:
            _USER_DATA.mkdir(parents=True, exist_ok=True)
            data = {
                "updated_at": time.time(),
                "servers": [
                    {
                        "name": e.name,
                        "display_name": e.display_name,
                        "description": e.description,
                        "command": e.command,
                        "args": e.args,
                        "url": e.url,
                        "env": e.env,
                        "category": e.category,
                        "homepage": e.homepage,
                        "note": e.note,
                        "source": e.source,
                    }
                    for e in self._entries
                ],
            }
            _CACHE_MCP.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"保存 MCP 缓存失败: {e}")

    def _load_from_cache(self) -> bool:
        try:
            if not _CACHE_MCP.exists():
                return False
            raw = json.loads(_CACHE_MCP.read_text(encoding="utf-8"))
            for s in raw.get("servers", []):
                e = MCPServerEntry(
                    name=s.get("name", ""),
                    display_name=s.get("display_name", ""),
                    description=s.get("description", ""),
                    command=s.get("command", ""),
                    args=s.get("args", []),
                    url=s.get("url", ""),
                    env=s.get("env", {}),
                    category=s.get("category", ""),
                    homepage=s.get("homepage", ""),
                    note=s.get("note", ""),
                    source=s.get("source", ""),
                )
                self._entries.append(e)
                self._by_name[e.name] = e
            self._source = "cache"
            return True
        except Exception as e:
            logger.debug(f"读取 MCP 缓存失败: {e}")
            return False

    def _load_fallback(self) -> None:
        """内置离线兜底（最精简的几个）。"""
        fallback = [
            MCPServerEntry(
                name="fetch", display_name="Fetch",
                description="HTTP 网页抓取 — 将网页内容转为 Markdown",
                command="npx", args=["-y", "@modelcontextprotocol/server-fetch"],
                category="网络", source="fallback",
            ),
            MCPServerEntry(
                name="filesystem", display_name="Filesystem",
                description="安全的文件系统读写",
                command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "."],
                category="系统", source="fallback",
            ),
            MCPServerEntry(
                name="brave", display_name="Brave Search",
                description="Brave 搜索引擎 — 网页搜索",
                command="npx", args=["-y", "@modelcontextprotocol/server-brave-search"],
                env={"BRAVE_API_KEY": "<你的 Brave API Key>"},
                category="搜索", source="fallback",
            ),
        ]
        for e in fallback:
            self._entries.append(e)
            self._by_name[e.name] = e

    # ── 查询 ──────────────────────────────────────────

    @property
    def source_label(self) -> str:
        if self._source == "cloud":
            return "☁️ Smithery 实时"
        elif self._source == "github":
            return "☁️ GitHub 云端"
        elif self._source == "cache":
            try:
                age = int(time.time() - _CACHE_MCP.stat().st_mtime)
            except Exception:
                age = 0
            return f"💾 缓存（{age // 60} 分钟前）"
        else:
            return "📦 内置（离线）"

    def discover(self, keyword: str = "") -> list[MCPServerEntry]:
        """浏览或模糊搜索。"""
        if keyword:
            kw = keyword.lower()
            return sorted(
                [e for e in self._entries
                 if kw in e.name.lower()
                 or kw in e.description.lower()
                 or kw in e.category.lower()
                 or kw in e.display_name.lower()],
                key=lambda e: e.name,
            )
        return sorted(self._entries, key=lambda e: e.name)

    def get(self, name: str) -> MCPServerEntry | None:
        """精确或模糊获取。"""
        if name in self._by_name:
            return self._by_name[name]
        nl = name.lower()
        for e in self._entries:
            if e.name.lower() == nl:
                return e
        for e in self._entries:
            if e.name.lower().startswith(nl):
                return e
        return None


# ══════════════════════════════════════════════════════════
# Skill 库 — GitHub 云端
# ══════════════════════════════════════════════════════════

class SkillLibrary:
    """Skill 云端库。"""

    def __init__(self) -> None:
        self._entries: list[SkillEntry] = []
        self._by_name: dict[str, SkillEntry] = {}
        self._source: str = "未加载"
        self._error: str = ""

    def load(self, force_refresh: bool = False) -> None:
        self._entries.clear()
        self._by_name.clear()
        self._error = ""

        if not force_refresh and _cache_valid(_CACHE_SKILL):
            if self._load_from_cache():
                return

        ok, text = _http_fetch(_GITHUB_SKILL_URL, timeout=8.0)
        if ok:
            self._parse_and_index(text)
            self._source = "cloud"
            try:
                _USER_DATA.mkdir(parents=True, exist_ok=True)
                _CACHE_SKILL.write_text(text, encoding="utf-8")
            except Exception:
                pass
            return

        self._error = text

        if self._load_from_cache():
            return

        self._load_fallback()
        self._source = "fallback"

    def _parse_and_index(self, raw: str) -> None:
        try:
            data = yaml.safe_load(raw) or {}
        except Exception:
            return
        for s in data.get("skills", []):
            name = s.get("name", "")
            if not name:
                continue
            e = SkillEntry(
                name=name,
                description=s.get("description", ""),
                category=s.get("category", ""),
                steps=s.get("steps", []),
                params=s.get("params", []),
                system_prompt=s.get("system_prompt", ""),
            )
            self._entries.append(e)
            self._by_name[name] = e

    def _load_from_cache(self) -> bool:
        try:
            if not _CACHE_SKILL.exists():
                return False
            self._parse_and_index(_CACHE_SKILL.read_text(encoding="utf-8"))
            self._source = "cache"
            return bool(self._entries)
        except Exception:
            return False

    def _load_fallback(self) -> None:
        e = SkillEntry(
            name="git-commit",
            description="AI 生成 commit message（内置离线兜底）",
            category="开发",
            steps=[
                {"type": "command", "action": "git diff --cached", "output_var": "diff"},
                {"type": "llm", "prompt": "根据以下 git diff 生成一条规范的 commit message（中文 50 字以内）：\n{diff}"},
            ],
        )
        self._entries.append(e)
        self._by_name[e.name] = e

    @property
    def source_label(self) -> str:
        if self._source == "cloud":
            return "☁️ GitHub 云端"
        elif self._source == "cache":
            try:
                age = int(time.time() - _CACHE_SKILL.stat().st_mtime)
            except Exception:
                age = 0
            return f"💾 缓存（{age // 60} 分钟前）"
        else:
            return "📦 内置（离线）"

    def discover(self, keyword: str = "") -> list[SkillEntry]:
        if keyword:
            kw = keyword.lower()
            return sorted(
                [e for e in self._entries
                 if kw in e.name.lower()
                 or kw in e.description.lower()
                 or kw in (e.category or "").lower()],
                key=lambda e: e.name,
            )
        return sorted(self._entries, key=lambda e: e.name)

    def get(self, name: str) -> SkillEntry | None:
        if name in self._by_name:
            return self._by_name[name]
        nl = name.lower()
        for e in self._entries:
            if e.name.lower() == nl:
                return e
        for e in self._entries:
            if e.name.lower().startswith(nl):
                return e
        return None

    def install(self, name: str) -> tuple[bool, str]:
        entry = self.get(name)
        if not entry:
            return False, f"未找到 Skill '{name}'"

        from omniagent.repl.skill_manager import SkillManager

        mgr = SkillManager()
        try:
            mgr.create(
                name=entry.name,
                description=entry.description,
                steps=entry.steps,
                system_prompt=entry.system_prompt,
                params=entry.params,
            )
            return True, f"✅ Skill '{entry.name}' 已安装（{len(entry.steps)} 个步骤）"
        except Exception as e:
            return False, f"安装失败: {e}"

    def refresh_repl_skills(self) -> None:
        try:
            from omniagent.repl.skill_manager import SkillManager
            mgr = SkillManager()
            mgr.load()
        except Exception as e:
            logger.debug(f"刷新 Skill 失败: {e}")


# ── 全局单例 ────────────────────────────────────────────

_mcp_lib: MCPLibrary | None = None


def get_mcp_library(force_refresh: bool = False) -> MCPLibrary:
    global _mcp_lib
    if _mcp_lib is None or force_refresh:
        _mcp_lib = MCPLibrary()
        _mcp_lib.load(force_refresh=force_refresh)
    return _mcp_lib


_skill_lib: SkillLibrary | None = None


def get_skill_library(force_refresh: bool = False) -> SkillLibrary:
    global _skill_lib
    if _skill_lib is None or force_refresh:
        _skill_lib = SkillLibrary()
        _skill_lib.load(force_refresh=force_refresh)
    return _skill_lib
