"""Deterministic execution boundaries derived from the user's request.

The policy is intentionally separate from intent detection.  ``write_code``
describes what the user wants produced; it does not grant permission to write
files or execute commands.  Explicit user constraints always win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum


class ExecutionLevel(IntEnum):
    """Maximum side-effect level authorized by the current request."""

    ANSWER_ONLY = 0
    READ_ONLY = 1
    WRITE = 2
    EXECUTE = 3


@dataclass(frozen=True)
class ExecutionPolicy:
    """A small, inspectable authorization decision for one user turn."""

    level: ExecutionLevel
    reason: str
    explicit_no_write: bool = False
    explicit_no_execute: bool = False

    @property
    def requires_tools(self) -> bool:
        return self.level >= ExecutionLevel.READ_ONLY

    @property
    def allows_write(self) -> bool:
        return self.level >= ExecutionLevel.WRITE and not self.explicit_no_write

    @property
    def allows_execute(self) -> bool:
        return self.level >= ExecutionLevel.EXECUTE and not self.explicit_no_execute

    @property
    def locks_answer_only(self) -> bool:
        """Whether automatic routing is forbidden from escalating this turn."""

        return self.level is ExecutionLevel.ANSWER_ONLY and (
            self.explicit_no_write or self.explicit_no_execute
        )


_NO_TOOLS = re.compile(
    r"(?:不要|无需|不需要|禁止)(?:使用|调用)?(?:任何)?(?:工具|tool)"
    r"|(?:do\s+not|don't|without)\s+(?:use|using|call(?:ing)?)\s+(?:any\s+)?tools?"
    r"|\bno\s+tools?\b",
    re.IGNORECASE,
)
_CHAT_OUTPUT = re.compile(
    r"(?:只|仅)?(?:在)?(?:对话|聊天)(?:框|区域|中|里)?(?:内)?(?:直接)?(?:输出|展示|给出)"
    r"|(?:输出|展示|给出)(?:到|在|至)?(?:当前)?(?:对话|聊天)(?:框|区域|中|里)?"
    r"|(?:output|show|return|respond)(?:\s+it)?\s+(?:only\s+)?(?:in|to)\s+"
    r"(?:the\s+)?(?:chat|conversation)"
    r"|\b(?:chat|conversation)\s+only\b",
    re.IGNORECASE,
)
_NO_WRITE = re.compile(
    r"(?:不要|无需|不需要|禁止|不)(?:再)?(?:写入|保存|创建|新建|落盘)(?:任何)?(?:到)?(?:文件|磁盘)?"
    r"|(?:不|无需)(?:写|存)(?:入|到)?(?:任何)?文件"
    r"|(?:do\s+not|don't|without)\s+(?:write|save|create|modify)(?:\s+(?:any|a|the))?\s+files?"
    r"|\bno\s+file\s+(?:write|changes?)\b",
    re.IGNORECASE,
)
_NO_EXECUTE = re.compile(
    r"(?:不要|无需|不需要|禁止|不)(?:执行|运行|跑|测试)(?:任何)?(?:命令|脚本|程序|代码|测试)?"
    r"|(?:do\s+not|don't|without)\s+(?:run|execute|test)(?:ing)?\b"
    r"|\bno\s+(?:execution|commands?|tests?)\b",
    re.IGNORECASE,
)

_EXECUTE = re.compile(
    r"(?:执行|运行|跑一下|跑下|测试|验证)(?:这|该|一下|下|看看|脚本|程序|代码|命令|测试|pytest|python|npm|pnpm|yarn)?"
    r"|(?:run|execute|test|verify)(?:\s+it|\s+this|\s+the|\s+pytest|\s+python|\s+npm|\b)"
    r"|\b(?:pytest|npm\s+test|pnpm\s+test|cargo\s+test|go\s+test)\b",
    re.IGNORECASE,
)
_WRITE = re.compile(
    r"(?:写入|保存|落盘).{0,12}(?:文件|目录|磁盘|路径|[/~.]|[A-Za-z]:\\)"
    r"|(?:创建|新建|生成|修改|编辑|替换|删除).{0,24}(?:文件|目录|文件夹|项目|仓库|代码库|\w+\.[A-Za-z0-9]+)"
    r"|(?:\w+\.[A-Za-z0-9]+).{0,16}(?:修改|编辑|替换|删除|改一下|改下)"
    r"|(?:修复|重构|改造|升级|处理).{0,20}(?:bug|错误|问题|代码|项目|仓库|功能)"
    r"|(?:write|save|create|edit|modify|patch|replace|delete).{0,30}\b(?:file|directory|project|repo|disk)\b"
    r"|(?:写|保存|生成)(?:到|至|进)\s*(?:[/~.]|[A-Za-z]:\\)"
    r"|(?:提交|推送|合并)(?:这|该|当前|上述|刚才|本次)?(?:份|个|些)?"
    r"(?:代码|更改|修改|变更|commit|PR|分支|标签|版本)"
    r"|(?:提交|推送|合并)(?:到|至)\s*(?:GitHub|GitLab|Gitee|origin|远程仓库)"
    r"|\bgit\s+(?:add|commit|push|merge|rebase|checkout)\b",
    re.IGNORECASE,
)
_READ_ONLY = re.compile(
    r"(?:读取|查看|打开|检查|搜索|查询|查找|查一下|调研|调查|了解|研究|"
    r"比较|对比|列出|统计)(?:一下|下|这个|该|当前|文件|目录|内容|代码|项目|仓库)?"
    r"|(?:分析|审查).{0,16}(?:文件|目录|项目|仓库|代码库)"
    r"|(?:read|view|inspect|search|find|list|count|check|grep)\b"
    r"|(?:show|open).{0,24}(?:content|file|directory|\w+\.[A-Za-z0-9]+)"
    r"|(?:review|analy[sz]e).{0,16}(?:file|directory|project|repo|codebase)"
    r"|(?:抓取|下载|访问).{0,16}(?:网页|页面|网址|URL)"
    r"|(?:fetch|download|scrape|crawl).{0,20}(?:web|page|url)"
    r"|https?://|github\.com/",
    re.IGNORECASE,
)

_REQUEST_CUE = re.compile(
    r"(?:请(?:你)?|请帮我|帮(?:我)?|麻烦(?:你)?|劳烦(?:你)?|能否|"
    r"可否|可以(?:请)?(?:你)?)\s*",
    re.IGNORECASE,
)
_DIRECT_BARE_GIT_REQUEST = re.compile(
    r"(?:请(?:你)?|请帮我|帮(?:我)?|麻烦(?:你)?|现在|立即|直接|开始|继续)"
    r"\s*(?:提交|推送|合并)(?:一下|吧)?(?:[，。！？,.!?]|$)",
    re.IGNORECASE,
)
_PATH_REFERENCE = re.compile(
    r"(?:^|\s)(?:\./|\.\./|src/|tests?/|lib/|app/|[/~])\S+"
    r"|(?:^|\s)[A-Za-z]:\\\S+"
    r"|\b\w+\.(?:py|js|ts|jsx|tsx|java|c|cpp|h|go|rs|rb|php|html|css|json|yaml|yml|toml|xml|md|txt|sh|bat|ps1)\b",
    re.IGNORECASE,
)


def classify_execution_policy(
    text: str,
    *,
    intent: str | None = None,
) -> ExecutionPolicy:
    """Classify the maximum authorized action for a single request.

    Code generation defaults to an answer in chat.  Writing or execution only
    becomes authorized when the user asks for that side effect explicitly.
    """

    source = text.strip()
    no_tools = bool(_NO_TOOLS.search(source))
    chat_output = bool(_CHAT_OUTPUT.search(source))
    no_write = bool(_NO_WRITE.search(source))
    no_execute = bool(_NO_EXECUTE.search(source))

    if no_tools:
        return ExecutionPolicy(
            ExecutionLevel.ANSWER_ONLY,
            "用户明确要求不使用工具",
            explicit_no_write=True,
            explicit_no_execute=True,
        )

    # An explicit chat destination is a hard boundary.  It must be evaluated
    # before broad action verbs such as "write" or "run".
    if chat_output or (no_write and no_execute):
        return ExecutionPolicy(
            ExecutionLevel.ANSWER_ONLY,
            "用户明确要求只在对话中回答",
            explicit_no_write=True,
            explicit_no_execute=True,
        )

    # Long conversational prompts often state background plans before the
    # actual request: "我打算提交到某平台，请你查一下……".  Authorize side
    # effects from the final explicit request clause, while constraints above
    # still apply to the complete original turn.
    request_source = source
    cues = list(_REQUEST_CUE.finditer(source))
    if cues:
        request_source = source[cues[-1].end():].strip() or source

    wants_execute = bool(_EXECUTE.search(request_source)) and not no_execute
    wants_write = bool(
        _WRITE.search(request_source) or _DIRECT_BARE_GIT_REQUEST.search(source)
    ) and not no_write
    wants_read = bool(
        _READ_ONLY.search(request_source) or _PATH_REFERENCE.search(request_source)
    )

    if wants_execute:
        return ExecutionPolicy(
            ExecutionLevel.EXECUTE,
            "用户明确要求执行或验证",
            explicit_no_write=no_write,
            explicit_no_execute=False,
        )
    if wants_write:
        return ExecutionPolicy(
            ExecutionLevel.WRITE,
            "用户明确要求修改持久化内容",
            explicit_no_write=False,
            explicit_no_execute=no_execute,
        )

    if intent in {"query", "research"}:
        return ExecutionPolicy(
            ExecutionLevel.READ_ONLY,
            "信息查询或资料调研只允许只读工具",
            explicit_no_write=no_write,
            explicit_no_execute=no_execute,
        )
    if intent == "write_code":
        return ExecutionPolicy(
            ExecutionLevel.ANSWER_ONLY,
            "代码生成默认仅返回对话内容；未授权写盘或执行",
            explicit_no_write=True,
            explicit_no_execute=True,
        )

    if wants_read:
        return ExecutionPolicy(
            ExecutionLevel.READ_ONLY,
            "用户要求读取或检查外部信息",
            explicit_no_write=no_write,
            explicit_no_execute=no_execute,
        )

    return ExecutionPolicy(
        ExecutionLevel.ANSWER_ONLY,
        "请求不需要外部操作",
        explicit_no_write=no_write,
        explicit_no_execute=no_execute,
    )
