"""Deterministic execution boundaries derived from the user's request.

The policy is intentionally separate from intent detection.  ``write_code``
describes what the user wants produced; it does not grant permission to write
files or execute commands.  Explicit user constraints always win.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)


class ExecutionLevel(IntEnum):
    """Maximum side-effect level authorized by the current request."""

    ANSWER_ONLY = 0
    READ_ONLY = 1
    WRITE = 2
    EXECUTE = 3


_EXECUTION_BOUNDARY_MARKER = "## 本轮执行边界（最高优先级）"


def execution_boundary_text(level: ExecutionLevel | int) -> str:
    """Render one deterministic, turn-local authorization boundary."""
    boundary = {
        int(ExecutionLevel.ANSWER_ONLY): (
            "本轮未授权任何工具调用，请直接在回答中完成；"
            "若完成任务确实需要读取、写入或执行外部内容，"
            "请先向用户说明原因并询问是否授权，不要仅以权限为由放弃。"
        ),
        int(
            ExecutionLevel.READ_ONLY
        ): (
            "本轮只允许只读工具，禁止写文件、修改状态或执行命令；"
            "若完成任务必须写入或运行命令，请先向用户说明原因并询问是否授权，"
            "不要仅以权限为由放弃。"
        ),
        int(
            ExecutionLevel.WRITE
        ): (
            "本轮允许读取和写入，但禁止 command、动态工具及任何命令执行；"
            "如需运行命令，请先向用户说明原因并询问是否授权。"
        ),
        int(ExecutionLevel.EXECUTE): "本轮已授权按正常权限闸门使用执行类工具。",
    }.get(int(level), "")
    if not boundary:
        return ""
    return (
        f"{_EXECUTION_BOUNDARY_MARKER}\n{boundary}"
        "该边界优先于其他提示；需要越界时先询问用户。"
    )


def bind_execution_boundary(text: str, level: ExecutionLevel | int) -> str:
    """Append the boundary once so it becomes part of immutable turn history."""
    if _EXECUTION_BOUNDARY_MARKER in text:
        return text
    boundary = execution_boundary_text(level)
    return f"{text}\n\n{boundary}" if boundary else text


def strip_execution_boundary(text: str) -> str:
    """Remove Xenon's own suffix before classifying the user's tool intent."""
    return text.split(_EXECUTION_BOUNDARY_MARKER, 1)[0].rstrip()


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
    r"(?:只|仅)?(?:在)?(?:对话|聊天)(?:框|区域|中|里)?(?:内)?(?:直接)?"
    r"(?:输出|展示|给出|回答|回复)"
    r"|(?:输出|展示|给出)(?:到|在|至)?(?:当前)?(?:对话|聊天)(?:框|区域|中|里)?"
    r"|(?:output|show|return|respond)(?:\s+it)?\s+(?:only\s+)?(?:in|to)\s+"
    r"(?:the\s+)?(?:chat|conversation)"
    r"|\b(?:chat|conversation)\s+only\b",
    re.IGNORECASE,
)
# 通用禁令词。裸「别」前不允许直接跟汉字，避免把「特别是修改方案」读成
# 禁令；「先别/我别/可别/千万别」是真实口语，单独放行。
_NEG_WORDS = (
    r"(?:不要|不用|不需要|无需|请勿|切勿|严禁|禁止|不准|不许|不得|不可|勿|"
    r"先别|我别|可别|千万别|(?<![\u4e00-\u9fff])别)"
)
# 变更类动词。裸「改」必须携带目标（改代码/改文件/改任何…），否则
# 「改变/改善/改为」等词会被误伤。
_MUTATION_VERBS = (
    r"(?:修改|编辑|改动|变更|替换|删除|移除|覆盖|重写|重构|触碰|碰|乱改"
    r"|动(?:这个|该|当前|任何|所有|它|其|代码|文件|脚本|模块|配置|项目|仓库|"
    r"README|readme)"
    r"|改(?:代码|文件|脚本|模块|配置|项目|仓库|README|readme|这个|该|当前|"
    r"任何|所有|它|其))"
)
_NO_WRITE = re.compile(
    "|".join(
        [
            r"(?:不要|不用|别|勿|无需|不需要|禁止|不)(?:再)?"
            r"(?:写入|保存|创建|新建|落盘)(?:任何)?(?:到)?(?:文件|磁盘)?",
            r"(?:不要|不用|别|勿|无需|不)(?:再)?(?:写|存|建)(?:入|到)?(?:任何)?文件",
            _NEG_WORDS + r"(?:再)?[^，。！？,.!?\n]{0,4}?" + _MUTATION_VERBS,
            r"(?:do\s+not|don't|without|must\s+not|may\s+not|not\s+allowed\s+to)\s+"
            r"(?:write|save|create|modify|edit|delete|change|touch|update)"
            r"(?:\s+(?:any|a|the))?\s+files?",
            r"\bno\s+file\s+(?:write|changes?)\b",
        ]
    ),
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
    r"|(?:写|编写)(?:一个|个)?\s*(?:[\w.-]+\.[A-Za-z0-9]+\s*)?(?:文件|目录|文件夹)"
    r"|(?:创建|新建|生成|修改|编辑|替换|删除).{0,24}(?:文件|目录|文件夹|项目|仓库|代码库|\w+\.[A-Za-z0-9]+)"
    r"|(?:\w+\.[A-Za-z0-9]+).{0,16}(?:修改|编辑|替换|删除|改一下|改下)"
    # 语义模式保留在降级路径（无可用 provider / 分类失败时）：显式征询
    # （问原因/思路/建议）由 _ADVISORY 在下方否决，不会变成施工。
    r"|(?:修复|重构|改造|升级|处理).{0,20}(?:bug|错误|问题|代码|项目|仓库|功能)"
    r"|(?:修复|纠正|更正|重构|改进|优化|更新|修改|调整)(?:一下|下)?"
    r"(?:这|该|当前|上述|刚才|本次|下面)?(?:个|份|段|处)?"
    r"(?:模块|函数|方法|类|实现|逻辑|注释|文档|配置|脚本|算法|接口|测试|错误)"
    r"|(?:删除|移除|去掉|清理).{0,16}(?:代码|注释|日志|文件|目录|依赖|引用|导入|import)"
    # 动宾倒装：「把重复代码重构成函数」「把 X 改成 Y」。
    r"|(?:把|将).{1,24}(?:重构|改写|修改|调整|改|变)(?:成|为|到|一下|下)"
    r"|(?:write|save|create|edit|modify|patch|replace|delete).{0,30}\b(?:file|directory|project|repo|disk)\b"
    r"|(?:fix(?:ing|es|ed)?|repair(?:ing|s|ed)?|correct(?:ing|s|ed)?|implement(?:ing|s|ed)?|"
    r"update(?:ing|s|ed)?|remov(?:e|ing|ed|es)|refactor(?:ing|s|ed)?|improve(?:ing|s|d)?|"
    r"add(?:ing|s|ed)?|rename(?:ing|s|d)?|mov(?:e|ing|ed|es)|copy(?:ing|ies|ied)?)"
    r".{0,40}\b(?:bug|bugfix|issue|error|defect|problem|function|method|class|test|testcase|"
    r"task|workflow|feature|change|behavior|behaviour)\b"
    r"|(?:a|the|this|that|minimal|correct|proper|fix|repair|implementation)?\s*"
    r"(?:correct|proper|minimal|small|full|complete)?\s*(?:fix|repair|implementation|"
    r"patch)\b"
    r"|(?:fix(?:ing|es|ed)?|repair(?:ing|s|ed)?|correct(?:ing|s|ed)?|implement(?:ing|s|ed)?|"
    r"update(?:ing|s|ed)?|remov(?:e|ing|ed|es)|refactor(?:ing|s|ed)?|improve(?:ing|s|d)?)"
    r".{0,40}\b(?:file|directory|project|repo|codebase)\b"
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
    # “读”是中文口语中最常见的文件读取请求。使用“短对象 + 外部资源类型”
    # 的语言结构，而不是枚举简历/论文等业务领域；同时排除“读懂”。
    r"|(?:读|阅读)(?!懂)(?:一下|下)?[^，。！？,.!?\n]{0,12}"
    r"(?:工具|文件|目录|内容|代码|项目|仓库|文档|资料)"
    r"|(?:分析|审查).{0,16}(?:文件|目录|项目|仓库|代码库)"
    r"|(?:read|view|inspect|search|find|list|count|check|grep)\b"
    r"|(?:show|open).{0,24}(?:content|file|directory|\w+\.[A-Za-z0-9]+)"
    r"|(?:review|analy[sz]e).{0,16}(?:file|directory|project|repo|codebase)"
    r"|(?:抓取|下载|访问).{0,16}(?:网页|页面|网址|URL)"
    r"|(?:fetch|download|scrape|crawl).{0,20}(?:web|page|url)"
    r"|https?://|github\.com/",
    re.IGNORECASE,
)

# 显式但此前漏判的写入结构：动词 + 方向补语 + 目标（写到/输出到/记录到/
# 保存为…）、在某个文件里加/补内容、更新 README/说明、以及英文 to/into/as。
# 这些句式在真实对话里比「写入 X 文件」常见得多，旧词表全部漏判为只读/闲聊。
_WRITE_TARGETED = re.compile(
    # 中文：动词 +（量词/内容）+ 到/至/进/为 + 目标
    r"(?:写|写入|保存|记录|输出|导出|生成|整理|汇总|追加|另存)"
    r"(?:一份|一个|个|些|一段|成|一下)?"
    r"[^，。！？,.!?\n]{0,14}?"
    r"(?:到|至|进|为)\s*"
    r"(?:[`'\"]?[\w./\\~:+-]+\.[A-Za-z0-9]{1,8}|文件|目录|文件夹|磁盘|磁盘上|"
    r"本地|路径|README|readme)"
    # 中文：生成/写… + 存/保存/归档 + 补语（目标由上下文承载）
    r"|(?:生成|写|整理|汇总|导出|记录|输出)[^，。！？,.!?\n]{0,14}?"
    r"(?:存|保存|放|落|归档)(?:起来|下来|到|进|成)"
    # 中文：在 <文件/模块/…> 里加/补/插入
    r"|(?:在|往|向|给)[^，。！？,.!?\n]{0,16}?"
    r"(?:文件|脚本|模块|配置|代码|项目|仓库|README|readme|[\w.-]+\.\w{1,8})"
    r"[^，。！？,.!?\n]{0,10}?(?:加|添加|补上|补|新增|插入|追加)"
    # 中文：加/补/插入 … 到/进 <文件/路径>
    r"|(?:加|添加|补上|补|新增|插入|追加)[^，。！？,.!?\n]{0,12}?"
    r"(?:到|进|在)[^，。！？,.!?\n]{0,12}?"
    r"(?:文件|脚本|模块|配置|README|readme|[\w.-]+\.\w{1,8})"
    # 中文：更新/修改/改 + README/说明/配置
    r"|(?:更新|修改|改|编辑|修订|补充|完善)(?:一下|下)?\s*"
    r"(?:README|readme|read\s*me|说明文档|安装说明|使用说明|配置(?:文件)?)"
    # English: write/save/output/append … to/into/as <file-ish>
    r"|(?:write|save|output|log|export|append|dump)\s+(?:\w+\s+){0,3}?"
    r"(?:to|into|as)\s+"
    r"(?:[`'\"]?(?:\S*[/\\]\S+|\S+\.\w{1,8})|the\s+file|a\s+file|file)\b",
    re.IGNORECASE,
)


# ── 隐含意图（显式动词缺失时的兜底） ──────────────────────
# 上面的 _WRITE / _READ_ONLY 要求用户说出「写入/保存/修改」这类显式动词。
# 但真实请求里目标往往由句式承载而非动词：「我需要一个 config.yaml」
# 「帮我把这段存起来」表达的是写盘意图，却一个显式写入动词都没有，此前
# 被判成 ANSWER_ONLY——LLM 拿不到写工具，只能把内容贴在对话里，用户看到的
# 就是「明明让他写文件，他却只是聊天」。
#
# 这里刻意不枚举业务领域，只描述语言结构：需求句式（需要/想要/给我）+
# 文件实体（扩展名/路径/目录），或处置句式（把…存/放/整理）。
_IMPLICIT_WRITE = re.compile(
    # 需求句式 + 具名文件：「我需要一个 config.yaml」「给我一份 README.md」
    # 三条边界，缺一条就会误判：
    #  1. 裸的「要/想」要排除前置否定，否则「不要写文件」被读成授权；
    #  2. 需求动词后不能紧跟生成动词——「想写一个脚本」要的是代码本身，
    #     不是磁盘上的文件，属于 write_code 的仅回答语义；
    #  3. 实体必须是**带扩展名的具名文件**。裸的「脚本/模块/配置」在
    #     「写个脚本给我看看」里指的是内容，不是落盘目标。
    r"(?<![不别勿])(?:需要|想要|想|要|给我|来一个|来个|搞|弄)"
    r"(?!\s*(?:写|编写|生成|做|实现|设计))(?:一个|一份|个|份)?"
    r"[^，。！？,.!?\n]{0,16}"
    r"(?:\w+\.[A-Za-z0-9]{1,6}\b|文件|文件夹|目录)"
    # 处置句式：「把这段存起来」「将结果保存下来」（存/放/整理 + 趋向补语）
    r"|(?:把|将)[^，。！？,.!?\n]{0,24}(?:存|放|落|整理|归档|导出)"
    r"(?:起来|下来|到|进|成|好)"
    # 口语完成动词 + 缺陷实体：「搞定这个 bug」「处理下这个报错」
    r"|(?:搞定|解决|处理|收拾|干掉|消掉)(?:一下|下)?"
    r"[^，。！？,.!?\n]{0,12}"
    r"(?:bug|BUG|错误|报错|异常|问题|崩溃|失败|警告|warning)"
    # 英文需求句式
    r"|(?:need|want|give\s+me|make\s+me|create\s+me)\s+"
    r"(?:a|an|the|one)?\s*[^,.!?\n]{0,16}"
    r"(?:\w+\.[A-Za-z0-9]+|file|script|config|directory|folder|module)",
    re.IGNORECASE,
)
_IMPLICIT_READ = re.compile(
    # 认知动词 + 外部实体：「看看这个项目」「了解下这份代码」
    r"(?:看看|看下|瞧瞧|了解|熟悉|摸清|梳理|捋一下|过一遍)(?:一下|下)?"
    r"[^，。！？,.!?\n]{0,12}"
    r"(?:文件|目录|代码|项目|仓库|实现|结构|逻辑|配置|文档|日志)"
    # 疑问句式指向代码实体：「这个函数是干什么的」「哪里定义的」
    r"|(?:是(?:干|做)什么|干嘛用|有什么用|在哪(?:里)?|哪里)"
    r"(?:的|之)?(?:定义|实现|调用|声明)?"
    # 英文认知动词
    r"|(?:take\s+a\s+look|walk\s+through|go\s+over|figure\s+out)\b",
    re.IGNORECASE,
)

_REQUEST_CUE = re.compile(
    r"(?:请(?:你)?|请帮我|帮(?:我)?|麻烦(?:你)?|劳烦(?:你)?|能否|"
    r"可否|可以(?:请你|帮我))\s*",
    re.IGNORECASE,
)
_DIRECT_BARE_GIT_REQUEST = re.compile(
    r"(?:请(?:你)?|请帮我|帮(?:我)?|麻烦(?:你)?|现在|立即|直接|开始|继续)"
    r"\s*(?:提交|推送|合并)(?:一下|吧)?(?:[，。！？,.!?]|$)",
    re.IGNORECASE,
)
_PATH_REFERENCE = re.compile(
    r"(?:^|\s)(?:\./|\.\./|src/|tests?/|lib/|app/|[/~])\S+"
    r"|(?:^|\s)[A-Za-z]:[\\/]\S+"
    r"|\b\w+\.(?:py|js|ts|jsx|tsx|java|c|cpp|h|go|rs|rb|php|html|css|json|"
    r"yaml|yml|toml|xml|md|txt|pdf|tex|docx?|rtf|csv|xlsx?|xlsm|et|pptx?|epub|"
    r"log|sh|bat|ps1)\b",
    re.IGNORECASE,
)
_URL_REFERENCE = re.compile(r"https?://|github\.com/", re.IGNORECASE)
# 征询解释语义：用户要的是原因/思路/建议（提问式），而不是施工。
# 只收录疑问/征询形式，避免「修复它并告诉我原因」这类命令式误伤。
_ADVISORY = re.compile(
    r"(?:思路|有什么建议|有什么看法|什么建议|为什么呢?|原因是什么|告诉我原因|"
    r"怎么解决|如何解决|工作原理|是什么原理|做什么用的|怎么回事|哪个好)"
)


def _split_request_clause(source: str) -> str:
    """Authorize side effects from the final explicit request clause."""

    cues = list(_REQUEST_CUE.finditer(source))
    if cues:
        return source[cues[-1].end() :].strip() or source
    return source


def _snippets(
    pattern: re.Pattern[str],
    text: str,
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    """Return deduplicated matched snippets (bounded, for prompts/logs)."""

    out: list[str] = []
    for match in pattern.finditer(text):
        snippet = match.group(0).strip()
        if snippet and snippet not in out:
            out.append(snippet)
        if len(out) >= limit:
            break
    return tuple(out)


@dataclass(frozen=True)
class ExecutionSignals:
    """Deterministic signals extracted from one user turn.

    This is the regex layer and it **never decides the final level**.  It
    reports primitives (write/execute structures), constraints (negations,
    chat-only) and evidence snippets.  ``classify_execution_policy`` and the
    LLM merge layer both consume this object, so regexes live in one place
    and cannot drift apart across layers.
    """

    write_patterns: tuple[str, ...] = ()
    write_snippets: tuple[str, ...] = ()
    execute_snippets: tuple[str, ...] = ()
    read_snippets: tuple[str, ...] = ()
    negation_snippets: tuple[str, ...] = ()
    no_write: bool = False
    no_execute: bool = False
    no_tools: bool = False
    chat_only: bool = False
    advisory: bool = False
    paths: tuple[str, ...] = ()

    @property
    def explicit_write(self) -> bool:
        return bool(self.write_patterns)

    @property
    def strong_write(self) -> bool:
        """只有强结构（动词+目标、路径、git 操作）能压过征询语气。

        ``write_verb``/``implicit_write`` 里的「修复/处理/优化」属于语义，
        在用户问原因/思路时不应算作施工授权。
        """
        if "write_targeted" in self.write_patterns or "git_request" in self.write_patterns:
            return True
        return self.explicit_write and bool(self.paths)

    @property
    def explicit_execute(self) -> bool:
        return bool(self.execute_snippets)

    @property
    def read_evidence(self) -> bool:
        return bool(self.read_snippets) or bool(self.paths)


def extract_execution_signals(text: str) -> ExecutionSignals:
    """Extract primitives/constraints/evidence from *text* (no final decision)."""

    source = text.strip()
    request_source = _split_request_clause(source)

    write_patterns: list[str] = []
    write_snippets: list[str] = []
    for label, pattern, haystack in (
        ("write_verb", _WRITE, request_source),
        ("write_targeted", _WRITE_TARGETED, request_source),
        ("git_request", _DIRECT_BARE_GIT_REQUEST, source),
        ("implicit_write", _IMPLICIT_WRITE, request_source),
    ):
        hits = _snippets(pattern, haystack, limit=1)
        if hits:
            write_patterns.append(label)
            write_snippets.extend(hits)

    negation_snippets: list[str] = []
    for pattern in (_NO_WRITE, _NO_EXECUTE, _NO_TOOLS, _CHAT_OUTPUT):
        negation_snippets.extend(_snippets(pattern, source, limit=1))

    read_snippets: list[str] = []
    for pattern in (_READ_ONLY, _IMPLICIT_READ):
        read_snippets.extend(_snippets(pattern, request_source, limit=1))
    for pattern in (_PATH_REFERENCE, _URL_REFERENCE):
        read_snippets.extend(_snippets(pattern, source, limit=2))

    return ExecutionSignals(
        write_patterns=tuple(write_patterns),
        write_snippets=tuple(dict.fromkeys(write_snippets)),
        execute_snippets=_snippets(_EXECUTE, request_source, limit=2),
        read_snippets=tuple(dict.fromkeys(read_snippets))[:5],
        negation_snippets=tuple(dict.fromkeys(negation_snippets))[:5],
        no_write=bool(_NO_WRITE.search(source)),
        no_execute=bool(_NO_EXECUTE.search(source)),
        no_tools=bool(_NO_TOOLS.search(source)),
        chat_only=bool(_CHAT_OUTPUT.search(source)),
        advisory=bool(_ADVISORY.search(source)),
        paths=tuple(
            dict.fromkeys(
                match.group(0).strip()
                for match in _PATH_REFERENCE.finditer(source)
                if match.group(0).strip()
            )
        )[:5],
    )


def _decision(
    level: ExecutionLevel,
    reason: str,
    *,
    explicit_no_write: bool = False,
    explicit_no_execute: bool = False,
    evidence: tuple[str, ...] = (),
) -> ExecutionPolicy:
    """Build the per-turn policy and emit the decision chain to the log."""

    policy = ExecutionPolicy(
        level,
        reason,
        explicit_no_write=explicit_no_write,
        explicit_no_execute=explicit_no_execute,
    )
    logger.debug(
        "execution_policy level=%s reason=%s no_write=%s no_execute=%s evidence=%s",
        level.name,
        reason,
        explicit_no_write,
        explicit_no_execute,
        ",".join(evidence) or "-",
    )
    return policy


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
    signals = extract_execution_signals(text)
    no_tools = signals.no_tools
    chat_output = signals.chat_only
    no_write = signals.no_write
    no_execute = signals.no_execute

    if no_tools:
        return _decision(
            ExecutionLevel.ANSWER_ONLY,
            "用户明确要求不使用工具",
            explicit_no_write=True,
            explicit_no_execute=True,
        )

    # An explicit chat destination is a hard boundary.  It must be evaluated
    # before broad action verbs such as "write" or "run".
    if chat_output or (no_write and no_execute):
        return _decision(
            ExecutionLevel.ANSWER_ONLY,
            "用户明确要求只在对话中回答",
            explicit_no_write=True,
            explicit_no_execute=True,
        )

    wants_execute = signals.explicit_execute and not no_execute
    # 显式写入动词优先；缺失时再看结构化的写入句式（写到/输出到/在…里加）
    # 与隐含写盘句式（需求/处置/口语修复），否则「我需要一个 config.yaml」
    # 这类请求会掉到 ANSWER_ONLY。显式禁令拥有最终否决权。
    wants_write = signals.explicit_write and not no_write
    # 征询解释（问原因/思路/建议）不是施工：没有显式写入/执行结构时，
    # 不允许仅凭语境动词（修复/处理/优化）拿到写或执行权限。
    if signals.advisory and not (signals.strong_write or signals.explicit_execute):
        wants_write = False
        wants_execute = False
    # Keep path/URL evidence from the complete user turn.  They are frequently
    # placed before “请你分析/学习…”, while request_source intentionally starts
    # after the last polite request cue.  Looking only at request_source used
    # to discard `/media/.../resume.tex` and GitHub repository URLs, routing
    # those turns to direct mode without a read-only tools schema.
    wants_read = signals.read_evidence

    if wants_execute:
        return _decision(
            ExecutionLevel.EXECUTE,
            "用户明确要求执行或验证",
            explicit_no_write=no_write,
            explicit_no_execute=False,
        )
    if wants_write:
        return _decision(
            ExecutionLevel.WRITE,
            "用户明确要求修改持久化内容",
            explicit_no_write=False,
            explicit_no_execute=no_execute,
            evidence=signals.write_patterns,
        )

    if intent in {"query", "research"}:
        return _decision(
            ExecutionLevel.READ_ONLY,
            "信息查询或资料调研只允许只读工具",
            explicit_no_write=no_write,
            explicit_no_execute=no_execute,
        )
    if intent == "write_code":
        return _decision(
            ExecutionLevel.ANSWER_ONLY,
            "代码生成默认仅返回对话内容；未授权写盘或执行",
            explicit_no_write=True,
            explicit_no_execute=True,
        )

    if wants_read:
        return _decision(
            ExecutionLevel.READ_ONLY,
            "用户要求读取或检查外部信息",
            explicit_no_write=no_write,
            explicit_no_execute=no_execute,
        )

    return _decision(
        ExecutionLevel.ANSWER_ONLY,
        "请求不需要外部操作",
        explicit_no_write=no_write,
        explicit_no_execute=no_execute,
    )
