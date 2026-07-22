"""Rule-first detection for explicit memory requests and safe suggestions."""

from __future__ import annotations

import re

from xenon.memory.compiler import estimate_tokens
from xenon.memory.models import MemoryKind, MemoryProposal, MemoryScope


_SECRET_PATTERNS = (
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|secret|password|passwd)\b\s*[:=]", re.I),
    re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:ghp_|github_pat_|AKIA)[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{12,}\b", re.I),
    re.compile(r"(?:密码|密钥|令牌)\s*[:：=]\s*\S+"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class MemoryCandidateDetector:
    """Detect candidates without ever persisting them by itself."""

    _EXPLICIT_PATTERNS = (
        re.compile(r"^\s*(?:请)?(?:帮我)?(?:记住|记下|记忆|保存为记忆)\s*[:：]?\s*(?P<body>.+)$", re.S),
        re.compile(
            r"^\s*(?:请)?(?:帮我)?(?:把|将)\s*(?P<body>.+?)\s*"
            r"(?:存入|写入|记录到|保存到).{0,12}(?:记忆|memory)\s*$",
            re.I | re.S,
        ),
        re.compile(r"^\s*remember\s+(?P<body>.+)$", re.I | re.S),
    )
    _REFERENCE_PATTERN = re.compile(
        r"^\s*(?:请)?(?:帮我)?(?:把|将)?(?:这|上一|上面|上述)(?:一)?条(?:内容|信息|回复)?"
        r"(?:帮我)?(?:存入|写入|记录到|保存到).{0,16}(?:记忆|memory)\s*$",
        re.I | re.S,
    )

    _AUTO_SIGNALS: tuple[tuple[re.Pattern[str], str, MemoryKind], ...] = (
        (re.compile(r"(?:我(?:更)?喜欢|我偏好|我的习惯|prefer|preference)", re.I), "稳定的用户偏好", MemoryKind.PREFERENCE),
        (re.compile(r"(?:以后|今后|始终|一律|永远).{0,24}(?:要|不要|使用|采用|保持)", re.I), "可复用的长期约定", MemoryKind.CONSTRAINT),
        (re.compile(r"(?:项目|仓库|代码库).{0,24}(?:使用|采用|基于|要求|禁止)", re.I), "项目级事实或约束", MemoryKind.FACT),
        (re.compile(r"(?:我们决定|已经决定|架构决策|decision)", re.I), "可复用的项目决策", MemoryKind.DECISION),
        (re.compile(r"(?:下次|以后).{0,24}(?:避免|先|记得)|(?:踩坑|根因是|教训)", re.I), "可能避免重复错误的经验", MemoryKind.LESSON),
    )

    def parse_explicit(self, text: str) -> MemoryProposal | None:
        """Parse an unambiguous user-authorized memory command."""
        for pattern in self._EXPLICIT_PATTERNS:
            match = pattern.match(text)
            if not match:
                continue
            body = self._clean_body(match.group("body"))
            body = self._remove_scope_words(body)
            if not body:
                return None
            return MemoryProposal(
                content=body,
                reason="用户明确要求持久化",
                scope=self.detect_scope(text),
                kind=self.detect_kind(body),
                confidence=1.0,
                explicit=True,
            )
        return None

    def propose(self, text: str) -> MemoryProposal | None:
        """Return at most one conservative candidate; never write it."""
        if self.parse_explicit(text) is not None:
            return None
        clean = text.strip()
        if not clean or "?" in clean or "？" in clean:
            return None
        if self.contains_secret(clean) or estimate_tokens(clean) > 300:
            return None
        for pattern, reason, kind in self._AUTO_SIGNALS:
            if pattern.search(clean):
                return MemoryProposal(
                    content=clean,
                    reason=reason,
                    scope=MemoryScope.PROJECT_LOCAL,
                    kind=kind,
                    confidence=0.78,
                    explicit=False,
                )
        return None

    def parse_reference(self, text: str) -> MemoryProposal | None:
        """Recognize an explicit request that refers to the preceding turn."""
        if not self._REFERENCE_PATTERN.match(text):
            return None
        return MemoryProposal(
            content="",
            reason="用户明确要求保存上一条对话内容",
            scope=self.detect_scope(text),
            kind=MemoryKind.FACT,
            confidence=1.0,
            explicit=True,
        )

    @staticmethod
    def contains_secret(text: str) -> bool:
        return any(pattern.search(text) for pattern in _SECRET_PATTERNS)

    @staticmethod
    def detect_scope(text: str) -> MemoryScope:
        lowered = text.casefold()
        if re.search(r"(?:项目共享|团队|仓库共享|project[- ]shared)", lowered):
            return MemoryScope.PROJECT_SHARED
        if re.search(r"(?:用户全局|全局|跨项目|user global)", lowered):
            return MemoryScope.USER
        if re.search(r"(?:仅本次会话|会话记忆|session)", lowered):
            return MemoryScope.SESSION
        return MemoryScope.PROJECT_LOCAL

    @staticmethod
    def detect_kind(text: str) -> MemoryKind:
        if re.search(r"(?:偏好|喜欢|习惯|prefer)", text, re.I):
            return MemoryKind.PREFERENCE
        if re.search(r"(?:决定|决策|decision)", text, re.I):
            return MemoryKind.DECISION
        if re.search(r"(?:必须|禁止|不要|约定|constraint)", text, re.I):
            return MemoryKind.CONSTRAINT
        if re.search(r"(?:教训|踩坑|根因|避免|lesson)", text, re.I):
            return MemoryKind.LESSON
        return MemoryKind.FACT

    @staticmethod
    def _clean_body(body: str) -> str:
        value = body.strip().rstrip("。.!！")
        pairs = (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"), ("`", "`"))
        for left, right in pairs:
            if len(value) >= 2 and value.startswith(left) and value.endswith(right):
                return value[len(left):-len(right)].strip()
        return value

    @staticmethod
    def _remove_scope_words(body: str) -> str:
        value = re.sub(
            r"(?:到|进|为)?(?:我的)?(?:项目本地|项目共享|团队|用户全局|全局|会话)(?:的)?(?:记忆)?\s*$",
            "",
            body,
        ).strip()
        return MemoryCandidateDetector._clean_body(value)
