"""Web 工具 — WebFetchTool, GithubFetchTool。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from omniagent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 50_000


def _html_to_text(html: str) -> str:
    """简单 HTML 转纯文本。"""
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


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "通过 HTTP GET 请求抓取任意 URL 的内容并返回文本。HTML 页面会自动转为纯文本。"
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要抓取的完整 URL，如 https://example.com/api/data"},
        },
        "required": ["url"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        url = str(params.get("url", ""))
        if not url:
            return ToolResult.schema_error("web_fetch 需要 url 参数")

        # URL 安全检查
        url_lower = url.lower().strip()
        if url_lower.startswith("file://"):
            return ToolResult.permission_denied("禁止访问 file:// 协议")
        if any(url_lower.startswith(p) for p in [
            "http://169.254", "http://10.", "http://172.1",
            "http://192.168", "http://localhost", "http://127.",
        ]):
            return ToolResult.permission_denied("禁止访问内网地址")

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "OmniAgent-CLI/0.3"})
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                text = _html_to_text(resp.text) if "text/html" in content_type else resp.text

                if len(text) > MAX_CONTENT_LENGTH:
                    text = text[:MAX_CONTENT_LENGTH] + "\n\n... (内容已截断)"

                return ToolResult.ok(text, status_code=resp.status_code, url=url)

        except httpx.HTTPStatusError as e:
            return ToolResult.error(f"HTTP {e.response.status_code}: {url}")
        except Exception as e:
            return ToolResult.error(f"抓取失败: {e}")


class GithubFetchTool(BaseTool):
    name = "github_fetch"
    description = (
        "GitHub 仓库专用操作工具。list_files: 列出仓库中所有文件路径（通过 GitHub API）；"
        "fetch_file: 获取指定文件的源码内容；fetch_readme: 自动查找并获取 README 文件。仅支持公开仓库。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "仓库标识，格式为 owner/repo"},
            "github_action": {
                "type": "string",
                "description": "list_files | fetch_file | fetch_readme",
            },
            "github_path": {"type": "string", "description": "文件路径（仅 fetch_file 时需要）"},
            "branch": {"type": "string", "description": "分支名（可选，默认 main）"},
        },
        "required": ["repo"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        repo = str(params.get("repo", "")).strip().rstrip("/")
        if "github.com" in repo:
            m = re.search(r"github\.com/([^/]+/[^/]+)", repo)
            if m:
                repo = m.group(1)

        action = str(params.get("github_action", "list_files"))
        branch = str(params.get("branch", "main"))
        github_path = str(params.get("github_path", ""))

        headers = {"User-Agent": "OmniAgent-CLI/0.3"}

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                if action == "list_files":
                    api_url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
                    resp = await client.get(api_url, headers=headers)
                    if resp.status_code == 404:
                        api_url = f"https://api.github.com/repos/{repo}/git/trees/master?recursive=1"
                        resp = await client.get(api_url, headers=headers)
                    resp.raise_for_status()
                    tree = resp.json().get("tree", [])
                    files = [
                        item["path"] for item in tree
                        if item.get("type") == "blob" and not item["path"].startswith(".git/")
                    ]
                    result_text = f"仓库 {repo} 共 {len(files)} 个文件:\n" + "\n".join(files)
                    if len(result_text) > 10_000:
                        result_text = result_text[:10_000] + f"\n\n... (共 {len(files)} 个文件，已截断)"
                    return ToolResult.ok(result_text, file_count=len(files), files=files)

                elif action == "fetch_file":
                    if not github_path:
                        return ToolResult.schema_error("fetch_file 需要 github_path 参数")
                    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{github_path}"
                    resp = await client.get(raw_url, headers=headers)
                    if resp.status_code == 404:
                        raw_url = f"https://raw.githubusercontent.com/{repo}/master/{github_path}"
                        resp = await client.get(raw_url, headers=headers)
                    resp.raise_for_status()
                    text = resp.text[:MAX_CONTENT_LENGTH]
                    if len(resp.text) > MAX_CONTENT_LENGTH:
                        text += "\n\n... (内容已截断)"
                    return ToolResult.ok(text, url=raw_url, content_length=len(resp.text))

                elif action == "fetch_readme":
                    for name in ["README.md", "readme.md", "README.rst", "README"]:
                        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{name}"
                        resp = await client.get(raw_url, headers=headers)
                        if resp.status_code == 200:
                            text = resp.text[:20_000]
                            if len(resp.text) > 20_000:
                                text += "\n\n... (已截断)"
                            return ToolResult.ok(text, readme_name=name)
                    return ToolResult.error("未找到 README 文件")

                else:
                    return ToolResult.schema_error(
                        f"不支持的 github_action: {action}。可选: list_files, fetch_file, fetch_readme"
                    )

        except httpx.HTTPStatusError as e:
            return ToolResult.error(f"GitHub API 错误: {e.response.status_code}")
        except Exception as e:
            return ToolResult.error(f"GitHub 操作失败: {e}")
