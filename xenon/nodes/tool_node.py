"""
ToolNode — 本地工具执行节点。

支持操作类型：
1. command — 执行终端命令（Bash/PowerShell）
2. write_file — 将内容写入文件
3. read_file — 读取文件内容
4. list_files — 目录遍历（支持 glob 模式）
5. search_files — 文件内容搜索（类似 grep）
6. git — Git 操作封装
7. web_fetch — HTTP 抓取网页内容

所有操作支持 {variable} 上下文变量替换。
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, quote

import httpx

from xenon.engine.context import AgentContext
from xenon.utils.llm_client import _create_http_client
from xenon.nodes.base import BaseNode
from xenon.utils.atomic_write import atomic_write_bytes, atomic_write_text
from xenon.utils.github_reference import parse_github_reference

logger = logging.getLogger(__name__)

# ── 动态工具注册表 ──────────────────────────────────────────
# 存储通过 register_tool 注册的自定义工具
# key: 工具名, value: {"handler": callable, "description": str, "params": dict}
_DYNAMIC_TOOLS: dict[str, dict] = {}
_GITHUB_DEFAULT_BRANCH_CACHE: dict[str, str] = {}


def register_dynamic_tool(name: str, handler, description: str, params: dict) -> None:
    """注册一个动态工具，之后可通过 ToolNode(action_type=name) 调用。"""
    _DYNAMIC_TOOLS[name] = {
        "handler": handler,
        "description": description,
        "params": params,
    }
    logger.info(f"[DynamicTool] 注册工具: {name}")


def get_dynamic_tool_schema(name: str) -> dict | None:
    """获取动态工具的描述（用于注入到 LLM 工具列表）。"""
    info = _DYNAMIC_TOOLS.get(name)
    if not info:
        return None
    return {"name": name, "description": info["description"], "params": info["params"]}


def list_dynamic_tools() -> list[str]:
    """列出所有已注册的动态工具名。"""
    return list(_DYNAMIC_TOOLS.keys())


# ── register_tool 安全策略 ──────────────────────────────
# 模式1（python_function）允许导入的模块前缀白名单。
# 默认仅允许项目自身模块；可通过环境变量 XENON_REGISTER_MODULE_ALLOW
# （逗号分隔）显式追加额外的安全模块前缀，供高级用户扩展。
_EXTRA_ALLOWED_MODULES = os.environ.get("XENON_REGISTER_MODULE_ALLOW", "")
_ALLOWED_MODULE_PREFIXES: tuple[str, ...] = ("xenon.",) + tuple(
    p.strip() + "." for p in _EXTRA_ALLOWED_MODULES.split(",") if p.strip()
)

# 危险模块顶层名：即便落在允许前缀内也一律拒绝导入（防 os.system / subprocess 等 RCE）。
_DANGEROUS_MODULE_TOPS: frozenset[str] = frozenset({
    "os", "subprocess", "builtins", "importlib", "sys", "shutil",
    "ctypes", "pickle", "socket", "ssl", "multiprocessing", "pty",
})

# 内置 action_type 集合：动态工具注册时禁止重名（防内置工具名劫持）。
# 注意：若新增内置 action_type，需同步本集合（与 ToolNode.execute 的 handlers 字典保持一致）。
_BUILTIN_ACTION_TYPES: frozenset[str] = frozenset({
    "command", "write_file", "read_file", "list_files", "search_files",
    "git", "web_fetch", "edit_file", "create_directory", "batch_write",
    "batch_edit", "code_index", "ast_analyze", "refactor", "diff_preview",
    "mcp_call", "github_fetch", "weather", "datetime", "register_tool",
    "clone_repo", "lsp_goto_def", "lsp_find_refs", "lsp_hover",
    "lsp_diagnostics", "lsp_symbols",
})


def _last_error_lines(stderr: str, max_chars: int = 300) -> str:
    """从 stderr 尾部提取错误信息。

    git 等工具把 info 行（如 "Cloning into..."）输出在前，
    真正的错误（如 "fatal: ..."）在末尾。取后 max_chars 字符。
    """
    stderr = stderr.strip()
    if len(stderr) <= max_chars:
        return stderr
    return "…" + stderr[-(max_chars - 1):]


_CLOCK_LINE_RE = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")
_DURATION_RE = re.compile(r"\d+\s*(?:时|小时).{0,4}\d*\s*分|\d+\s*分")
_TRAIN_CODE_RE = re.compile(r"[A-Z]{1,3}\d{1,6}", re.IGNORECASE)
_CHINESE_NUMBER = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                   "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _small_chinese_number(value: str) -> int | None:
    """Parse the small Chinese numerals used in clock expressions."""
    if value.isdigit():
        return int(value)
    if value in _CHINESE_NUMBER:
        return _CHINESE_NUMBER[value]
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CHINESE_NUMBER.get(left, 1) if left else 1
        ones = _CHINESE_NUMBER.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _clock_value(hour_text: str, minute_text: str, period: str) -> str | None:
    hour = _small_chinese_number(hour_text)
    if hour is None or not 0 <= hour <= 23:
        return None
    minute = 30 if minute_text == "半" else int(minute_text or 0)
    if not 0 <= minute <= 59:
        return None
    if period in {"下午", "傍晚", "晚上", "夜里", "夜间"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in {"凌晨", "早上", "上午"} and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _infer_time_window(text: str) -> tuple[str | None, str | None]:
    """Extract a deterministic HH:MM window from natural-language constraints."""
    source = text or ""
    range_match = re.search(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(?:-|~|～|至|到)\s*"
        r"([01]?\d|2[0-3]):([0-5]\d)\b",
        source,
    )
    if range_match:
        return (
            f"{int(range_match.group(1)):02d}:{range_match.group(2)}",
            f"{int(range_match.group(3)):02d}:{range_match.group(4)}",
        )

    clock_pattern = re.compile(
        r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间)?\s*"
        r"(?P<hour>[零〇一二两三四五六七八九十\d]{1,3})\s*"
        r"(?:点|时)(?:(?P<minute>半|[0-5]?\d)\s*分?)?\s*"
        r"(?P<direction>之后|以后|及以后|起|之前|以前|及以前|前)"
    )
    start: str | None = None
    end: str | None = None
    for match in clock_pattern.finditer(source):
        value = _clock_value(
            match.group("hour"),
            match.group("minute") or "",
            match.group("period") or "",
        )
        if value is None:
            continue
        if match.group("direction") in {"之后", "以后", "及以后", "起"}:
            start = value
        else:
            end = value

    for match in re.finditer(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(之后|以后|及以后|起|之前|以前|及以前)",
        source,
    ):
        value = f"{int(match.group(1)):02d}:{match.group(2)}"
        if match.group(3) in {"之后", "以后", "及以后", "起"}:
            start = value
        else:
            end = value
    return start, end


def _prefilter_time_records(
    text: str,
    *,
    start_time: str | None,
    end_time: str | None,
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    """Select schedule-like records by departure time before output truncation."""
    start_minutes = int(start_time[:2]) * 60 + int(start_time[3:]) if start_time else 0
    end_minutes = int(end_time[:2]) * 60 + int(end_time[3:]) if end_time else 23 * 60 + 59

    # Prefer preserving structured API/MCP responses when they contain a list
    # of objects with a recognizable departure-time field.
    try:
        parsed_json = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_json = None
    time_keys = {
        "departure_time", "depart_time", "start_time", "departuretime",
        "departtime", "starttime", "出发时间", "发车时间", "开车时间",
    }

    def filter_json(value: Any) -> tuple[Any, int, int]:
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            detected = 0
            selected_items: list[dict[str, Any]] = []
            for item in value:
                raw_time = next(
                    (field for key, field in item.items() if str(key).casefold() in time_keys),
                    None,
                )
                match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", str(raw_time or ""))
                if not match:
                    continue
                detected += 1
                minutes = int(match.group(1)) * 60 + int(match.group(2))
                if start_minutes <= minutes <= end_minutes:
                    selected_items.append(item)
            if detected:
                return selected_items, detected, len(selected_items)
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            detected_strings = 0
            selected_strings: list[str] = []
            for item in value:
                # 12306-style records are pipe-delimited strings whose first
                # clock token is the departure time.
                match = re.search(r"(?:^|\|)([01]?\d|2[0-3]):([0-5]\d)(?:\||$)", item)
                if not match:
                    continue
                detected_strings += 1
                minutes = int(match.group(1)) * 60 + int(match.group(2))
                if start_minutes <= minutes <= end_minutes:
                    selected_strings.append(item)
            if detected_strings:
                return selected_strings, detected_strings, len(selected_strings)
        if isinstance(value, dict):
            for key, child in value.items():
                filtered_child, detected, matched = filter_json(child)
                if detected:
                    copied = dict(value)
                    copied[key] = filtered_child
                    return copied, detected, matched
        return value, 0, 0

    if parsed_json is not None:
        filtered_json, detected, matched = filter_json(parsed_json)
        if detected:
            filtered = json.dumps(filtered_json, ensure_ascii=False, indent=2)
            truncated = len(filtered) > max_chars
            if truncated:
                filtered = filtered[:max_chars] + "\n... (筛选后的 JSON 已截断)"
            return filtered, {
                "prefilter_applied": True,
                "filter_type": "time_window_json",
                "filter_start_time": start_time,
                "filter_end_time": end_time,
                "records_detected": detected,
                "records_matched": matched,
                "original_content_length": len(text),
                "filtered_content_length": len(filtered),
                "filtered_content_truncated": truncated,
            }

    lines = text.splitlines()
    candidates: list[tuple[int, int]] = []
    all_clock_lines: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _CLOCK_LINE_RE.fullmatch(line)
        if not match:
            continue
        minutes = int(match.group(1)) * 60 + int(match.group(2))
        all_clock_lines.append((index, minutes))
        lookahead = [item.strip() for item in lines[index + 1:index + 18] if item.strip()][:7]
        if (
            any(_DURATION_RE.search(item) for item in lookahead)
            and any(_TRAIN_CODE_RE.fullmatch(item) for item in lookahead)
        ):
            candidates.append((index, minutes))

    # Structured departure rows are preferred.  A generic time-line fallback
    # still preserves useful tail context for unfamiliar list formats.
    record_starts = candidates if candidates else (
        all_clock_lines if len(all_clock_lines) >= 3 else []
    )
    if not record_starts:
        return text, {}
    selected: list[str] = []
    for offset, (line_index, minutes) in enumerate(record_starts):
        if not start_minutes <= minutes <= end_minutes:
            continue
        next_index = (
            record_starts[offset + 1][0]
            if offset + 1 < len(record_starts)
            else min(len(lines), line_index + 30)
        )
        selected.append("\n".join(lines[line_index:next_index]).strip())

    label = f"{start_time or '00:00'}–{end_time or '23:59'}"
    header = (
        f"[已在截断前应用时间筛选：{label}；"
        f"识别 {len(record_starts)} 条记录，命中 {len(selected)} 条]\n"
    )
    filtered = header + ("\n\n".join(selected) if selected else "未发现符合时间条件的记录。")
    truncated = len(filtered) > max_chars
    if truncated:
        suffix = "\n\n... (筛选后的内容仍超出字符预算，已截断)"
        filtered = filtered[:max(0, max_chars - len(suffix))] + suffix
    return filtered, {
        "prefilter_applied": True,
        "filter_type": "time_window",
        "filter_start_time": start_time,
        "filter_end_time": end_time,
        "records_detected": len(record_starts),
        "records_matched": len(selected),
        "original_content_length": len(text),
        "filtered_content_length": len(filtered),
        "filtered_content_truncated": truncated,
    }


def _prefilter_keyword_context(
    text: str,
    *,
    query: str,
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    """Keep bounded line windows around selective keyword matches."""
    tokens = re.findall(r"[A-Za-z0-9_.-]{2,}|[\u3400-\u9fff]{2,}", query)
    stopwords = {"查询", "结果", "数据", "内容", "信息", "筛选", "过滤", "search", "query"}
    tokens = [token.casefold() for token in tokens if token.casefold() not in stopwords][:8]
    if not tokens:
        return text, {}
    lines = text.splitlines()
    matches = [
        index for index, line in enumerate(lines)
        if any(token in line.casefold() for token in tokens)
    ]
    # A keyword present almost everywhere (for example a destination name in
    # every timetable row) does not reduce the response and should not replace
    # the normal truncation path.
    if not matches or len(matches) > max(30, int(len(lines) * 0.6)):
        return text, {}
    selected_indexes: set[int] = set()
    for index in matches:
        selected_indexes.update(range(max(0, index - 4), min(len(lines), index + 7)))
    selected = "\n".join(
        line for index, line in enumerate(lines) if index in selected_indexes
    ).strip()
    header = f"[已在截断前应用关键词筛选：{', '.join(tokens)}；命中 {len(matches)} 处]\n"
    filtered = header + selected
    truncated = len(filtered) > max_chars
    if truncated:
        suffix = "\n\n... (筛选后的内容仍超出字符预算，已截断)"
        filtered = filtered[:max(0, max_chars - len(suffix))] + suffix
    return filtered, {
        "prefilter_applied": True,
        "filter_type": "keyword",
        "filter_query": query[:300],
        "keyword_matches": len(matches),
        "original_content_length": len(text),
        "filtered_content_length": len(filtered),
        "filtered_content_truncated": truncated,
    }


def _validate_register_module(module_path: str) -> tuple[bool, str]:
    """校验 register_tool 模式1 的 module_path 是否在安全白名单内。

    返回 (ok, reason)；ok=False 时 reason 为人类可读的拒绝原因。
    拒绝顺序：先危险模块（os/subprocess/builtins/importlib 等），再白名单前缀。
    """
    mp = (module_path or "").strip()
    if not mp:
        return False, "module_path 为空"
    top = mp.split(".", 1)[0]
    if top in _DANGEROUS_MODULE_TOPS:
        logger.warning(f"[register_tool] 拒绝导入危险模块: {mp}")
        return False, (f"安全策略禁止导入危险模块: {top}"
                       f"（os/subprocess/builtins/importlib 等不可注册）")
    if not any(mp.startswith(p) for p in _ALLOWED_MODULE_PREFIXES):
        logger.warning(f"[register_tool] 模块不在白名单: {mp}")
        return False, (f"模块 {top} 不在注册白名单内（仅允许 xenon.*，"
                       f"或通过环境变量 XENON_REGISTER_MODULE_ALLOW 显式声明）")
    return True, ""


# ── SSRF 防护（A5，§8.3.3 / §8.24.1）──────────────────────
# web_fetch 等工具抓取 URL 前必须校验目标 IP，禁止访问内网/保留/环回/链路本地地址。
_MAX_REDIRECTS = 5


class _SSRFRedirectError(Exception):
    """重定向目标未通过 SSRF 校验时抛出。"""


# ── RFC 1918 / RFC 6598 私有网络范围（显式定义，避免 ipaddress.is_private
# 误伤 198.18.0.0/15 等 IANA 基准测试保留段） ──────────────────
_RFC1918_NETWORKS: list[ipaddress._BaseNetwork] = [
    ipaddress.ip_network("10.0.0.0/8"),        # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),    # RFC 1918
    ipaddress.ip_network("100.64.0.0/10"),     # RFC 6598 CGNAT
    ipaddress.ip_network("fc00::/7"),          # IPv6 ULA
]


def _is_rfc1918_private(ip: ipaddress._BaseAddress) -> bool:
    """检查 IP 是否在 RFC 1918 / RFC 6598 私有地址段内。

    不使用 ipaddress.is_private，因为它把 198.18.0.0/15（IANA 基准测试）
    也归入 private，导致 wttr.in 等合法公网服务被 SSRF 误拦。
    """
    return any(ip in net for net in _RFC1918_NETWORKS)


def _is_internal_ip(ip: ipaddress._BaseAddress) -> bool:
    """判断 IP 是否为内网/保留/环回/链路本地/组播/未指定等不可达外部地址。"""
    return bool(
        ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified or _is_rfc1918_private(ip)
    )


def _resolve_host_ips(host: str) -> list[str]:
    """将 host 解析为 IP 字符串列表（含 IPv6）。

    host 可以是域名或字面量 IP；ipaddress.ip_address 接受十进制整数编码（如 2130706433），
    getaddrinfo 兜底处理十六进制/八进制编码与域名 DNS 解析。
    """
    # 先尝试直接当字面量 IP 解析
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    # 域名/编码 IP：DNS 解析全部地址（去重保序）
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    seen: list[str] = []
    for info in infos:
        ip_str = info[4][0]
        # getaddrinfo 对 IPv6 可能带 %scope，去掉
        ip_str = ip_str.split("%", 1)[0]
        if ip_str not in seen:
            seen.append(ip_str)
    return seen


# ── SSRF 已知安全域名白名单 ─────────────────────────────────
# 这些是公认的公共 API 服务，即使 DNS 解析到非标准 IP（如 CDN 使用的
# 198.18.0.0/15 基准测试段），也允许访问。白名单在 SSRF 校验前检查，
# 匹配则跳过 IP 级校验，作为防御纵深（defense-in-depth）的最后一道防线。
_SSRF_DOMAIN_ALLOWLIST: frozenset[str] = frozenset({
    "wttr.in",                     # 天气 API
    "weather.com.cn",              # 中国天气网
    "api.github.com",              # GitHub API
    "raw.githubusercontent.com",   # GitHub raw 内容
    "httpbin.org",                 # HTTP 测试
    "postman-echo.com",            # HTTP 测试
})


def _ssrf_check_url(url: str) -> tuple[bool, str]:
    """SSRF 校验：解析 URL 的 host，拒绝内网/保留/环回/链路本地地址。

    返回 (ok, reason)；ok=False 时 reason 为拒绝原因。覆盖 IPv4/IPv6、十进制/十六进制
    IP 编码、localhost、元数据地址 169.254.169.254、[::1] 等。
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL 解析失败: {e}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"仅允许 http/https 协议，拒绝: {scheme or '(空)'}"
    host = parsed.hostname
    if not host:
        return False, "URL 缺少 host"

    # ── 域名白名单：已知公共 API 跳过 IP 校验（防御纵深） ──
    host_lower = host.lower()
    if host_lower in _SSRF_DOMAIN_ALLOWLIST or any(
        host_lower.endswith("." + allowed) for allowed in _SSRF_DOMAIN_ALLOWLIST
    ):
        return True, ""

    ips = _resolve_host_ips(host)
    if not ips:
        return False, f"无法解析 host: {host}"
    for ip_str in ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_internal_ip(ip):
            return False, f"禁止访问内网/保留地址: {host} -> {ip_str}"
    return True, ""


def _fetch_with_redirect_check(client, url: str, headers: dict | None = None):
    """逐跳跟随重定向，每个 Location 都经过 SSRF 校验。最多 _MAX_REDIRECTS 跳。

    用于替代 httpx 的 follow_redirects=True，防止"重定向到内网"绕过起始 URL 校验。
    """
    import httpx
    current = url
    hdrs = headers or {"User-Agent": "Xenon/0.2"}
    for _ in range(_MAX_REDIRECTS + 1):
        resp = client.get(current, headers=hdrs)
        if not resp.is_redirect:
            return resp
        location = resp.headers.get("location", "")
        if not location:
            return resp
        next_url = str(httpx.URL(current).join(location))
        ok, reason = _ssrf_check_url(next_url)
        if not ok:
            raise _SSRFRedirectError(f"{next_url}: {reason}")
        current = next_url
    raise _SSRFRedirectError(f"重定向次数超过上限 ({_MAX_REDIRECTS})")


# ── 安全常量 ──────────────────────────────────────────────

# 文件大小限制
MAX_READ_SIZE = 2 * 1024 * 1024       # 2MB — 读取上限
MAX_WRITE_SIZE = 10 * 1024 * 1024     # 10MB — 写入上限
MAX_VERIFY_SIZE = 1 * 1024 * 1024     # 1MB — 回读验证上限

# 系统敏感路径黑名单（写入操作禁止）
_SENSITIVE_PATHS = [
    "c:\\windows", "c:\\program files", "c:\\programdata",
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys",
    "/var/log", "/root/.ssh", "/root/.gnupg",
]

# 用户敏感目录黑名单
_USER_SENSITIVE = [
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gh",
    ".docker/config.json", "credentials", "id_rsa", "id_ed25519",
]

# 危险命令黑名单模式
_DANGEROUS_CMD_PATTERNS = [
    # 删除根目录/系统目录
    r"rm\s+(-[rfR]+\s+)?/", r"rm\s+(-[rfR]+\s+)?~",
    r"rmdir\s+/", r"del\s+/[sfq]\s+[a-zA-Z]:\\",
    r"del\s+/[sfq]\s+C:\\",
    # 格式化
    r"\bformat\s+[a-zA-Z]:", r"\bmkfs\b",
    # 磁盘直接写入
    r"\bdd\s+if=",
    # 系统关机/重启
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    # 下载并执行
    r"curl.*\|\s*(?:bash|sh|python|node)", r"wget.*\|\s*(?:bash|sh|python|node)",
    # PowerShell 危险命令
    r"Remove-Item\s+-[rR].*C:\\", r"Format-Volume",
    r"Clear-RecycleBin\s+-Force",
    # 权限变更
    r"\bchmod\s+777\b", r"\bchown\b.*root",
]

# 危险 Git 子命令
_DANGEROUS_GIT_PATTERNS = [
    "push --force", "push -f", "reset --hard",
    "clean -fd", "clean -fXd", "checkout -- .",
    "branch -D", "reflog expire --all",
]


class SecurityError(Exception):
    """安全策略违规异常。"""
    pass


class ToolNode(BaseNode):
    """本地工具执行节点，支持命令执行、文件操作、搜索、Git 和网页抓取。"""

    def __init__(
        self,
        node_id: str,
        *,
        action_type: str = "command",
        action: str = "",
        file_path: str | None = None,
        content: str | None = None,
        output_slot: str | None = None,
        cwd: str | None = None,
        timeout: int = 60,
        default_next: str | None = None,
        encoding: str = "utf-8",
        append: bool = False,
        # list_files 参数
        pattern: str = "*",
        max_depth: int = 5,
        # search_files 参数
        search_pattern: str = "",
        file_filter: str = "",
        # git 参数
        git_command: str = "status",
        # web_fetch 参数
        url: str = "",
        start_time: str = "",
        end_time: str = "",
        # docs_fetch 参数（query 复用下方通用 query）
        max_pages: int = 4,
        max_chars: int = 12000,
        # edit_file 参数
        old_text: str = "",
        new_text: str = "",
        # 批量操作参数
        files: list[dict] | None = None,
        edits: list[dict] | None = None,
        # code_index / ast_analyze 参数
        symbol: str = "",
        query: str = "",
        # refactor 参数
        old_name: str = "",
        new_name: str = "",
        refactor_action: str = "rename",  # rename | clean_imports | analyze
        # diff_preview 参数
        # (复用 file_path, old_text, new_text)
        # mcp_call 参数
        tool_name: str = "",
        tool_args: dict | None = None,
        mcp_server: str = "",
        # github_fetch / clone_repo 参数
        repo: str = "",
        github_action: str = "list_files",  # list_files | fetch_file | fetch_readme | repo_activity
        github_path: str = "",
        branch: str = "",
        # weather 参数
        city: str = "",
        lang: str = "zh",
        # register_tool 参数
        description: str = "",
        python_function: str = "",
        command_template: str = "",
        params: dict | None = None,
        # 安全参数
        security_enabled: bool = True,
        # read_file 分段读取参数
        start_line: int | None = None,
        max_lines: int | None = None,
        # v0.6.1: LSP 工具参数
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(node_id, output_slot=output_slot, default_next=default_next)
        self.action_type = action_type
        self.action = action
        self.file_path = file_path
        self.content = content
        self.cwd = cwd
        self.timeout = timeout
        self.encoding = encoding
        self.append = append
        self.pattern = pattern
        self.max_depth = max_depth
        self.search_pattern = search_pattern
        self.file_filter = file_filter
        self.git_command = git_command
        self.url = url
        self.start_time = start_time
        self.end_time = end_time
        self.max_pages = max_pages
        self.max_chars = max_chars
        self.old_text = old_text
        self.new_text = new_text
        self.files = files or []
        self.edits = edits or []
        self.symbol = symbol
        self.query = query
        self.old_name = old_name
        self.new_name = new_name
        self.refactor_action = refactor_action
        self.tool_name = tool_name
        self.tool_args = tool_args or {}
        self.mcp_server = mcp_server
        self.repo = repo
        self.github_action = github_action
        self.github_path = github_path
        self.branch = branch
        self.city = city
        self.lang = lang
        self.description = description
        self.python_function = python_function
        self.command_template = command_template
        self.params = params or {}
        self.security_enabled = security_enabled
        self._extra_start_line = start_line
        self._extra_max_lines = max_lines
        # v0.6.1: LSP 工具参数
        self._lsp_line = line
        self._lsp_column = column

    # ── 参数规范化 ──────────────────────────────────────────

    # LLM 经常使用与 ToolNode 不同的参数名，这里统一映射。
    # 注意: pattern 是 list_files 的合法参数，不能作为 search_pattern 的别名。
    _PARAM_ALIASES: dict[str, list[str]] = {
        "file_path":      ["path", "dir", "directory", "folder", "filepath", "file", "target"],
        "action":         ["command", "cmd", "shell", "exec", "run", "execute"],
        "content":        ["text", "data", "body", "value"],
        "search_pattern": ["query", "keyword", "term", "search"],
        "file_filter":    ["filter", "glob", "filetype", "ext", "extension"],
        "old_text":       ["old", "find", "search_text", "before", "original"],
        "new_text":       ["new", "replace", "replace_text", "after", "replacement"],
        "git_command":    ["subcommand", "git_cmd", "git_subcmd"],
        "url":            ["uri", "link", "href"],
        "symbol":         ["name", "func", "function_name", "class_name", "identifier"],
        "old_name":       ["from", "before_name"],
        "new_name":       ["to", "after_name"],
        "repo":           ["repository", "repo_url", "github_url", "github_repo"],
        "github_action":  ["gh_action", "git_action"],
        "github_path":    ["gh_path", "file", "filepath"],
        "branch":         ["ref", "git_branch"],
        "city":           ["location", "place", "address"],
        "lang":           ["language", "locale"],
        # v0.6.1: LSP 参数
        "line":           ["row", "lineno", "line_number"],
        "column":         ["col", "colno", "column_number", "cursor"],
    }

    # ToolNode.__init__ 接受的所有合法参数名（不含 node_id，它是位置参数）
    _VALID_PARAMS: set[str] = {
        "action_type", "action", "file_path", "content", "output_slot",
        "cwd", "timeout", "default_next", "encoding", "append",
        "pattern", "max_depth", "search_pattern", "file_filter",
        "git_command", "url", "start_time", "end_time", "old_text", "new_text",
        "max_pages", "max_chars",
        "files", "edits", "symbol", "query",
        "old_name", "new_name", "refactor_action",
        "tool_name", "tool_args", "mcp_server",
        "repo", "github_action", "github_path", "branch",
        "city", "lang", "description", "python_function", "command_template", "params",
        "security_enabled", "start_line", "max_lines",
        # v0.6.1: LSP 工具参数
        "line", "column",
    }

    @classmethod
    def normalize_params(cls, params: dict, *, action_type: str = "") -> dict:
        """将 LLM 常用的参数别名映射为 ToolNode 接受的标准参数名，
        并过滤掉 ToolNode 不支持的未知参数（如 LLM 凭空发明的 start_line）。

        Args:
            params: LLM 返回的原始参数字典
            action_type: 工具类型（如 "list_files"），用于跳过冲突的别名

        例: {"path": ".", "query": "foo", "start_line": 100} → {"file_path": ".", "search_pattern": "foo"}
        """
        result = dict(params)

        # 1. 别名映射
        for std_name, aliases in cls._PARAM_ALIASES.items():
            if (
                std_name == "search_pattern"
                and action_type in {"docs_fetch", "web_fetch", "mcp_call"}
            ):
                aliases = [alias for alias in aliases if alias != "query"]
            if std_name in result:
                continue  # 标准名已存在，不覆盖
            for alias in aliases:
                if alias in result:
                    result[std_name] = result.pop(alias)
                    break

        # 2. 过滤未知参数（防止 ToolNode.__init__ 因未知 kwargs 崩溃）
        filtered = {k: v for k, v in result.items() if k in cls._VALID_PARAMS}
        dropped = set(result.keys()) - set(filtered.keys())
        if dropped:
            logger.warning(f"过滤未知参数: {dropped}")
        return filtered

    # ── 安全验证 ──────────────────────────────────────────

    def _get_allowed_root(self) -> Path:
        """获取允许操作的根目录。"""
        if self.cwd:
            return Path(self.cwd).resolve()
        return Path.cwd().resolve()

    def _validate_path(self, file_path: str, *, for_write: bool = False) -> Path:
        """验证文件路径是否在安全范围内。

        Args:
            file_path: 原始文件路径
            for_write: True 表示写入操作（更严格），False 表示读取操作

        Returns:
            验证通过的 Path 对象（保留原始路径格式）

        Raises:
            SecurityError: 路径不安全
        """
        if not file_path:
            raise SecurityError("文件路径不能为空")

        path = Path(file_path)
        if self.cwd and not path.is_absolute():
            path = Path(self.cwd) / path

        # 安全检查可禁用（用于测试或受信任的调用方）
        if not self.security_enabled:
            return path

        resolved = path.resolve()
        root = self._get_allowed_root()

        # v0.5.3: 允许读写 /tmp 等临时目录
        _ALLOWED_EXTRA_ROOTS = [
            Path("/tmp").resolve(),
            Path("/var/tmp").resolve(),
        ]

        # 检查路径是否在允许的根目录下
        in_allowed_root = False
        try:
            resolved.relative_to(root)
            in_allowed_root = True
        except ValueError:
            pass

        if not in_allowed_root:
            for extra in _ALLOWED_EXTRA_ROOTS:
                try:
                    resolved.relative_to(extra)
                    in_allowed_root = True
                    break
                except ValueError:
                    pass

        if not in_allowed_root:
            raise SecurityError(
                f"路径越界: {resolved} 不在允许的目录 {root} 下。"
                f"文件操作限制在项目目录内。"
            )

        # 写入操作额外检查敏感路径
        if for_write:
            # v0.5.3: 使用路径组件匹配（加前后 /），避免 "binary" 匹配 "/bin"
            resolved_lower = str(resolved).lower().replace("\\", "/")
            # 确保路径以 / 结尾，便于组件匹配
            resolved_normalized = resolved_lower.rstrip("/") + "/"
            for sensitive in _SENSITIVE_PATHS:
                sensitive_normalized = sensitive.lower().rstrip("/") + "/"
                if resolved_normalized.startswith(sensitive_normalized) or \
                   ("/" + sensitive_normalized.lstrip("/")) in resolved_normalized:
                    raise SecurityError(
                        f"禁止写入系统敏感路径: {resolved}"
                    )
            # 检查用户敏感文件（文件名精确匹配）
            name_lower = resolved.name.lower()
            for sensitive in _USER_SENSITIVE:
                if name_lower == sensitive or name_lower.endswith(sensitive):
                    raise SecurityError(
                        f"禁止写入敏感文件: {resolved}"
                    )
        else:
            # A13: 读取操作也禁止凭证等高敏感文件，防 prompt 注入诱导泄露凭证
            name_lower = resolved.name.lower()
            resolved_lower = str(resolved).lower().replace("\\", "/")
            for sensitive in _USER_SENSITIVE:
                if sensitive in name_lower or sensitive in resolved_lower:
                    raise SecurityError(
                        f"禁止读取敏感凭证文件: {resolved}"
                    )

        # 返回原始路径格式（不调用 resolve，保留 Windows 短路径等）
        return path

    def _validate_command(self, cmd: str) -> None:
        """验证命令是否安全。

        Raises:
            SecurityError: 命令不安全
        """
        if not self.security_enabled:
            return
        if not cmd or not cmd.strip():
            return

        cmd_lower = cmd.lower().strip()
        # v0.3.0+ 修复（B-1）：匹配前先剥取引号内容（防止 echo "rm -rf /" 等
        # 字符串字面量触发误报）。通用机制，不针对特定任务加白名单。
        cmd_stripped = self._strip_quoted(cmd_lower)
        for pattern in _DANGEROUS_CMD_PATTERNS:
            if re.search(pattern, cmd_stripped):
                raise SecurityError(
                    f"危险命令被拦截: 匹配到禁止模式 '{pattern}'。"
                    f"命令: {cmd[:100]}"
                )

    @staticmethod
    def _strip_quoted(cmd_lower: str) -> str:
        """去掉双/单引号内的内容（v0.3.0+ B-1 修复：字符串字面量不触发误报）。"""
        s = re.sub(r'"[^"]*"', '""', cmd_lower)
        s = re.sub(r"'[^']*'", "''", s)
        return s

    def _validate_git_command(self, git_cmd: str) -> None:
        """验证 Git 子命令是否安全。

        Raises:
            SecurityError: Git 命令不安全
        """
        if not self.security_enabled:
            return
        cmd_lower = git_cmd.lower().strip()
        for dangerous in _DANGEROUS_GIT_PATTERNS:
            if dangerous.lower() in cmd_lower:
                raise SecurityError(
                    f"危险 Git 命令被拦截: '{dangerous}'。"
                    f"完整命令: git {git_cmd[:80]}"
                )

    def execute(self, context: AgentContext) -> dict[str, Any]:
        """根据 action_type 分发到不同的处理方法。"""
        handlers = {
            "command": self._exec_command,
            "write_file": self._write_file,
            "read_file": self._read_file,
            "list_files": self._list_files,
            "search_files": self._search_files,
            "git": self._git,
            "web_fetch": self._web_fetch,
            "docs_fetch": self._docs_fetch,
            "edit_file": self._edit_file,
            "create_directory": self._create_directory,
            "batch_write": self._batch_write,
            "batch_edit": self._batch_edit,
            "code_index": self._code_index,
            "ast_analyze": self._ast_analyze,
            "refactor": self._refactor,
            "diff_preview": self._diff_preview,
            "mcp_call": self._mcp_call,
            "github_fetch": self._github_fetch,
            "clone_repo": self._clone_repo,
            "lsp_goto_def": self._lsp_goto_def,
            "lsp_find_refs": self._lsp_find_refs,
            "lsp_hover": self._lsp_hover,
            "lsp_diagnostics": self._lsp_diagnostics,
            "lsp_symbols": self._lsp_symbols,
            "weather": self._weather,
            "datetime": self._datetime,
            "register_tool": self._register_tool,
        }
        handler = handlers.get(self.action_type)
        if not handler:
            # 尝试从动态工具注册表中查找
            dynamic = _DYNAMIC_TOOLS.get(self.action_type)
            if dynamic:
                return self._exec_dynamic_tool(dynamic, context)
            raise ValueError(f"[{self.id}] 不支持的 action_type: {self.action_type}")
        return handler(context)

    # ── 命令执行 ──────────────────────────────────────────

    def _exec_command(self, context: AgentContext) -> dict[str, Any]:
        """执行终端命令。"""
        resolved_cmd = self._resolve_template(self.action, context)

        # 安全验证
        self._validate_command(resolved_cmd)

        if sys.platform == "win32":
            shell_exec = ["powershell", "-Command", resolved_cmd]
        else:
            shell_exec = ["/bin/bash", "-c", resolved_cmd]

        logger.info(f"[{self.id}] 执行命令: {resolved_cmd}")

        try:
            proc = subprocess.run(
                shell_exec,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                cwd=self.cwd,
            )
            result = {
                "action_type": "command",
                "command": resolved_cmd,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "success": proc.returncode == 0,
            }
            self._write_output(context, proc.stdout.strip())
            logger.info(f"[{self.id}] 命令完成，返回码: {proc.returncode}")
            return result

        except subprocess.TimeoutExpired:
            error_msg = f"命令执行超时 ({self.timeout}s): {resolved_cmd}"
            logger.error(f"[{self.id}] {error_msg}")
            return {
                "action_type": "command",
                "command": resolved_cmd,
                "returncode": -1,
                "stdout": "",
                "stderr": error_msg,
                "success": False,
                "error": error_msg,
            }

    # ── 文件写入 ──────────────────────────────────────────

    @staticmethod
    def _snapshot_path(path: Path) -> tuple[bytes, int] | None:
        """Capture exact file bytes and permissions for transactional rollback."""
        if not path.exists():
            return None
        if not path.is_file():
            raise IsADirectoryError(f"目标不是普通文件: {path}")
        return path.read_bytes(), path.stat().st_mode & 0o7777

    @staticmethod
    def _rollback_paths(
        paths: list[Path],
        snapshots: dict[Path, tuple[bytes, int] | None],
    ) -> list[str]:
        """Restore written paths in reverse order and report rollback failures."""
        errors: list[str] = []
        for path in reversed(paths):
            try:
                snapshot = snapshots[path]
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    content, mode = snapshot
                    atomic_write_bytes(path, content, mode=mode)
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        return errors

    def _write_file(self, context: AgentContext) -> dict[str, Any]:
        """将内容写入文件。"""
        file_path = self._resolve_template(self.file_path or "", context)
        content = self._resolve_template(self.content or "", context)

        if not file_path:
            raise ValueError(f"[{self.id}] write_file 需要 file_path")

        # 如果 content 为空，尝试从 context 中读取
        if not content and self.output_slot:
            content = context.get(self.output_slot, "")

        # 安全验证：路径 + 大小
        path = self._validate_path(file_path, for_write=True)
        content_bytes = len(content.encode(self.encoding))
        if content_bytes > MAX_WRITE_SIZE:
            return {
                "action_type": "write_file",
                "file_path": str(path),
                "bytes_written": 0,
                "success": False,
                "error": f"写入内容过大: {content_bytes} 字节，上限 {MAX_WRITE_SIZE} 字节",
            }

        # 创建父目录
        path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"[{self.id}] {'追加' if self.append else '写入'}文件: {path}")

        snapshot = self._snapshot_path(path)
        if self.append and snapshot is not None:
            previous = snapshot[0].decode(self.encoding)
            final_content = previous + content
        else:
            final_content = content

        try:
            atomic_write_text(
                path,
                final_content,
                backup=snapshot is not None,
                encoding=self.encoding,
            )
        except Exception as exc:
            return {
                "action_type": "write_file",
                "file_path": str(path),
                "bytes_written": 0,
                "success": False,
                "error": f"原子写入失败: {exc}",
            }

        # ── 写入后验证 ──
        verify_error = self._verify_write(path, content, self.append)
        if verify_error:
            logger.error(f"[{self.id}] 写入验证失败: {verify_error}")
            rollback_errors = self._rollback_paths([path], {path: snapshot})
            return {
                "action_type": "write_file",
                "file_path": str(path),
                "bytes_written": 0,
                "success": False,
                "error": verify_error,
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
            }

        result = {
            "action_type": "write_file",
            "file_path": str(path),
            "bytes_written": len(content.encode(self.encoding)),
            "append": self.append,
            "success": True,
        }
        self._write_output(context, str(path))
        return result

    def _verify_write(self, path: Path, expected_content: str, is_append: bool) -> str | None:
        """验证文件写入是否成功。返回错误信息，成功返回 None。"""
        if not path.exists():
            return f"文件写入后验证失败: {path} 不存在"

        if not path.is_file():
            return f"写入验证失败: {path} 不是文件"

        # 大文件只验证存在性，不回读内容
        try:
            file_size = path.stat().st_size
        except OSError:
            return "写入验证失败: 无法获取文件大小"

        if file_size > MAX_VERIFY_SIZE:
            logger.info(f"文件 {path} 大小 {file_size} 字节，跳过内容回读验证")
            return None

        try:
            actual = path.read_text(encoding=self.encoding)
        except UnicodeDecodeError:
            # 二进制文件无法以文本方式读取，只验证大小
            logger.info(f"文件 {path} 为二进制格式，跳过内容验证")
            return None
        except Exception as e:
            return f"写入后读取验证失败: {e}"

        if is_append:
            if not actual.endswith(expected_content) and expected_content not in actual:
                return "追加验证失败: 写入的内容未在文件中找到"
        else:
            if actual != expected_content:
                return (
                    f"内容验证失败: 期望 {len(expected_content)} 字符, "
                    f"实际 {len(actual)} 字符"
                )

        return None

    # ── 文件编辑（精确替换） ──────────────────────────────

    def _edit_file(self, context: AgentContext) -> dict[str, Any]:
        """精确文本替换编辑文件。"""
        file_path = self._resolve_template(self.file_path or "", context)
        old_text = self._resolve_template(self.old_text, context)
        new_text = self._resolve_template(self.new_text, context)

        if not file_path:
            raise ValueError(f"[{self.id}] edit_file 需要 file_path")
        if not old_text:
            raise ValueError(f"[{self.id}] edit_file 需要 old_text")

        # 安全验证
        path = self._validate_path(file_path, for_write=True)
        if not path.exists():
            return {"error": f"文件不存在: {path}", "success": False}

        content = path.read_text(encoding=self.encoding)
        count = content.count(old_text)

        if count == 0:
            return {"error": "未找到匹配文本", "success": False}
        if count > 1:
            return {"error": f"找到 {count} 处匹配，请提供更多上下文", "success": False}

        new_content = content.replace(old_text, new_text, 1)
        snapshot = self._snapshot_path(path)
        try:
            atomic_write_text(
                path,
                new_content,
                backup=True,
                encoding=self.encoding,
            )
        except Exception as exc:
            return {
                "file": str(path),
                "replacements": 0,
                "success": False,
                "error": f"原子编辑失败: {exc}",
            }

        # ── 编辑后验证 ──
        try:
            actual = path.read_text(encoding=self.encoding)
            if actual != new_content:
                rollback_errors = self._rollback_paths([path], {path: snapshot})
                return {
                    "file": str(path),
                    "replacements": 0,
                    "success": False,
                    "error": "编辑验证失败: 文件内容与预期不一致",
                    "rolled_back": not rollback_errors,
                    "rollback_errors": rollback_errors,
                }
        except Exception as e:
            rollback_errors = self._rollback_paths([path], {path: snapshot})
            return {
                "file": str(path),
                "replacements": 0,
                "success": False,
                "error": f"编辑后验证读取失败: {e}",
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
            }

        result = {
            "file": str(path),
            "replacements": 1,
            "success": True,
        }
        self._write_output(context, str(path))
        return result

    # ── 文件读取 ──────────────────────────────────────────

    def _create_directory(self, context: AgentContext) -> dict[str, Any]:
        """创建目录（含所有父目录）。"""
        dir_path = self._resolve_template(self.file_path or "", context)
        if not dir_path:
            # 兼容 action 参数
            dir_path = self._resolve_template(self.action, context)

        if not dir_path:
            raise ValueError(f"[{self.id}] create_directory 需要 file_path")

        # 安全验证
        path = self._validate_path(dir_path, for_write=True)

        logger.info(f"[{self.id}] 创建目录: {path}")

        try:
            path.mkdir(parents=True, exist_ok=True)

            # 验证目录确实存在
            if not path.exists() or not path.is_dir():
                return {
                    "action_type": "create_directory",
                    "path": str(path),
                    "success": False,
                    "error": f"目录创建后验证失败: {path} 不存在或不是目录",
                }

            result = {
                "action_type": "create_directory",
                "path": str(path),
                "success": True,
            }
            self._write_output(context, str(path))
            return result

        except Exception as e:
            return {
                "action_type": "create_directory",
                "path": str(path),
                "success": False,
                "error": f"目录创建失败: {e}",
            }

    # ── 批量操作 ──────────────────────────────────────────

    def _batch_write(self, context: AgentContext) -> dict[str, Any]:
        """Atomically write a group of files with all-or-nothing rollback."""
        if not self.files:
            return {
                "action_type": "batch_write",
                "success": False,
                "error": "batch_write 需要 files 参数，格式: [{\"path\": \"...\", \"content\": \"...\"}]",
            }

        prepared: list[tuple[int, Path, str, int]] = []
        results: list[dict[str, Any]] = []
        seen_paths: set[Path] = set()

        # Validate and snapshot the whole transaction before touching disk.
        for i, file_spec in enumerate(self.files):
            error = ""
            path: Path | None = None
            content = ""
            content_bytes = 0
            if not isinstance(file_spec, dict):
                error = "文件描述必须是对象"
            else:
                path_str = file_spec.get("path") or file_spec.get("file_path", "")
                content = file_spec.get("content", "")
                if not path_str:
                    error = "缺少 path"
                elif not isinstance(content, str):
                    error = "content 必须是字符串"
                else:
                    try:
                        path = self._validate_path(str(path_str), for_write=True)
                        content_bytes = len(content.encode(self.encoding))
                        if path in seen_paths:
                            error = f"同一事务中路径重复: {path}"
                        elif content_bytes > MAX_WRITE_SIZE:
                            error = f"内容过大: {content_bytes} 字节"
                        else:
                            seen_paths.add(path)
                    except Exception as exc:
                        error = str(exc)

            if error or path is None:
                results.append({
                    "index": i,
                    "path": str(path) if path else "",
                    "success": False,
                    "error": error or "无效路径",
                })
            else:
                prepared.append((i, path, content, content_bytes))
                results.append({
                    "index": i,
                    "path": str(path),
                    "success": False,
                    "error": "事务尚未提交",
                })

        if len(prepared) != len(self.files):
            for result in results:
                if result.get("error") == "事务尚未提交":
                    result["error"] = "事务包含无效操作，已整体取消"
            return {
                "action_type": "batch_write",
                "total": len(self.files),
                "success_count": 0,
                "success": False,
                "rolled_back": False,
                "error": "批量写入预检失败，未修改任何文件",
                "results": results,
            }

        snapshots = {path: self._snapshot_path(path) for _, path, _, _ in prepared}
        written_paths: list[Path] = []
        try:
            for i, path, content, content_bytes in prepared:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(
                    path,
                    content,
                    # Transaction snapshots provide rollback; avoiding .bak
                    # files here keeps a failed batch completely side-effect free.
                    backup=False,
                    encoding=self.encoding,
                )
                written_paths.append(path)
                verify_error = self._verify_write(path, content, False)
                if verify_error:
                    raise OSError(verify_error)
                results[i] = {
                    "index": i,
                    "path": str(path),
                    "success": True,
                    "bytes": content_bytes,
                }
        except Exception as exc:
            rollback_errors = self._rollback_paths(written_paths, snapshots)
            for result in results:
                result["success"] = False
                result["error"] = "事务执行失败，已回滚"
            return {
                "action_type": "batch_write",
                "total": len(self.files),
                "success_count": 0,
                "success": False,
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
                "error": f"批量写入失败: {exc}",
                "results": results,
            }

        return {
            "action_type": "batch_write",
            "total": len(self.files),
            "success_count": len(prepared),
            "success": True,
            "results": results,
        }

    def _batch_edit(self, context: AgentContext) -> dict[str, Any]:
        """Apply a group of exact edits as one transactional operation."""
        if not self.edits:
            return {
                "action_type": "batch_edit",
                "success": False,
                "error": "batch_edit 需要 edits 参数，格式: [{\"file_path\": \"...\", \"old_text\": \"...\", \"new_text\": \"...\"}]",
            }

        results: list[dict[str, Any]] = []
        staged_content: dict[Path, str] = {}
        path_order: list[Path] = []

        # Stage edits in memory. Repeated edits to one file are applied in the
        # declared order and the file is written only once at commit time.
        for i, edit_spec in enumerate(self.edits):
            error = ""
            path: Path | None = None
            if not isinstance(edit_spec, dict):
                error = "编辑描述必须是对象"
                old_text = ""
                new_text = ""
                file_path = ""
            else:
                file_path = edit_spec.get("file_path", "")
                old_text = edit_spec.get("old_text", "")
                new_text = edit_spec.get("new_text", "")
                if not file_path or not old_text:
                    error = "缺少 file_path 或 old_text"
                elif not isinstance(old_text, str) or not isinstance(new_text, str):
                    error = "old_text 和 new_text 必须是字符串"

            if not error:
                try:
                    path = self._validate_path(str(file_path), for_write=True)
                    if not path.exists():
                        error = f"文件不存在: {path}"
                    else:
                        if path not in staged_content:
                            staged_content[path] = path.read_text(encoding=self.encoding)
                            path_order.append(path)
                        count = staged_content[path].count(old_text)
                        if count == 0:
                            error = "未找到匹配文本"
                        elif count > 1:
                            error = f"找到 {count} 处匹配，请提供更多上下文"
                        else:
                            staged_content[path] = staged_content[path].replace(
                                old_text, new_text, 1
                            )
                except Exception as exc:
                    error = f"编辑预检异常: {exc}"

            results.append({
                "index": i,
                "file": str(path) if path else str(file_path),
                "success": False,
                "error": error or "事务尚未提交",
            })

        if any(result["error"] != "事务尚未提交" for result in results):
            for result in results:
                if result["error"] == "事务尚未提交":
                    result["error"] = "事务包含无效操作，已整体取消"
            return {
                "action_type": "batch_edit",
                "total": len(self.edits),
                "success_count": 0,
                "success": False,
                "rolled_back": False,
                "error": "批量编辑预检失败，未修改任何文件",
                "results": results,
            }

        snapshots = {path: self._snapshot_path(path) for path in path_order}
        written_paths: list[Path] = []
        try:
            for path in path_order:
                atomic_write_text(
                    path,
                    staged_content[path],
                    backup=False,
                    encoding=self.encoding,
                )
                written_paths.append(path)
                verify_error = self._verify_write(path, staged_content[path], False)
                if verify_error:
                    raise OSError(verify_error)
        except Exception as exc:
            rollback_errors = self._rollback_paths(written_paths, snapshots)
            for result in results:
                result["error"] = "事务执行失败，已回滚"
            return {
                "action_type": "batch_edit",
                "total": len(self.edits),
                "success_count": 0,
                "success": False,
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
                "error": f"批量编辑失败: {exc}",
                "results": results,
            }

        for result in results:
            result["success"] = True
            result.pop("error", None)
            result["replacements"] = 1
        return {
            "action_type": "batch_edit",
            "total": len(self.edits),
            "success_count": len(self.edits),
            "success": True,
            "results": results,
        }

    # ── 代码索引 / AST / 重构 ──────────────────────────────

    def _code_index(self, context: AgentContext) -> dict[str, Any]:
        """代码索引搜索。"""
        from xenon.utils.code_index import CodeIndex

        query = self._resolve_template(self.search_pattern or self.symbol or "", context)
        file_path = self._resolve_template(self.file_path or "", context)

        if not query:
            return {
                "action_type": "code_index",
                "success": False,
                "error": "需要 search_pattern 或 symbol 参数",
            }

        # 确定索引根目录
        root = file_path if file_path and Path(file_path).is_dir() else "."
        try:
            root = str(self._validate_path(root, for_write=False))
        except Exception:
            root = "."

        index = CodeIndex(root)
        index.build(max_files=200)
        results = index.search(query, limit=30)
        stats = index.stats()

        matches = []
        for sym in results:
            matches.append({
                "name": sym.name,
                "kind": sym.kind,
                "file": sym.file_path,
                "line": sym.line,
                "parent": sym.parent or "",
                "signature": sym.signature,
            })

        display = f"索引 {stats['files']} 个文件, {stats['symbols']} 个符号\n"
        display += f"搜索 '{query}': 找到 {len(matches)} 个匹配\n"
        for m in matches[:20]:
            parent = f"{m['parent']}." if m['parent'] else ""
            sig = f"({m['signature']})" if m['signature'] else ""
            display += f"  {m['kind']} {parent}{m['name']}{sig} @ {m['file']}:{m['line']}\n"

        result = {
            "action_type": "code_index",
            "query": query,
            "total_files": stats["files"],
            "total_symbols": stats["symbols"],
            "matches": matches,
            "success": True,
        }
        self._write_output(context, display)
        return result

    def _ast_analyze(self, context: AgentContext) -> dict[str, Any]:
        """AST 代码分析。"""
        from xenon.utils.ast_analyzer import ASTAnalyzer

        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": "需要 file_path 参数",
            }

        path = self._validate_path(file_path, for_write=False)
        if not path.exists():
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": f"文件不存在: {path}",
            }

        analyzer = ASTAnalyzer()
        try:
            result = analyzer.analyze_file(path)
        except Exception as e:
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": f"分析失败: {e}",
            }

        display = result.summary()

        # 函数签名
        if result.functions:
            display += "\n\n函数:\n"
            for f in result.functions[:20]:
                async_kw = "async " if f.is_async else ""
                display += f"  {async_kw}def {f.name}({', '.join(f.args)}) -> {f.return_annotation or 'None'} @ 行{f.line} [复杂度:{f.complexity}]\n"

        # 类
        if result.classes:
            display += "\n\n类:\n"
            for c in result.classes[:10]:
                bases = f"({', '.join(c.bases)})" if c.bases else ""
                display += f"  class {c.name}{bases} @ 行{c.line}, {len(c.methods)} 个方法\n"

        ret = {
            "action_type": "ast_analyze",
            "file": str(path),
            "syntax_valid": result.syntax_valid,
            "functions": len(result.functions),
            "classes": len(result.classes),
            "complexity": result.complexity,
            "unused_imports": result.unused_imports,
            "success": True,
        }
        self._write_output(context, display)
        return ret

    def _refactor(self, context: AgentContext) -> dict[str, Any]:
        """代码重构操作。"""
        from xenon.utils.refactor import RefactorEngine

        action = self._resolve_template(self.refactor_action, context)
        file_path = self._resolve_template(self.file_path or "", context)

        if not action:
            return {
                "action_type": "refactor",
                "success": False,
                "error": "需要 refactor_action 参数: rename | clean_imports | analyze",
            }

        # 确定项目根目录
        root = "."
        if file_path and Path(file_path).is_dir():
            root = str(file_path)
        elif file_path:
            root = str(Path(file_path).parent)

        try:
            root = str(self._validate_path(root, for_write=False))
        except Exception:
            root = "."

        engine = RefactorEngine(root)
        engine.build_index(max_files=200)

        if action == "rename":
            old_name = self._resolve_template(self.old_name, context)
            new_name = self._resolve_template(self.new_name, context)
            if not old_name or not new_name:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "rename 需要 old_name 和 new_name 参数",
                }
            # A8: rename 默认单文件作用域，防 LLM 跨全部文件盲目文本重命名误改同名符号
            if not file_path or not Path(file_path).is_file():
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "rename 需指定 file_path（单文件作用域重命名）；跨文件批量重命名易误改其他模块同名符号，已禁用",
                }
            result = engine.rename_symbol(old_name, new_name, definition_file=file_path)
            display = f"重命名 '{old_name}' → '{new_name}'\n"
            display += f"修改 {len(result['changes'])} 处\n"
            if result["errors"]:
                display += f"错误: {'; '.join(result['errors'])}\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "rename", **result}

        elif action == "clean_imports":
            if not file_path:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "clean_imports 需要 file_path 参数",
                }
            result = engine.clean_unused_imports(file_path)
            display = f"清理导入: {file_path}\n"
            if result.get("removed"):
                display += f"移除 {len(result['removed'])} 个未使用导入\n"
            else:
                display += "没有未使用的导入\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "clean_imports", **result}

        elif action == "analyze":
            if not file_path:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "analyze 需要 file_path 参数",
                }
            result = engine.analyze_for_refactor(file_path)
            display = result["summary"]
            if result["suggestions"]:
                display += "\n\n重构建议:\n"
                for s in result["suggestions"]:
                    display += f"  [{s['type']}] {s['message']}\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "analyze", **result}

        else:
            return {
                "action_type": "refactor",
                "success": False,
                "error": f"未知 refactor_action: {action}。支持: rename | clean_imports | analyze",
            }

    def _diff_preview(self, context: AgentContext) -> dict[str, Any]:
        """生成 diff 预览（不实际修改文件）。"""
        import difflib

        file_path = self._resolve_template(self.file_path or "", context)
        old_text = self._resolve_template(self.old_text, context)
        new_text = self._resolve_template(self.new_text, context)

        if not file_path:
            return {
                "action_type": "diff_preview",
                "success": False,
                "error": "需要 file_path 参数",
            }

        path = self._validate_path(file_path, for_write=False)

        if old_text and new_text:
            # edit 模式：展示替换 diff
            if not path.exists():
                return {
                    "action_type": "diff_preview",
                    "success": False,
                    "error": f"文件不存在: {path}",
                }
            content = path.read_text(encoding=self.encoding)
            if old_text not in content:
                return {
                    "action_type": "diff_preview",
                    "success": False,
                    "error": "未找到匹配文本",
                }
            new_content = content.replace(old_text, new_text, 1)
        elif new_text or self.content:
            # write 模式：展示新文件 diff
            target_content = new_text or self.content or ""
            content = path.read_text(encoding=self.encoding) if path.exists() else ""
            new_content = target_content
        else:
            return {
                "action_type": "diff_preview",
                "success": False,
                "error": "需要 old_text/new_text 或 content 参数",
            }

        # 生成 diff
        old_lines = content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{Path(file_path).name}",
            tofile=f"b/{Path(file_path).name}",
            lineterm="",
        ))

        diff_text = "\n".join(diff) if diff else "(无变化)"

        result = {
            "action_type": "diff_preview",
            "file": str(path),
            "diff": diff_text,
            "has_changes": len(diff) > 0,
            "success": True,
        }
        self._write_output(context, diff_text)
        return result

    def _prefilter_result_text(
        self,
        text: str,
        context: AgentContext,
    ) -> tuple[str, dict[str, Any]]:
        """Apply user list constraints before any prefix truncation."""
        constraint_source = str(
            context.get("_query_constraint_source")
            or context.get("_current_user_request")
            or ""
        )
        inferred_start, inferred_end = _infer_time_window(
            f"{constraint_source}\n{self.url}"
        )

        def valid_clock(value: Any) -> str | None:
            match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", str(value or ""))
            if not match:
                return None
            return f"{int(match.group(1)):02d}:{match.group(2)}"

        start_time = valid_clock(self.start_time) or inferred_start
        end_time = valid_clock(self.end_time) or inferred_end
        try:
            max_chars = max(1000, min(int(self.max_chars), 30000))
        except (TypeError, ValueError):
            max_chars = 12000
        if start_time or end_time:
            return _prefilter_time_records(
                text,
                start_time=start_time,
                end_time=end_time,
                max_chars=max_chars,
            )
        query = self._resolve_template(self.query, context).strip()
        if query:
            return _prefilter_keyword_context(
                text,
                query=query,
                max_chars=max_chars,
            )
        return text, {}

    def _mcp_call(self, context: AgentContext) -> dict[str, Any]:
        """调用 MCP 服务器工具。"""

        tool_name = self._resolve_template(self.tool_name, context)
        if not tool_name:
            return {
                "action_type": "mcp_call",
                "success": False,
                "error": "需要 tool_name 参数",
            }

        # 获取注册表（从 context 或创建新的）
        registry = context.get("_mcp_registry")
        if not registry:
            return {
                "action_type": "mcp_call",
                "success": False,
                "error": "MCP 未初始化。请先使用 /mcp add 命令添加 MCP 服务器",
            }

        try:
            # 解析参数中的模板
            args = {}
            for k, v in self.tool_args.items():
                if isinstance(v, str):
                    args[k] = self._resolve_template(v, context)
                else:
                    args[k] = v

            result = registry.call_tool(tool_name, args)

            # 提取结果内容
            content_parts = []
            for item in result.get("content", []):
                if item.get("type") == "text":
                    content_parts.append(item.get("text", ""))
                else:
                    content_parts.append(str(item))

            display = "\n".join(content_parts) if content_parts else str(result)
            display, filter_meta = self._prefilter_result_text(display, context)
            display_cap = 12000 if filter_meta else 5000
            display = display[:display_cap]
            self._write_output(context, display)

            return {
                "action_type": "mcp_call",
                "tool": tool_name,
                "result": result,
                "content": display,  # v0.5.3: LLM 可读的文本输出
                "success": True,
                **filter_meta,
            }

        except Exception as e:
            return {
                "action_type": "mcp_call",
                "tool": tool_name,
                "success": False,
                "error": str(e),
            }

    def _read_file(self, context: AgentContext) -> dict[str, Any]:
        """读取文件内容。支持通过 start_line/max_lines 分段读取。"""
        file_path = self._resolve_template(self.file_path or "", context)

        if not file_path:
            raise ValueError(f"[{self.id}] read_file 需要 file_path")

        # 安全验证
        path = self._validate_path(file_path, for_write=False)

        if not path.exists():
            result = {
                "action_type": "read_file",
                "file_path": str(path),
                "content": "",
                "exists": False,
                "success": False,
                "error": f"文件不存在: {path}",
            }
            self._write_output(context, "")
            logger.warning(f"[{self.id}] 文件不存在: {path}")
            return result

        # 文件大小检查
        try:
            file_size = path.stat().st_size
            if file_size > MAX_READ_SIZE:
                return {
                    "action_type": "read_file",
                    "file_path": str(path),
                    "content": "",
                    "exists": True,
                    "success": False,
                    "error": f"文件过大: {file_size} 字节，读取上限 {MAX_READ_SIZE} 字节。请使用 command + head/tail 查看部分内容。",
                }
        except OSError:
            pass

        logger.info(f"[{self.id}] 读取文件: {path}")

        # 分段读取：start_line（从 1 开始）和 max_lines
        start_line = getattr(self, '_extra_start_line', None)
        max_lines = getattr(self, '_extra_max_lines', None)

        if start_line is not None or max_lines is not None:
            # 按行分段读取
            all_lines = path.read_text(encoding=self.encoding).splitlines(keepends=True)
            total_lines = len(all_lines)
            s = max(1, int(start_line)) - 1 if start_line else 0  # 转为 0-based
            e = s + int(max_lines) if max_lines else total_lines
            e = min(e, total_lines)
            content = "".join(all_lines[s:e])
            result = {
                "action_type": "read_file",
                "file_path": str(path),
                "content": content,
                "total_lines": total_lines,
                "from_line": s + 1,
                "to_line": e,
                "size": len(content),
                "exists": True,
                "success": True,
            }
        else:
            content = path.read_text(encoding=self.encoding)
            result = {
                "action_type": "read_file",
                "file_path": str(path),
                "content": content,
                "size": len(content),
                "exists": True,
                "success": True,
            }

        self._write_output(context, content)
        return result

    # ── 目录遍历 ──────────────────────────────────────────

    def _list_files(self, context: AgentContext) -> dict[str, Any]:
        """遍历目录，支持 glob 模式和递归深度限制。"""
        base_path = self._resolve_template(self.file_path or ".", context)
        pattern = self._resolve_template(self.pattern, context)

        # 安全验证
        path = self._validate_path(base_path, for_write=False)

        if not path.exists():
            result = {
                "action_type": "list_files", "path": str(path),
                "files": [], "count": 0, "success": False,
                "error": f"路径不存在: {path}",
            }
            self._write_output(context, f"路径不存在: {path}")
            return result

        files = []
        if path.is_file():
            files.append(str(path))
        else:
            for item in self._walk_with_depth(path, pattern, self.max_depth):
                files.append(str(item))

        display = "\n".join(files) if files else "(空目录)"
        result = {
            "action_type": "list_files", "path": str(path),
            "pattern": pattern, "files": files, "count": len(files), "success": True,
        }
        self._write_output(context, display)
        logger.info(f"[{self.id}] 列出 {len(files)} 个文件: {path}")
        return result

    def _walk_with_depth(self, base: Path, pattern: str, max_depth: int):
        """递归遍历，受深度限制。支持 **/*.ext 递归 glob 模式。"""
        import os

        # 处理 **/*.ext 模式：拆分为前缀目录模式和文件名模式
        recursive_mode = "**" in pattern
        if recursive_mode:
            # "**/*.py" → file_pattern = "*.py"
            # "**/test_*.py" → file_pattern = "test_*.py"
            file_pattern = pattern.split("**/")[-1] if "**/" in pattern else pattern.replace("**", "*")
        else:
            file_pattern = pattern

        base_depth = len(base.parts)
        for root, dirs, files in os.walk(base):
            current_depth = len(Path(root).parts) - base_depth
            if not recursive_mode and current_depth > max_depth:
                dirs.clear()
                continue
            if current_depth > max_depth * 2:  # 递归模式给更多深度
                dirs.clear()
                continue
            for f in files:
                if fnmatch.fnmatch(f, file_pattern):
                    yield Path(root) / f

    # ── 文件内容搜索 ──────────────────────────────────────

    def _search_files(self, context: AgentContext) -> dict[str, Any]:
        """在文件中搜索内容（类似 grep）。"""
        search_dir = self._resolve_template(self.file_path or ".", context)
        search_pattern = self._resolve_template(self.search_pattern, context)
        file_filter = self._resolve_template(self.file_filter, context)

        if not search_pattern:
            raise ValueError(f"[{self.id}] search_files 需要 search_pattern")

        # 安全验证
        path = self._validate_path(search_dir, for_write=False)

        if not path.exists():
            result = {
                "action_type": "search_files", "path": str(path),
                "matches": [], "match_count": 0, "success": False,
                "error": f"路径不存在: {path}",
            }
            self._write_output(context, f"路径不存在: {path}")
            return result

        matches = []
        files_scanned = 0
        try:
            regex = re.compile(search_pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(search_pattern), re.IGNORECASE)

        search_files = [path] if path.is_file() else self._walk_with_depth(path, file_filter or "*", self.max_depth)

        for file_path in search_files:
            try:
                text = Path(file_path).read_text(encoding=self.encoding, errors="ignore")
                files_scanned += 1
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append({
                            "file": str(file_path), "line": i,
                            "content": line.strip()[:200],
                        })
                        if len(matches) >= 200:
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if len(matches) >= 200:
                break

        lines = [f"{m['file']}:{m['line']}: {m['content']}" for m in matches[:50]]
        display = "\n".join(lines) if lines else "(无匹配结果)"

        result = {
            "action_type": "search_files", "path": str(path), "pattern": search_pattern,
            "matches": matches, "match_count": len(matches),
            "files_scanned": files_scanned,
            "stdout": display,  # v0.5.3: 文本表示，LLM 可直接读取
            "success": True,
        }
        self._write_output(context, display)
        logger.info(f"[{self.id}] 搜索到 {len(matches)} 处匹配: {search_pattern}")
        return result

    # ── Git 操作 ──────────────────────────────────────────

    def _git(self, context: AgentContext) -> dict[str, Any]:
        """执行 Git 操作。支持: status, diff, log, add, commit, branch。"""
        git_cmd = self._resolve_template(self.git_command, context).strip()
        extra_args = self._resolve_template(self.action, context).strip()

        # 安全验证
        self._validate_git_command(git_cmd)
        if extra_args:
            self._validate_git_command(extra_args)

        git_commands = {
            "status": ["git", "status", "--short"],
            "diff": ["git", "diff", "--stat"],
            "diff_full": ["git", "diff"],
            "log": ["git", "log", "--oneline", "-10"],
            "branch": ["git", "branch", "-a"],
            "add": ["git", "add", "."],
            "stash": ["git", "stash"],
        }

        if git_cmd in git_commands:
            cmd = git_commands[git_cmd]
        elif git_cmd.startswith("commit"):
            msg = git_cmd.replace("commit", "").strip() or extra_args or "auto commit"
            cmd = ["git", "commit", "-m", msg]
        elif git_cmd.startswith("add"):
            target = git_cmd.replace("add", "").strip() or extra_args or "."
            cmd = ["git", "add", target]
        else:
            cmd = ["git"] + git_cmd.split() + (extra_args.split() if extra_args else [])

        logger.info(f"[{self.id}] git {' '.join(cmd[1:])}")

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.timeout, cwd=self.cwd or ".",
            )
            output = proc.stdout.strip() or proc.stderr.strip()
            result = {
                "action_type": "git", "command": " ".join(cmd),
                "returncode": proc.returncode,
                "stdout": output,    # v0.5.3: 统一字段名，与 command 工具一致
                "output": output,    # 保留兼容
                "success": proc.returncode == 0,
            }
            self._write_output(context, output)
            return result
        except subprocess.TimeoutExpired:
            return {
                "action_type": "git", "command": " ".join(cmd),
                "returncode": -1, "stdout": "", "output": "",
                "success": False,
                "error": f"Git 命令超时 ({self.timeout}s): {' '.join(cmd)}",
            }
        except FileNotFoundError:
            return {
                "action_type": "git", "command": " ".join(cmd),
                "returncode": -1, "stdout": "", "output": "",
                "success": False,
                "error": "Git 未安装或不在 PATH 中。请先安装 git。",
            }

    # ── 网页抓取 ──────────────────────────────────────────

    def _web_fetch(self, context: AgentContext) -> dict[str, Any]:
        """抓取网页内容，返回纯文本。"""
        url = self._resolve_template(self.url, context)
        if not url:
            url = self._resolve_template(self.action, context)
        if not url:
            raise ValueError(f"[{self.id}] web_fetch 需要 url")

        # A GitHub HTML/raw URL is repository data, not a generic webpage.
        # Route it through the typed GitHub client so pasted links, private
        # repositories and blob/issue/pull semantics work even if the model
        # selected web_fetch.
        host = (urlparse(url).hostname or "").lower()
        if host in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
            try:
                parse_github_reference(url)
            except ValueError:
                # Organization/user/search pages are ordinary public HTML, not
                # repository references.  Keep them on the generic fetch path
                # instead of returning the misleading "owner/repo" error.
                if host == "raw.githubusercontent.com":
                    return {
                        "action_type": "web_fetch", "url": url,
                        "content": "", "success": False,
                        "retryable": False,
                        "error": "无效的 GitHub raw 文件 URL",
                    }
            else:
                github_node = ToolNode(
                    f"{self.id}:github",
                    action_type="github_fetch",
                    repo=url,
                    branch="",
                    timeout=self.timeout,
                    output_slot=self.output_slot,
                    security_enabled=self.security_enabled,
                )
                return github_node._github_fetch(context)

        # A5: SSRF 防护 — 校验起始 URL 的目标 IP（覆盖 IPv6/编码 IP/元数据地址/file://）
        ok, reason = _ssrf_check_url(url)
        if not ok:
            return {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False,
                "error": (
                    f"SSRF 拦截: {reason}"
                    f"。可尝试用 command 工具执行 curl 获取数据作为降级方案"
                ),
            }

        logger.info(f"[{self.id}] 抓取网页: {url}")

        try:
            # A5: 禁用自动重定向，逐跳校验 Location 防"重定向到内网"
            with _create_http_client(timeout=self.timeout, follow_redirects=False) as client:
                resp = _fetch_with_redirect_check(client, url)
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                if "text/html" in content_type:
                    text = self._html_to_text(resp.text)
                else:
                    text = resp.text

                # User constraints are applied to the complete response before
                # prefix truncation.  This keeps tail records (for example
                # evening trains in a chronologically sorted timetable) visible.
                text, filter_meta = self._prefilter_result_text(text, context)
                if not filter_meta and len(text) > 50000:
                    text = text[:50000] + "\n\n... (内容已截断，超过 50000 字符)"

                result = {
                    "action_type": "web_fetch", "url": str(resp.url),
                    "status_code": resp.status_code, "content": text,
                    "content_length": len(text), "success": True,
                    **filter_meta,
                }
                self._write_output(context, text[:12000 if filter_meta else 5000])
                return result

        except ImportError:
            return {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False,
                "error": "web_fetch 需要 httpx 库。请 pip install httpx",
            }
        except _SSRFRedirectError as e:
            return {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False,
                "error": f"SSRF 拦截(重定向): {e}",
            }
        except Exception as e:
            result = {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False, "error": str(e),
            }
            self._write_output(context, f"抓取失败: {e}")
            return result

    def _docs_fetch(self, context: AgentContext) -> dict[str, Any]:
        """Discover llms.txt and retrieve a bounded, query-relevant doc bundle."""
        from xenon.utils.llms_txt import (
            llms_candidate_urls,
            parse_llms_txt,
            select_llms_links,
        )

        url = self._resolve_template(self.url, context)
        if not url:
            url = self._resolve_template(self.action, context)
        if not url:
            raise ValueError(f"[{self.id}] docs_fetch 需要 url")
        # ToolExecutor historically normalizes "query" to search_pattern.
        query = self._resolve_template(
            self.query or self.search_pattern, context
        )
        max_pages = max(0, min(int(self.max_pages), 8))
        max_chars = max(1000, min(int(self.max_chars), 30000))
        discovery_urls = llms_candidate_urls(url)
        attempts: list[dict[str, Any]] = []

        def fetch_text(client, target: str) -> tuple[str, str, int]:
            ok, reason = _ssrf_check_url(target)
            if not ok:
                raise SecurityError(f"SSRF 拦截: {reason}")
            response = _fetch_with_redirect_check(client, target)
            if response.status_code == 404:
                return "", str(response.url), 404
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            text = (
                self._html_to_text(response.text)
                if "text/html" in content_type
                else response.text
            )
            return text, str(response.url), response.status_code

        try:
            with _create_http_client(timeout=self.timeout, follow_redirects=False) as client:
                index_text = ""
                index_url = ""
                index_kind = ""
                for candidate in discovery_urls:
                    try:
                        text, final_url, status = fetch_text(client, candidate)
                    except (httpx.HTTPError, _SSRFRedirectError, SecurityError) as exc:
                        attempts.append({"url": candidate, "error": str(exc)[:160]})
                        continue
                    attempts.append({"url": candidate, "status_code": status})
                    if status == 404 or not text.strip():
                        continue
                    index_text = text
                    index_url = final_url
                    index_kind = final_url.rstrip("/").rsplit("/", 1)[-1].casefold()
                    break

                if index_text and index_kind in {
                    "llms-full.txt", "llms-ctx.txt", "llms-ctx-full.txt",
                }:
                    truncated = len(index_text) > max_chars
                    if truncated:
                        suffix = "\n\n... (文档已按上下文预算截断)"
                        content = index_text[:max(0, max_chars - len(suffix))] + suffix
                    else:
                        content = index_text
                    result = {
                        "action_type": "docs_fetch",
                        "url": url,
                        "strategy": "llms-full",
                        "discovery_url": index_url,
                        "discovery_attempts": attempts,
                        "selected_sources": [index_url],
                        "discovered_links": 0,
                        "content": content,
                        "content_length": len(content),
                        "truncated": truncated,
                        "success": True,
                    }
                    self._write_output(context, content[:5000])
                    return result

                if index_text:
                    try:
                        document = parse_llms_txt(index_text, index_url)
                    except ValueError as exc:
                        attempts.append({"url": index_url, "error": str(exc)})
                    else:
                        selected = select_llms_links(
                            document, query, max_pages=max_pages
                        )
                        parts = [f"# {document.title}"]
                        if document.summary:
                            parts.append(f"> {document.summary}")
                        if document.details:
                            parts.append(document.details)
                        selected_sources: list[str] = []
                        source_errors: list[dict[str, str]] = []
                        for link in selected:
                            try:
                                page, final_url, status = fetch_text(client, link.url)
                                if status == 404 or not page.strip():
                                    raise ValueError(f"HTTP {status}")
                            except Exception as exc:  # isolated linked-page failure
                                source_errors.append({
                                    "url": link.url, "error": str(exc)[:160],
                                })
                                continue
                            selected_sources.append(final_url)
                            parts.extend([
                                f"## {link.title}",
                                f"Source: {final_url}",
                                page,
                            ])
                            if sum(len(part) for part in parts) >= max_chars:
                                break

                        combined = "\n\n".join(parts)
                        truncated = len(combined) > max_chars
                        if truncated:
                            suffix = "\n\n... (文档包已按上下文预算截断)"
                            content = combined[:max(0, max_chars - len(suffix))] + suffix
                        else:
                            content = combined
                        result = {
                            "action_type": "docs_fetch",
                            "url": url,
                            "query": query,
                            "strategy": "llms-index",
                            "discovery_url": index_url,
                            "discovery_attempts": attempts,
                            "selected_sources": selected_sources,
                            "source_errors": source_errors,
                            "discovered_links": len(document.links),
                            "optional_links": sum(
                                1 for link in document.links if link.optional
                            ),
                            "content": content,
                            "content_length": len(content),
                            "truncated": truncated,
                            "success": True,
                        }
                        self._write_output(context, content[:5000])
                        return result

            # No valid index: preserve usefulness by reusing the hardened web
            # fetch path for the exact user URL.
            fallback = ToolNode(
                f"{self.id}:fallback",
                action_type="web_fetch",
                url=url,
                timeout=self.timeout,
                output_slot=self.output_slot,
                security_enabled=self.security_enabled,
            )._web_fetch(context)
            fallback["action_type"] = "docs_fetch"
            fallback["strategy"] = "html-fallback"
            fallback["discovery_attempts"] = attempts
            fallback["degraded"] = True
            return fallback
        except Exception as exc:
            result = {
                "action_type": "docs_fetch",
                "url": url,
                "strategy": "failed",
                "discovery_attempts": attempts,
                "content": "",
                "success": False,
                "error": str(exc),
            }
            self._write_output(context, f"文档抓取失败: {exc}")
            return result

    def _weather(self, context: AgentContext) -> dict[str, Any]:
        """查询指定城市的天气信息。"""
        city = self._resolve_template(getattr(self, "city", ""), context) or "Beijing"
        lang = self._resolve_template(getattr(self, "lang", ""), context) or "zh"

        logger.info(f"[{self.id}] 查询天气: {city}")

        try:
            from xenon.utils.weather import get_weather, format_weather_report
            info = get_weather(city, lang)
            report = format_weather_report(info)

            result = {
                "action_type": "weather",
                "city": city,
                "success": "error" not in info,
                "weather_info": info,
                "content": report,
            }
            self._write_output(context, report[:5000])
            return result

        except ImportError:
            return {
                "action_type": "weather", "city": city,
                "success": False, "content": "",
                "error": "weather 工具需要 httpx 库。请 pip install httpx",
            }
        except Exception as e:
            logger.error(f"[{self.id}] 天气查询失败: {e}")
            result = {
                "action_type": "weather",
                "city": city,
                "success": False,
                "content": "",
                "error": str(e),
            }
            self._write_output(context, f"天气查询失败: {e}")
            return result

    def _datetime(self, context: AgentContext) -> dict[str, Any]:
        """获取当前日期和时间信息。"""
        from datetime import datetime
        now = datetime.now()
        weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

        date_str = f"{now.year}年{now.month}月{now.day}日"
        time_str = now.strftime("%H:%M:%S")
        weekday = weekdays_cn[now.weekday()]

        content = (
            f"📅 当前日期: {date_str} {weekday}\n"
            f"🕐 当前时间: {time_str}\n"
            f"📊 详细信息:\n"
            f"  - 年: {now.year}\n"
            f"  - 月: {now.month}\n"
            f"  - 日: {now.day}\n"
            f"  - 星期: {weekday}\n"
            f"  - 时: {now.hour}\n"
            f"  - 分: {now.minute}\n"
            f"  - 秒: {now.second}"
        )

        result = {
            "action_type": "datetime",
            "success": True,
            "content": content,
            "date": date_str,
            "time": time_str,
            "weekday": weekday,
            "year": now.year,
            "month": now.month,
            "day": now.day,
        }
        self._write_output(context, content)
        return result

    def _github_fetch(self, context: AgentContext) -> dict[str, Any]:
        """Fetch repository files, README, issues and pull requests via GitHub API."""
        repo_input = self._resolve_template(self.repo, context)
        if not repo_input:
            raise ValueError(f"[{self.id}] github_fetch 需要 repo 参数（格式: owner/repo）")

        try:
            reference = parse_github_reference(repo_input)
        except ValueError as exc:
            return {
                "action_type": "github_fetch", "repo": repo_input,
                "content": "", "success": False,
                "error": str(exc),
            }
        repo = reference.slug

        action = self._resolve_template(self.github_action, context) or "list_files"
        branch_value = (self._resolve_template(self.branch, context) or "").strip()
        path_value = (self._resolve_template(self.github_path, context) or "").strip("/")

        # A pasted resource URL carries stronger semantics than the default
        # list_files action, while explicit branch/path parameters still win.
        if reference.kind == "blob":
            action = "fetch_file"
            branch_value = branch_value or reference.ref
            path_value = path_value or reference.path
        elif reference.kind == "tree":
            action = "list_files"
            branch_value = branch_value or reference.ref
            path_value = path_value or reference.path
        elif reference.kind == "issue":
            action = "fetch_issue"
        elif reference.kind == "pull":
            action = "fetch_pull"

        try:
            import httpx
        except ImportError:
            return {
                "action_type": "github_fetch", "repo": repo,
                "action": action, "content": "", "success": False,
                "error": "github_fetch 需要 httpx 库。请 pip install httpx",
            }

        headers = self._github_headers()

        try:
            with _create_http_client(timeout=self.timeout, follow_redirects=True) as client:
                if action in {"list_files", "fetch_file", "fetch_readme"}:
                    branch_value = branch_value or self._github_default_branch(
                        client, repo, headers,
                    )
                branch = quote(branch_value, safe="")
                github_path = quote(path_value, safe="/")
                logger.info(
                    "[%s] GitHub %s: %s (branch=%s, path=%s)",
                    self.id, action, repo, branch_value or "-", path_value or "-",
                )

                if action == "list_files":
                    api_url = (
                        f"https://api.github.com/repos/{repo}/git/trees/"
                        f"{branch}?recursive=1"
                    )
                    resp = client.get(api_url, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    prefix = path_value.rstrip("/") + "/" if path_value else ""
                    files = [
                        item["path"] for item in data.get("tree", [])
                        if item.get("type") == "blob"
                        and not item.get("path", "").startswith(".git/")
                        and (not prefix or item.get("path", "").startswith(prefix))
                    ]
                    result_text = (
                        f"仓库 {repo}@{branch_value} 共 {len(files)} 个文件:\n"
                        + "\n".join(files)
                    )
                    if len(result_text) > 10000:
                        result_text = (
                            result_text[:10000]
                            + f"\n\n... (共 {len(files)} 个文件，已截断)"
                        )
                    self._write_output(context, result_text[:5000])
                    return {
                        "action_type": "github_fetch", "repo": repo,
                        "action": action, "branch": branch_value,
                        "path": path_value, "files": files,
                        "file_count": len(files), "content": result_text,
                        "success": True,
                    }

                if action == "fetch_file":
                    if not github_path:
                        return {
                            "action_type": "github_fetch", "repo": repo,
                            "action": action, "content": "", "success": False,
                            "error": "fetch_file 需要 github_path 参数",
                        }
                    api_url = (
                        f"https://api.github.com/repos/{repo}/contents/"
                        f"{github_path}?ref={branch}"
                    )
                    resp = client.get(api_url, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    text = self._decode_github_content(data)
                    if len(text) > 50000:
                        text = text[:50000] + "\n\n... (内容已截断，超过 50000 字符)"
                    self._write_output(context, text[:5000])
                    return {
                        "action_type": "github_fetch", "repo": repo,
                        "action": action, "branch": branch_value,
                        "path": path_value, "content": text,
                        "content_length": len(text), "success": True,
                    }

                if action == "fetch_readme":
                    api_url = f"https://api.github.com/repos/{repo}/readme?ref={branch}"
                    resp = client.get(api_url, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    text = self._decode_github_content(data)
                    if len(text) > 20000:
                        text = text[:20000] + "\n\n... (已截断)"
                    self._write_output(context, text[:5000])
                    return {
                        "action_type": "github_fetch", "repo": repo,
                        "action": action, "branch": branch_value,
                        "path": data.get("path", "README"),
                        "content": text, "success": True,
                    }

                if action in {"fetch_issue", "fetch_pull"}:
                    number = reference.number
                    if number is None:
                        return {
                            "action_type": "github_fetch", "repo": repo,
                            "action": action, "content": "", "success": False,
                            "error": f"{action} 需要 issues/pull URL 中的编号",
                        }
                    endpoint = "issues" if action == "fetch_issue" else "pulls"
                    api_url = f"https://api.github.com/repos/{repo}/{endpoint}/{number}"
                    resp = client.get(api_url, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    content = self._format_github_discussion(data, action, number)
                    self._write_output(context, content[:5000])
                    return {
                        "action_type": "github_fetch", "repo": repo,
                        "action": action, "number": number,
                        "state": data.get("state", ""),
                        "title": data.get("title", ""),
                        "content": content, "success": True,
                    }

                if action == "repo_activity":
                    return self._github_repo_activity(
                        client,
                        context,
                        repo,
                        headers,
                    )

                return {
                    "action_type": "github_fetch", "repo": repo,
                    "action": action, "content": "", "success": False,
                    "error": (
                        f"不支持的 github_action: {action}（可选: list_files, "
                        "fetch_file, fetch_readme, fetch_issue, fetch_pull, "
                        "repo_activity）"
                    ),
                }

        except httpx.HTTPStatusError as e:
            remaining = e.response.headers.get("x-ratelimit-remaining", "")
            rate_hint = "（GitHub API 限流）" if remaining == "0" else ""
            if e.response.status_code in {403, 429, 500, 502, 503, 504}:
                fallback = self._github_html_fallback(
                    context,
                    repo,
                    action,
                    reference=reference,
                    reason=f"HTTP {e.response.status_code}{rate_hint}",
                )
                if fallback.get("success"):
                    return fallback
            return {
                "action_type": "github_fetch", "repo": repo,
                "action": action, "content": "", "success": False,
                "retryable": False,
                "error": (
                    f"GitHub API 错误: {e.response.status_code} "
                    f"{e.response.reason_phrase}{rate_hint}"
                ),
            }
        except Exception as e:
            fallback = self._github_html_fallback(
                context,
                repo,
                action,
                reference=reference,
                reason=type(e).__name__,
            )
            if fallback.get("success"):
                return fallback
            return {
                "action_type": "github_fetch", "repo": repo,
                "action": action, "content": "", "success": False,
                "retryable": False,
                "error": f"GitHub 操作失败: {e}",
            }

    def _github_repo_activity(
        self,
        client: Any,
        context: AgentContext,
        repo: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Return compact public maintenance signals without cloning a repo."""
        from datetime import datetime, timezone
        from statistics import median

        repo_resp = client.get(f"https://api.github.com/repos/{repo}", headers=headers)
        repo_resp.raise_for_status()
        repo_data = repo_resp.json()
        pulls_resp = client.get(
            f"https://api.github.com/repos/{repo}/pulls"
            "?state=all&sort=updated&direction=desc&per_page=30",
            headers=headers,
        )
        pulls_resp.raise_for_status()
        pulls = pulls_resp.json()
        if not isinstance(pulls, list):
            pulls = []

        merge_hours: list[float] = []
        merged_count = 0
        open_count = 0
        now = datetime.now(timezone.utc)
        recent_updates = 0
        for pull in pulls:
            if not isinstance(pull, dict):
                continue
            if pull.get("state") == "open":
                open_count += 1
            updated_at = self._parse_github_timestamp(pull.get("updated_at"))
            if updated_at and (now - updated_at).days <= 90:
                recent_updates += 1
            created_at = self._parse_github_timestamp(pull.get("created_at"))
            merged_at = self._parse_github_timestamp(pull.get("merged_at"))
            if created_at and merged_at and merged_at >= created_at:
                merged_count += 1
                merge_hours.append((merged_at - created_at).total_seconds() / 3600)

        median_merge = median(merge_hours) if merge_hours else None
        lines = [
            f"# GitHub 维护信号: {repo}",
            f"- 默认分支: {repo_data.get('default_branch') or '-'}",
            f"- 最近 push: {repo_data.get('pushed_at') or '-'}",
            f"- 仓库更新时间: {repo_data.get('updated_at') or '-'}",
            f"- Open issues/PR 汇总字段: {repo_data.get('open_issues_count', 0)}",
            f"- 最近抽样 PR: {len(pulls)} 条；open {open_count}；merged {merged_count}",
            f"- 90 天内有更新的抽样 PR: {recent_updates}",
        ]
        if median_merge is not None:
            lines.append(f"- 已合并抽样 PR 的中位合并耗时: {median_merge:.1f} 小时")
        lines.extend([
            "",
            "说明：以上是公开 API 的最近 30 条 PR 抽样信号，不能等同于官方 SLA；",
            "比较多个项目时应使用相同时间窗口，并结合 CONTRIBUTING/提交入口核验。",
        ])
        content = "\n".join(lines)
        self._write_output(context, content)
        return {
            "action_type": "github_fetch",
            "repo": repo,
            "action": "repo_activity",
            "success": True,
            "content": content,
            "sample_size": len(pulls),
            "open_pull_count": open_count,
            "merged_pull_count": merged_count,
            "recent_pull_updates": recent_updates,
            "median_merge_hours": median_merge,
        }

    @staticmethod
    def _parse_github_timestamp(value: Any):
        from datetime import datetime

        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _github_html_fallback(
        self,
        context: AgentContext,
        repo: str,
        action: str,
        *,
        reference: Any,
        reason: str,
    ) -> dict[str, Any]:
        """Fall back to a public GitHub HTML page after API/network failure."""
        if action == "fetch_issue" and reference.number is not None:
            url = f"https://github.com/{repo}/issues/{reference.number}"
        elif action == "fetch_pull" and reference.number is not None:
            url = f"https://github.com/{repo}/pull/{reference.number}"
        elif action == "repo_activity":
            url = f"https://github.com/{repo}/pulls?q=is%3Apr"
        else:
            url = f"https://github.com/{repo}"

        try:
            with _create_http_client(
                timeout=min(self.timeout, 20),
                follow_redirects=False,
            ) as client:
                resp = _fetch_with_redirect_check(
                    client,
                    url,
                    headers={"User-Agent": "Xenon/0.7"},
                )
                resp.raise_for_status()
                text = self._html_to_text(resp.text)
                if not text:
                    raise ValueError("公开页面没有可读内容")
                text = text[:30000]
                content = (
                    f"[GitHub API 不可用，已降级读取公开网页：{reason}]\n"
                    f"来源: {url}\n\n{text}"
                )
                self._write_output(context, content[:5000])
                return {
                    "action_type": "github_fetch",
                    "repo": repo,
                    "action": action,
                    "url": url,
                    "content": content,
                    "success": True,
                    "degraded": True,
                    "retryable": False,
                }
        except Exception as fallback_error:
            logger.debug(
                "[%s] GitHub HTML 降级失败 (%s): %s",
                self.id,
                reason,
                fallback_error,
            )
            return {
                "action_type": "github_fetch",
                "repo": repo,
                "action": action,
                "content": "",
                "success": False,
                "retryable": False,
                "error": f"GitHub HTML 降级失败: {fallback_error}",
            }

    @staticmethod
    def _github_headers() -> dict[str, str]:
        """Build GitHub API headers, supporting public and private repositories."""
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Xenon/0.6",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _decode_github_content(data: dict[str, Any]) -> str:
        """Decode the base64 payload returned by GitHub's Contents API."""
        import base64

        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise ValueError("GitHub API 未返回可解码的文件内容")
        raw = base64.b64decode(data["content"], validate=False)
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _format_github_discussion(
        data: dict[str, Any], action: str, number: int,
    ) -> str:
        kind = "Pull Request" if action == "fetch_pull" else "Issue"
        user = data.get("user") or {}
        body = str(data.get("body") or "（无正文）")
        return (
            f"# {kind} #{number}: {data.get('title', '')}\n"
            f"- 状态: {data.get('state', '')}\n"
            f"- 作者: {user.get('login', '')}\n"
            f"- 创建: {data.get('created_at', '')}\n"
            f"- 更新: {data.get('updated_at', '')}\n\n"
            f"{body[:30000]}"
        )

    @staticmethod
    def _github_default_branch(client: Any, repo: str, headers: dict[str, str]) -> str:
        cached = _GITHUB_DEFAULT_BRANCH_CACHE.get(repo)
        if cached:
            return cached
        resp = client.get(f"https://api.github.com/repos/{repo}", headers=headers)
        resp.raise_for_status()
        branch = str(resp.json().get("default_branch") or "")
        if not branch:
            raise ValueError(f"GitHub 未返回 {repo} 的默认分支")
        _GITHUB_DEFAULT_BRANCH_CACHE[repo] = branch
        return branch

    def _clone_repo(self, context: AgentContext) -> dict[str, Any]:
        """将 GitHub 仓库克隆到本地缓存并返回结构化摘要。

        - 缓存目录：~/.xenon/repos/{owner}_{repo}/
        - 浅克隆（--depth 1），节省时间和空间
        - 自动分析：目录结构、关键文件、代码统计
        """
        import subprocess
        repo_input = self._resolve_template(self.repo, context)
        if not repo_input:
            raise ValueError(f"[{self.id}] clone_repo 需要 repo 参数（格式: owner/repo 或完整 URL）")

        try:
            reference = parse_github_reference(repo_input)
        except ValueError as exc:
            return {
                "action_type": "clone_repo", "repo": repo_input,
                "success": False,
                "error": str(exc),
            }
        repo = reference.slug

        # ── 构建缓存路径 ──
        cache_root = Path.home() / ".xenon" / "repos"
        cache_root.mkdir(parents=True, exist_ok=True)
        target_dir = cache_root / repo.replace("/", "_")

        # ── 决议分支（前置：无论命中缓存与否都需要，供给 _analyze_cloned_repo）──
        clone_url = f"https://github.com/{repo}.git"
        branch = self._resolve_branch_for_clone(
            clone_url,
            context,
            preferred_ref=reference.ref,
        )
        git_env = self._git_auth_env()
        cache_updated = False
        cache_warning = ""

        # ── 克隆（如果尚未缓存）──
        if not (target_dir / ".git").exists():
            # v0.6.1: 清理残留目录（上次克隆失败留下的半拉子目录）
            self._rmtree_cleanup(target_dir)

            logger.info(f"[{self.id}] 克隆仓库: {clone_url} → {target_dir}")
            try:
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", "--single-branch", "-b", branch,
                     clone_url, str(target_dir)],
                    capture_output=True, text=True, timeout=self.timeout, env=git_env,
                )
                if result.returncode != 0:
                    stderr = result.stderr.strip()
                    return {
                        "action_type": "clone_repo", "repo": repo,
                        "success": False,
                        "error": (
                            f"git clone 失败 (branch={branch}): {_last_error_lines(stderr)}"
                            f"\n提示: 仓库可能不存在、已改名或需认证。可尝试用浏览器打开 {clone_url}"
                        ),
                    }
                cache_updated = True
            except FileNotFoundError:
                return {
                    "action_type": "clone_repo", "repo": repo,
                    "success": False,
                    "error": "本机未安装 git，无法克隆仓库。请先安装 git。",
                }
            except subprocess.TimeoutExpired:
                self._rmtree_cleanup(target_dir)
                return {
                    "action_type": "clone_repo", "repo": repo,
                    "success": False,
                    "retryable": False,
                    "error": (
                        f"git clone 超时（>{self.timeout}s），已停止并清理不完整缓存；"
                        "为避免重复长任务，本次不会自动重试"
                    ),
                }
        else:
            logger.info(f"[{self.id}] 仓库已缓存: {target_dir}")
            # Refresh Xenon's cache without discarding local edits. A dirty or
            # diverged cache remains usable, but the caller sees a warning.
            try:
                fetch = subprocess.run(
                    ["git", "-C", str(target_dir), "fetch", "--depth", "1", "origin", branch],
                    capture_output=True, text=True, timeout=self.timeout, env=git_env,
                )
                if fetch.returncode == 0:
                    merge = subprocess.run(
                        ["git", "-C", str(target_dir), "merge", "--ff-only", "FETCH_HEAD"],
                        capture_output=True, text=True, timeout=self.timeout, env=git_env,
                    )
                    cache_updated = merge.returncode == 0
                    if not cache_updated:
                        cache_warning = (
                            "缓存存在本地修改或分叉，未覆盖；继续分析现有缓存: "
                            + _last_error_lines(merge.stderr)
                        )
                else:
                    cache_warning = (
                        "无法更新远程仓库，继续分析现有缓存: "
                        + _last_error_lines(fetch.stderr)
                    )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                cache_warning = f"缓存更新失败，继续分析现有缓存: {exc}"

        # ── 分析克隆结果 ──
        analysis = self._analyze_cloned_repo(target_dir, repo, branch)
        analysis["cache_updated"] = cache_updated
        if cache_warning:
            analysis["cache_warning"] = cache_warning
            analysis["content"] += f"\n\n- 缓存提示: {cache_warning}"
        return analysis

    # ── clone_repo 辅助方法 ───────────────────────────────────

    @staticmethod
    def _rmtree_cleanup(target_dir: Path) -> None:
        """清理残留目录（上次克隆失败留下的半拉子目录）。

        与 shutil.rmtree(ignore_errors=True) 不同：
        - 先尝试正常删除
        - 删除失败时记录 error 日志（留下排查痕迹）
        - 不抛异常——清理是尽力而为，不应阻塞 clone 流程
        """
        if not target_dir.exists():
            return
        try:
            shutil.rmtree(target_dir)
            logger.info("已清理残留目录: %s", target_dir)
        except OSError as e:
            logger.error(
                "清理残留目录失败 (%s)，clone 可能因 '目录非空' 失败: %s",
                target_dir, e,
            )

    @staticmethod
    def _git_auth_env() -> dict[str, str]:
        """Pass GitHub auth to git without embedding a token in the clone URL."""
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            try:
                config_index = int(env.get("GIT_CONFIG_COUNT", "0"))
            except ValueError:
                config_index = 0
            env.update({
                "GIT_CONFIG_COUNT": str(config_index + 1),
                f"GIT_CONFIG_KEY_{config_index}": "http.extraHeader",
                f"GIT_CONFIG_VALUE_{config_index}": f"Authorization: Bearer {token}",
            })
        return env

    def _resolve_branch_for_clone(
        self,
        clone_url: str,
        context: AgentContext,
        *,
        preferred_ref: str = "",
    ) -> str:
        """决议 clone 使用的分支名。

        优先级：
        1. 用户显式指定分支（通过 template 参数）
        2. git ls-remote 探测远程 HEAD 指向的默认分支
        3. 兜底 'main'

        与旧版 main→master 回退相比：不再靠猜，而是用 ls-remote 一次查清。
        覆盖 main / master / develop / trunk 等任意默认分支名。
        """
        # 第 1 层：用户显式指定
        explicit = self._resolve_template(self.branch, context)
        if explicit and explicit.strip():
            return explicit.strip()
        if preferred_ref:
            return preferred_ref

        # 第 2 层：ls-remote 探测
        import subprocess
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--symref", clone_url, "HEAD"],
                capture_output=True, text=True, timeout=10,
                env=self._git_auth_env(),
            )
            if result.returncode == 0:
                # 输出形如: ref: refs/heads/main	HEAD
                import re
                m = re.search(r"ref: refs/heads/(\S+)", result.stdout)
                if m:
                    default_branch = m.group(1)
                    logger.info(
                        "[%s] ls-remote 探测默认分支: %s",
                        self.id, default_branch,
                    )
                    return default_branch
        except Exception as e:
            logger.debug("[%s] ls-remote 失败，fallback main: %s", self.id, e)

        # 第 3 层：兜底
        logger.debug("[%s] 无法探测默认分支，兜底 main", self.id)
        return "main"

    @staticmethod
    def _analyze_cloned_repo(target_dir: Path, repo: str, branch: str) -> dict[str, Any]:
        """分析已克隆的仓库，返回结构化摘要。"""
        import fnmatch

        # ── 文件列表 ──
        all_files: list[str] = []
        dirs: dict[str, int] = {}  # 顶层目录 → 文件数
        key_files: dict[str, str] = {}  # 关键文件 → 描述
        lang_counts: dict[str, int] = {}  # 语言 → 文件数
        total_lines = 0

        # 忽略的目录和文件
        ignore_patterns = [
            ".git", "__pycache__", "node_modules", ".venv", "venv",
            ".tox", ".eggs", "*.egg-info", ".pytest_cache", ".mypy_cache",
            ".ruff_cache", "dist", "build", "*.pyc", ".DS_Store",
        ]

        ext_to_lang = {
            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
            ".go": "Go", ".rs": "Rust", ".java": "Java", ".c": "C",
            ".cpp": "C++", ".h": "C/C++ Header", ".rb": "Ruby",
            ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
            ".yaml": "YAML", ".yml": "YAML", ".json": "JSON",
            ".toml": "TOML", ".md": "Markdown", ".rst": "reStructuredText",
            ".txt": "Text", ".html": "HTML", ".css": "CSS",
            ".sql": "SQL", ".dockerfile": "Dockerfile",
        }

        key_file_patterns = {
            "README.md": "项目说明", "README.rst": "项目说明",
            "README": "项目说明", "pyproject.toml": "Python 项目配置",
            "setup.py": "Python 打包配置", "setup.cfg": "Python 打包配置",
            "package.json": "Node.js 项目配置", "Cargo.toml": "Rust 项目配置",
            "go.mod": "Go 模块定义", "Makefile": "构建脚本",
            "Dockerfile": "容器镜像定义", "docker-compose.yml": "容器编排",
            ".github/workflows": "CI 工作流", "LICENSE": "许可证",
        }

        for root, _dirs, files in os.walk(target_dir):
            # 跳过忽略的目录
            rel_root = os.path.relpath(root, target_dir)
            parts = rel_root.split(os.sep)
            if any(fnmatch.fnmatch(p, pat) or p in ignore_patterns for p in parts for pat in ignore_patterns):
                _dirs[:] = []  # 不进入子目录
                continue
            # 就地过滤忽略的目录
            _ignored: list[str] = []
            for d in _dirs:
                if d in ignore_patterns or any(fnmatch.fnmatch(d, p) for p in ignore_patterns):
                    _ignored.append(d)
            for d in _ignored:
                _dirs.remove(d)

            for fname in sorted(files):
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, target_dir)
                all_files.append(rel_path)

                # 顶层目录统计
                top_dir = rel_path.split(os.sep)[0] if os.sep in rel_path else "(root)"
                dirs[top_dir] = dirs.get(top_dir, 0) + 1

                # 语言统计
                _, ext = os.path.splitext(fname)
                lang = ext_to_lang.get(ext.lower(), ext or "(no ext)")
                lang_counts[lang] = lang_counts.get(lang, 0) + 1

                # 行数统计（仅文本文件）
                if ext.lower() in {'.py', '.js', '.ts', '.go', '.rs', '.java', '.c', '.cpp',
                                    '.h', '.rb', '.sh', '.bash', '.zsh', '.yaml', '.yml',
                                    '.json', '.toml', '.md', '.rst', '.txt', '.html', '.css',
                                    '.sql', ''}:
                    try:
                        with open(fpath, encoding='utf-8', errors='ignore') as f:
                            line_count = sum(1 for _ in f)
                        total_lines += line_count
                    except Exception:
                        pass

                # 关键文件识别
                for pattern, desc in key_file_patterns.items():
                    if pattern.startswith("."):
                        # 目录模式（如 .github/workflows）
                        if rel_path.startswith(pattern + os.sep) or rel_path == pattern:
                            key_files[rel_path] = desc
                    elif fname == pattern:
                        key_files[rel_path] = desc

        # ── 构建返回结果 ──
        # 顶层目录（按文件数降序，最多 15 个）
        sorted_dirs = sorted(dirs.items(), key=lambda x: -x[1])[:15]
        dir_tree = "\n".join(f"  {d}/ ({n} files)" for d, n in sorted_dirs)

        # 语言统计（按文件数降序，最多 10 个）
        sorted_langs = sorted(lang_counts.items(), key=lambda x: -x[1])[:10]
        lang_summary = ", ".join(
            f"{language}: {count}" for language, count in sorted_langs
        )

        # 关键文件（最多 20 个）
        key_list = [f"  {p} — {d}" for p, d in sorted(key_files.items())[:20]]
        key_summary = "\n".join(key_list) if key_list else "  (未识别到关键文件)"

        summary = (
            f"# 仓库分析: {repo}\n"
            f"- 路径: {target_dir}\n"
            f"- 分支: {branch}\n"
            f"- 文件总数: {len(all_files)}\n"
            f"- 代码总行数: {total_lines:,}\n"
            f"- 语言: {lang_summary}\n"
            f"\n## 目录结构\n{dir_tree}\n"
            f"\n## 关键文件\n{key_summary}"
        )

        return {
            "action_type": "clone_repo",
            "repo": repo,
            "repo_path": str(target_dir),
            "branch": branch,
            "file_count": len(all_files),
            "total_lines": total_lines,
            "top_dirs": dict(sorted_dirs),
            "languages": dict(sorted_langs),
            "key_files": {p: d for p, d in sorted(key_files.items())},
            "content": summary,
            "success": True,
        }

    # ── LSP 工具（基于 Jedi）────────────────────────────────

    def _lsp_goto_def(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 跳转到定义。"""
        return self._lsp_call("goto_definition", context)

    def _lsp_find_refs(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 查找引用。"""
        return self._lsp_call("find_references", context)

    def _lsp_hover(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 悬停信息。"""
        return self._lsp_call("get_hover", context)

    def _lsp_diagnostics(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 诊断信息。"""
        from xenon.utils.lsp_provider import LSPProvider
        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {"success": False, "error": "缺少 file_path 参数"}
        return LSPProvider.get_diagnostics(file_path)

    def _lsp_symbols(self, context: AgentContext) -> dict[str, Any]:
        """LSP: 文件符号列表。"""
        from xenon.utils.lsp_provider import LSPProvider
        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {"success": False, "error": "缺少 file_path 参数"}
        return LSPProvider.get_symbols(file_path)

    def _lsp_call(self, method: str, context: AgentContext) -> dict[str, Any]:
        """通用 LSP 调用分派。"""
        from xenon.utils.lsp_provider import LSPProvider

        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {"success": False, "error": "缺少 file_path 参数"}

        # line/column 从原始 action_input 获取（通过 normalize_params 后落在 action 字段中，
        # 但实际 line/column 是独立的参数，需要特殊处理）
        # 实际上 action_input = {"file_path": "...", "line": 10, "column": 5}
        # normalize_params 把 file_path 映射到 self.file_path，line/column 没有标准映射
        # 所以需要在 tool 执行时从原始 context 或 action_input 获取
        # v0.6.1: 对于 LSP 工具，line/column 从 self 的附加属性获取
        line = getattr(self, '_lsp_line', None)
        column = getattr(self, '_lsp_column', None)

        if line is None or column is None:
            return {"success": False, "error": "缺少 line 或 column 参数"}

        try:
            line = int(line)
            column = int(column)
        except (ValueError, TypeError):
            return {"success": False, "error": f"line/column 必须为整数: line={line}, column={column}"}

        if method == "goto_definition":
            return LSPProvider.goto_definition(file_path, line, column)
        elif method == "find_references":
            return LSPProvider.find_references(file_path, line, column)
        elif method == "get_hover":
            return LSPProvider.get_hover(file_path, line, column)
        else:
            return {"success": False, "error": f"未知 LSP 方法: {method}"}

    @staticmethod
    def _html_to_text(html: str) -> str:
        """简单 HTML 转纯文本。"""
        import re
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</(p|div|h[1-6]|li|tr)>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    # ── 动态工具注册 ──────────────────────────────────────

    def _register_tool(self, context: AgentContext) -> dict[str, Any]:
        """注册一个自定义工具。支持两种模式：
        1. python_function: 指定 module_path.function_name，系统自动导入
        2. command_template: 指定命令模板，工具调用时执行 shell 命令
        """
        tool_name = self._resolve_template(getattr(self, "tool_name", ""), context)
        description = self._resolve_template(getattr(self, "description", ""), context)
        params_raw = getattr(self, "params", {})
        if isinstance(params_raw, str):
            import json
            try:
                params_raw = json.loads(params_raw)
            except json.JSONDecodeError:
                params_raw = {}

        if not tool_name:
            return {"action_type": "register_tool", "success": False, "error": "缺少 tool_name 参数"}

        # A3: 重名检查 — 禁止劫持内置 action_type，禁止覆盖已注册动态工具（除非 overwrite=True）
        overwrite = str(self._resolve_template(getattr(self, "overwrite", ""), context)).strip().lower() in (
            "1", "true", "yes", "on",
        )
        if tool_name in _BUILTIN_ACTION_TYPES:
            return {"action_type": "register_tool", "success": False,
                    "error": f"工具名 '{tool_name}' 与内置 action_type 冲突，禁止注册（防内置工具名劫持）"}
        if tool_name in _DYNAMIC_TOOLS and not overwrite:
            return {"action_type": "register_tool", "success": False,
                    "error": f"工具名 '{tool_name}' 已被注册为动态工具；如需覆盖请显式设置 overwrite=true"}

        # 模式 1: Python 函数
        python_function = self._resolve_template(getattr(self, "python_function", ""), context)
        if python_function:
            try:
                parts = python_function.rsplit(".", 1)
                if len(parts) != 2:
                    return {"action_type": "register_tool", "success": False,
                            "error": f"python_function 格式错误，应为 module.function，收到: {python_function}"}
                module_path, func_name = parts
                # A1: 模块白名单校验 — 拒绝导入 os/subprocess/builtins/importlib 等危险模块
                ok, reason = _validate_register_module(module_path)
                if not ok:
                    return {"action_type": "register_tool", "success": False, "error": reason}
                import importlib
                mod = importlib.import_module(module_path)
                func = getattr(mod, func_name)
                if not callable(func):
                    return {"action_type": "register_tool", "success": False,
                            "error": f"{python_function} 不是可调用对象"}

                def make_handler(fn):
                    def handler(ctx):
                        # 从上下文中提取参数
                        kwargs = {}
                        for key in (params_raw.get("properties") or {}):
                            val = ctx.get(key)
                            if val is not None:
                                kwargs[key] = val
                        try:
                            result = fn(**kwargs) if kwargs else fn()
                            return {"action_type": tool_name, "success": True, "content": str(result)}
                        except Exception as e:
                            return {"action_type": tool_name, "success": False, "error": str(e)}
                    return handler

                register_dynamic_tool(tool_name, make_handler(func), description or f"自定义工具: {tool_name}", params_raw)
                msg = f"✅ 工具 '{tool_name}' 注册成功（Python 函数: {python_function}）"
                logger.info(f"[register_tool] {msg}")
                return {"action_type": "register_tool", "success": True, "content": msg}

            except Exception as e:
                return {"action_type": "register_tool", "success": False, "error": f"注册失败: {e}"}

        # 模式 2: Shell 命令模板
        command_template = self._resolve_template(getattr(self, "command_template", ""), context)
        if command_template:
            def cmd_handler(ctx):
                import shlex
                cmd = command_template
                # 替换模板变量（A4: 对替换值 shlex.quote 防 shell 注入）
                for key in (params_raw.get("properties") or {}):
                    val = ctx.get(key)
                    if val is not None:
                        cmd = cmd.replace(f"{{{key}}}", shlex.quote(str(val)))
                try:
                    result = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=30
                    )
                    output = result.stdout.strip()
                    if result.returncode != 0:
                        output += f"\nSTDERR: {result.stderr.strip()}"
                    return {"action_type": tool_name, "success": result.returncode == 0,
                            "content": output, "command": cmd}
                except subprocess.TimeoutExpired:
                    return {"action_type": tool_name, "success": False, "error": "命令超时 (30s)"}
                except Exception as e:
                    return {"action_type": tool_name, "success": False, "error": str(e)}

            register_dynamic_tool(tool_name, cmd_handler, description or f"自定义命令: {tool_name}", params_raw)
            msg = f"✅ 工具 '{tool_name}' 注册成功（命令模板: {command_template}）"
            logger.info(f"[register_tool] {msg}")
            return {"action_type": "register_tool", "success": True, "content": msg}

        return {"action_type": "register_tool", "success": False,
                "error": "必须提供 python_function 或 command_template 参数"}

    def _exec_dynamic_tool(self, tool_info: dict, context: AgentContext) -> dict[str, Any]:
        """执行已注册的动态工具。"""
        handler = tool_info["handler"]
        try:
            # 将 ToolNode 的属性作为参数传给 handler
            result = handler(context)
            return result if isinstance(result, dict) else {"action_type": self.action_type, "success": True, "content": str(result)}
        except Exception as e:
            logger.error(f"[动态工具] {self.action_type} 执行失败: {e}")
            return {"action_type": self.action_type, "success": False, "error": str(e)}

    # ── 模板替换 ──────────────────────────────────────────

    @staticmethod
    def _resolve_template(template: str, context: AgentContext) -> str:
        import re
        def _replace(m: re.Match) -> str:
            key = m.group(1)
            val = context.get(key)
            return str(val) if val is not None else m.group(0)
        return re.sub(r"\{(\w+)\}", _replace, template)
