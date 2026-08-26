"""Parse GitHub repository and resource references without regex ambiguity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_NON_REPOSITORY_ROUTES = frozenset(
    {
        "collections",
        "enterprise",
        "explore",
        "features",
        "marketplace",
        "notifications",
        "organizations",
        "orgs",
        "search",
        "settings",
        "sponsors",
        "topics",
        "users",
        # GitHub Discussions / Projects 等非仓库页面
        "discussions",
        "projects",
    }
)


@dataclass(frozen=True)
class GitHubReference:
    owner: str
    repo: str
    kind: str = "repo"  # repo | blob | tree | issue | pull
    ref: str = ""
    path: str = ""
    number: int | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def _validate(owner: str, repo: str) -> None:
    if not _PART_RE.fullmatch(owner) or not _PART_RE.fullmatch(repo):
        raise ValueError("GitHub 仓库格式应为 owner/repo，仅允许字母数字及 ._- 字符")


def parse_github_reference(value: str) -> GitHubReference:
    """Parse owner/repo, HTTPS, SSH, blob/tree, issue and pull URLs.

    Query strings and fragments are deliberately discarded. For ``blob`` and
    ``tree`` URLs the first segment after the kind is treated as the ref; users
    with slash-containing branch names can pass ``branch`` explicitly.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("GitHub 仓库不能为空")

    if raw.startswith("git@github.com:"):
        raw = "https://github.com/" + raw.split(":", 1)[1]
    elif raw.startswith("ssh://git@github.com/"):
        raw = "https://github.com/" + raw.split("github.com/", 1)[1]
    elif raw.startswith("github.com/") or raw.startswith("www.github.com/"):
        raw = "https://" + raw

    host = ""
    if "://" in raw:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if host not in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
            raise ValueError(f"仅支持 GitHub URL，收到主机: {host or '(空)'}")
        parts = [unquote(part) for part in parsed.path.split("/") if part]
    else:
        # Strip URL-like query/fragment suffixes from owner/repo input too.
        clean = raw.split("#", 1)[0].split("?", 1)[0]
        parts = [unquote(part) for part in clean.split("/") if part]

    if len(parts) < 2:
        raise ValueError("GitHub 仓库格式应为 owner/repo")

    if (
        host in {"github.com", "www.github.com"}
        and parts[0].lower() in _NON_REPOSITORY_ROUTES
    ):
        raise ValueError("GitHub URL 指向站点页面而不是 owner/repo 仓库")

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    _validate(owner, repo)

    if not host and len(parts) > 2:
        raise ValueError("简写格式只能是 owner/repo；子资源请使用完整 GitHub URL")

    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise ValueError("GitHub raw URL 缺少 ref 或文件路径")
        return GitHubReference(
            owner,
            repo,
            kind="blob",
            ref=parts[2],
            path="/".join(parts[3:]),
        )

    if len(parts) == 2:
        return GitHubReference(owner, repo)

    kind = parts[2].lower()
    if kind in {"blob", "tree"}:
        if len(parts) < 4:
            raise ValueError(f"GitHub {kind} URL 缺少分支或引用")
        return GitHubReference(
            owner,
            repo,
            kind=kind,
            ref=parts[3],
            path="/".join(parts[4:]),
        )

    if kind in {"issues", "pull", "pulls", "discussions"}:
        if len(parts) < 4 or not parts[3].isdigit():
            raise ValueError(f"GitHub {kind} URL 缺少有效编号")
        normalized = (
            "issue"
            if kind == "issues"
            else "pull"
            if kind in {"pull", "pulls"}
            else "discussion"
        )
        return GitHubReference(
            owner,
            repo,
            kind=normalized,
            number=int(parts[3]),
        )

    # Repository subpages such as /actions or /releases still identify the repo.
    return GitHubReference(owner, repo)
