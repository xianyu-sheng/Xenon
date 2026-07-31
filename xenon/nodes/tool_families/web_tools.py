"""Web and documentation retrieval tools.

This family owns HTTP response handling and ``llms.txt`` discovery.  URL
policy and redirect validation live in :mod:`xenon.nodes.network_security` so
other network-backed tools can reuse the same boundary.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from xenon.engine.context import AgentContext
from xenon.nodes.network_security import SSRFRedirectError, SecurityError
from xenon.utils.github_reference import parse_github_reference

logger = logging.getLogger(__name__)


def _tool_node_module():
    """Return the compatibility module lazily to avoid an import cycle.

    Existing callers/tests patch ``xenon.nodes.tool_node._create_http_client``
    and ``_ssrf_check_url``.  Looking these up at call time preserves that
    useful seam while the implementation itself lives in this mixin.
    """
    import xenon.nodes.tool_node as tool_module

    return tool_module


class WebToolsMixin:
    """Fetch web pages and bounded official documentation bundles."""

    def _web_fetch(self, context: AgentContext) -> dict[str, Any]:
        """抓取网页内容，返回纯文本。"""
        tool_module = _tool_node_module()
        url = self._resolve_template(self.url, context)
        if not url:
            url = self._resolve_template(self.action, context)
        if not url:
            raise ValueError(f"[{self.id}] web_fetch 需要 url")

        host = (urlparse(url).hostname or "").lower()
        if host in {"github.com", "www.github.com", "raw.githubusercontent.com"}:
            try:
                parse_github_reference(url)
            except ValueError:
                if host == "raw.githubusercontent.com":
                    return {
                        "action_type": "web_fetch", "url": url,
                        "content": "", "success": False,
                        "retryable": False,
                        "error": "无效的 GitHub raw 文件 URL",
                    }
            else:
                # Keep the historical delegation semantics.  Importing here
                # avoids a module cycle during ToolNode class construction.
                github_node = tool_module.ToolNode(
                    f"{self.id}:github",
                    action_type="github_fetch",
                    repo=url,
                    branch="",
                    timeout=self.timeout,
                    output_slot=self.output_slot,
                    security_enabled=self.security_enabled,
                )
                return github_node._github_fetch(context)

        ok, reason = tool_module._ssrf_check_url(url)
        if not ok:
            return {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False,
                "error": (
                    f"SSRF 拦截: {reason}"
                    f"。可尝试用 command 工具执行 curl 获取数据作为降级方案"
                ),
            }

        logger.info("[%s] 抓取网页: %s", self.id, url)
        try:
            with tool_module._create_http_client(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                resp = tool_module._fetch_with_redirect_check(client, url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                text = self._html_to_text(resp.text) if "text/html" in content_type else resp.text
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
        except (SSRFRedirectError, tool_module._SSRFRedirectError) as exc:
            return {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False,
                "error": f"SSRF 拦截(重定向): {exc}",
            }
        except Exception as exc:
            result = {
                "action_type": "web_fetch", "url": url,
                "content": "", "success": False, "error": str(exc),
            }
            self._write_output(context, f"抓取失败: {exc}")
            return result

    def _docs_fetch(self, context: AgentContext) -> dict[str, Any]:
        """Discover llms.txt and retrieve a bounded, query-relevant doc bundle."""
        import httpx

        tool_module = _tool_node_module()
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
        query = self._resolve_template(self.query or self.search_pattern, context)
        max_pages = max(0, min(int(self.max_pages), 8))
        max_chars = max(1000, min(int(self.max_chars), 30000))
        discovery_urls = llms_candidate_urls(url)
        attempts: list[dict[str, Any]] = []

        def fetch_text(client, target: str) -> tuple[str, str, int]:
            ok, reason = tool_module._ssrf_check_url(target)
            if not ok:
                raise SecurityError(f"SSRF 拦截: {reason}")
            response = tool_module._fetch_with_redirect_check(client, target)
            if response.status_code == 404:
                return "", str(response.url), 404
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            text = self._html_to_text(response.text) if "text/html" in content_type else response.text
            return text, str(response.url), response.status_code

        try:
            with tool_module._create_http_client(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                index_text = ""
                index_url = ""
                index_kind = ""
                for candidate in discovery_urls:
                    try:
                        text, final_url, status = fetch_text(client, candidate)
                    except (httpx.HTTPError, SSRFRedirectError,
                            tool_module._SSRFRedirectError, SecurityError) as exc:
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
                        "action_type": "docs_fetch", "url": url,
                        "strategy": "llms-full", "discovery_url": index_url,
                        "discovery_attempts": attempts,
                        "selected_sources": [index_url], "discovered_links": 0,
                        "content": content, "content_length": len(content),
                        "truncated": truncated, "success": True,
                    }
                    self._write_output(context, content[:5000])
                    return result

                if index_text:
                    try:
                        document = parse_llms_txt(index_text, index_url)
                    except ValueError as exc:
                        attempts.append({"url": index_url, "error": str(exc)})
                    else:
                        selected = select_llms_links(document, query, max_pages=max_pages)
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
                            except Exception as exc:
                                source_errors.append({"url": link.url, "error": str(exc)[:160]})
                                continue
                            selected_sources.append(final_url)
                            parts.extend([f"## {link.title}", f"Source: {final_url}", page])
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
                            "action_type": "docs_fetch", "url": url, "query": query,
                            "strategy": "llms-index", "discovery_url": index_url,
                            "discovery_attempts": attempts,
                            "selected_sources": selected_sources,
                            "source_errors": source_errors,
                            "discovered_links": len(document.links),
                            "optional_links": sum(1 for link in document.links if link.optional),
                            "content": content, "content_length": len(content),
                            "truncated": truncated, "success": True,
                        }
                        self._write_output(context, content[:5000])
                        return result

            fallback = tool_module.ToolNode(
                f"{self.id}:fallback", action_type="web_fetch", url=url,
                timeout=self.timeout, output_slot=self.output_slot,
                security_enabled=self.security_enabled,
            )._web_fetch(context)
            fallback["action_type"] = "docs_fetch"
            fallback["strategy"] = "html-fallback"
            fallback["discovery_attempts"] = attempts
            fallback["degraded"] = True
            return fallback
        except Exception as exc:
            result = {
                "action_type": "docs_fetch", "url": url, "strategy": "failed",
                "discovery_attempts": attempts, "content": "", "success": False,
                "error": str(exc),
            }
            self._write_output(context, f"文档抓取失败: {exc}")
            return result

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Convert a small HTML response to plain text without extra deps."""
        import re

        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

