"""GitHub retrieval and repository analysis tools.

The mixin keeps GitHub API semantics, public-page fallback, clone caching,
and repository summarization behind one extension boundary. Compatibility
seams are resolved from ``tool_node`` at call time so existing callers and
tests remain stable.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

from xenon.engine.context import AgentContext
from xenon.utils.github_auth import github_auth_headers, load_github_token
from xenon.utils.github_reference import parse_github_reference

logger = logging.getLogger(__name__)


def _tool_node_module():
    import xenon.nodes.tool_node as tool_module
    return tool_module

class GitHubToolsMixin:
    """Fetch GitHub resources and clone/analyse repositories."""
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
            with _tool_node_module()._create_http_client(timeout=self.timeout, follow_redirects=True) as client:
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
            with _tool_node_module()._create_http_client(
                timeout=min(self.timeout, 20),
                follow_redirects=False,
            ) as client:
                resp = _tool_node_module()._fetch_with_redirect_check(
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
        headers.update(github_auth_headers())
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
        cached = _tool_node_module()._GITHUB_DEFAULT_BRANCH_CACHE.get(repo)
        if cached:
            return cached
        resp = client.get(f"https://api.github.com/repos/{repo}", headers=headers)
        resp.raise_for_status()
        branch = str(resp.json().get("default_branch") or "")
        if not branch:
            raise ValueError(f"GitHub 未返回 {repo} 的默认分支")
        _tool_node_module()._GITHUB_DEFAULT_BRANCH_CACHE[repo] = branch
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
                            f"git clone 失败 (branch={branch}): {_tool_node_module()._last_error_lines(stderr)}"
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
                            + _tool_node_module()._last_error_lines(merge.stderr)
                        )
                else:
                    cache_warning = (
                        "无法更新远程仓库，继续分析现有缓存: "
                        + _tool_node_module()._last_error_lines(fetch.stderr)
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
        token = load_github_token()
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
