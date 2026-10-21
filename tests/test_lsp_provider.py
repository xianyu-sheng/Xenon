"""lsp_provider.py 覆盖率测试（#148）。

当前实现是基于 Jedi 的进程内静态分析（不是 JSON-RPC/stdio 语言服务器），
因此不引入 fake LSP server：直接构造临时文件、走真实 Jedi 分析，再用
monkeypatch 的假 Script 驱动异常/边界分支（读取失败、jedi 抛错、空结果、
语法错误对象等）。

测试过程中确认的三处实现事实（不在本 PR 修复，findings 见 PR 描述）：
1. ``get_diagnostics`` 对真实语法错误静默返回空列表——jedi>=0.19 的
   ``SyntaxError`` 没有 ``.message`` 属性（只有 ``get_message()``），
   ``AttributeError`` 被外层 ``except`` 吞掉；
2. ``find_references`` 的 ``in_same_file`` 恒为 ``False``——
   ``ref.module_path`` 是 ``pathlib.Path``，与 ``str(path)`` 比较永不相等；
3. 符号的 ``type == "import"`` 跳过分支对当前 jedi 不可达——import 名会被
   解析成 ``function``/``module`` 等真实类型，``include_imports=False``
   实际不过滤任何符号。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import xenon.utils.lsp_provider as lsp_provider
from xenon.utils.lsp_provider import LSPProvider

_GREET_SOURCE = (
    'def greet(name: str, times: int = 1) -> str:\n'
    '    """Say hi."""\n'
    "    return name * times\n"
    "\n"
    "\n"
    'greet("bo", times=2)\n'
)

_METHOD_CALLS = [
    pytest.param(lambda p: LSPProvider.goto_definition(p, 1, 0), id="goto-definition"),
    pytest.param(lambda p: LSPProvider.find_references(p, 1, 0), id="find-references"),
    pytest.param(lambda p: LSPProvider.get_hover(p, 1, 0), id="get-hover"),
    pytest.param(LSPProvider.get_diagnostics, id="get-diagnostics"),
    pytest.param(LSPProvider.get_symbols, id="get-symbols"),
]


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


_PathImpl = type(Path())


class _FlakyPath(_PathImpl):
    """仅对 lib.py 的 read_text 抛错，驱动“代码片段读取失败”的兜底分支。

    基类用具体实现（PosixPath/WindowsPath）而非 pathlib.Path：3.12 之前
    直接继承 Path 会因缺少 _flavour 在 3.10/3.11 上报 AttributeError。
    """

    def read_text(self, *args, **kwargs):
        if self.name == "lib.py":
            raise OSError("simulated read failure")
        return super().read_text(*args, **kwargs)


class _RaisingScript:
    """实例化即抛错，驱动各方法共用的“分析失败”分支。"""

    def __init__(self, *_args, **_kwargs) -> None:
        raise RuntimeError("jedi exploded")


class _HoverScript:
    """签名/帮助为空、infer 抛错，驱动 get_hover 的空结果与兜底分支。"""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_signatures(self, *_args):
        return []

    def help(self, *_args):
        return []

    def infer(self, *_args):
        raise RuntimeError("no inference")


class _SyntaxErrorScript:
    """返回两条语法错误：一条带 until_* 字段，一条没有（走 getattr 兜底）。"""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_syntax_errors(self):
        return [
            SimpleNamespace(
                line=1,
                column=0,
                message="bad token",
                until_line=1,
                until_column=2,
            ),
            SimpleNamespace(line=3, column=4, message="missing paren"),
        ]


class _DiagnosticsRaisingScript:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_syntax_errors(self):
        raise RuntimeError("syntax scan failed")


class _SymbolsScript:
    """一个 type=="import" 的符号 + 一个函数，驱动 include_imports 分支。"""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_names(self, **_kwargs):
        return [
            SimpleNamespace(
                name="os",
                type="import",
                line=1,
                column=0,
                description="import os",
                docstring=lambda raw=True: "",
            ),
            SimpleNamespace(
                name="func",
                type="function",
                line=4,
                column=0,
                description="def func",
                docstring=lambda raw=True: '"""doc"""',
            ),
        ]


# ───────────────────────────── 路径与支持检查 ─────────────────────────────


def test_resolve_path_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    target = _write(tmp_path, "home.py", "x = 1\n")
    assert LSPProvider._resolve_path("~/home.py") == target.resolve()


def test_check_supported_missing_file(tmp_path):
    err = LSPProvider._check_supported(tmp_path / "nope.py")
    assert err is not None and "文件不存在" in err


def test_check_supported_rejects_non_python(tmp_path):
    note = _write(tmp_path, "note.md", "# hi\n")
    err = LSPProvider._check_supported(note)
    assert err is not None and "不支持的文件类型" in err


def test_check_supported_accepts_uppercase_suffix_and_stub(tmp_path):
    upper = _write(tmp_path, "MOD.PY", "x = 1\n")
    stub = _write(tmp_path, "mod.pyi", "x: int\n")
    assert LSPProvider._check_supported(upper) is None
    assert LSPProvider._check_supported(stub) is None


@pytest.mark.parametrize("method", _METHOD_CALLS)
def test_missing_file_returns_error(tmp_path, method):
    result = method(str(tmp_path / "nope.py"))
    assert result["success"] is False
    assert "文件不存在" in result["error"]


@pytest.mark.parametrize("method", _METHOD_CALLS)
def test_read_failure_returns_error(tmp_path, method):
    # 路径存在且后缀受支持，但 read_text 会抛 IsADirectoryError
    directory = tmp_path / "dir.py"
    directory.mkdir()
    result = method(str(directory))
    assert result["success"] is False
    assert "无法读取文件" in result["error"]


# ─────────────────────────────── goto_definition ───────────────────────────────


def test_goto_definition_same_file_returns_snippet(tmp_path):
    target = _write(
        tmp_path,
        "target.py",
        "def target():\n    return 1\n\n\ndef caller():\n    return target()\n",
    )
    result = LSPProvider.goto_definition(str(target), 6, 11)
    assert result["success"] is True and result["found"] is True
    assert result["count"] == 1
    definition = result["definitions"][0]
    assert definition["name"] == "target"
    assert definition["line"] == 1 and definition["column"] == 4
    assert str(definition["module_path"]).endswith("target.py")
    assert "def target" in definition["code_snippet"]
    # 没有真实 docstring 的函数：docstring(raw=True) 返回空串
    assert definition["docstring"] == ""


def test_goto_definition_variable_has_empty_docstring(tmp_path):
    target = _write(tmp_path, "var.py", "value = 1\n")
    result = LSPProvider.goto_definition(str(target), 1, 0)
    assert result["found"] is True
    assert result["definitions"][0]["docstring"] == ""


def test_goto_definition_not_found(tmp_path):
    target = _write(tmp_path, "undef.py", "value = undefined_name\n")
    result = LSPProvider.goto_definition(str(target), 1, 8)
    assert result["success"] is True
    assert result["found"] is False
    assert "未找到定义" in result["message"]


def test_goto_definition_follow_imports(tmp_path):
    _write(tmp_path, "lib.py", "def helper():\n    return 2\n")
    consumer = _write(
        tmp_path,
        "consumer.py",
        "from lib import helper\n\n\nhelper()\n",
    )
    followed = LSPProvider.goto_definition(str(consumer), 4, 2, follow_imports=True)
    definition = followed["definitions"][0]
    assert str(definition["module_path"]).endswith("lib.py")
    assert "def helper" in definition["code_snippet"]

    local = LSPProvider.goto_definition(str(consumer), 4, 2, follow_imports=False)
    assert str(local["definitions"][0]["module_path"]).endswith("consumer.py")


def test_goto_definition_snippet_read_failure_keeps_empty_snippet(tmp_path, monkeypatch):
    _write(tmp_path, "lib.py", "def helper():\n    return 2\n")
    consumer = _write(tmp_path, "consumer.py", "from lib import helper\n\n\nhelper()\n")
    monkeypatch.setattr(lsp_provider, "Path", _FlakyPath)
    result = LSPProvider.goto_definition(str(consumer), 4, 2)
    assert result["found"] is True
    assert str(result["definitions"][0]["module_path"]).endswith("lib.py")
    assert result["definitions"][0]["code_snippet"] == ""


def test_goto_definition_jedi_error(tmp_path, monkeypatch):
    target = _write(tmp_path, "boom.py", "x = 1\n")
    monkeypatch.setattr(lsp_provider.jedi, "Script", _RaisingScript)
    result = LSPProvider.goto_definition(str(target), 1, 0)
    assert result["success"] is False
    assert "分析失败" in result["error"]


# ─────────────────────────────── find_references ───────────────────────────────


def test_find_references_same_file_groups_by_file(tmp_path):
    target = _write(
        tmp_path,
        "refs.py",
        "def target():\n    return 1\n\n\ndef caller():\n    return target()\n",
    )
    result = LSPProvider.find_references(str(target), 1, 4)
    assert result["success"] is True and result["found"] is True
    assert result["count"] == 2
    assert sum(result["by_file"].values()) == 2
    first, second = result["references"]
    assert first["code_line"].startswith("def target")
    assert "target()" in second["code_line"]
    assert "in_same_file" in first
    assert str(first["module_path"]).endswith("refs.py")


def test_find_references_not_found(tmp_path):
    target = _write(tmp_path, "literal.py", "x = 1\n")
    result = LSPProvider.find_references(str(target), 1, 4)
    assert result["success"] is True
    assert result["found"] is False
    assert "未找到引用" in result["message"]


def test_find_references_jedi_error(tmp_path, monkeypatch):
    target = _write(tmp_path, "boom.py", "x = 1\n")
    monkeypatch.setattr(lsp_provider.jedi, "Script", _RaisingScript)
    result = LSPProvider.find_references(str(target), 1, 0)
    assert result["success"] is False
    assert "分析失败" in result["error"]


def test_find_references_code_line_read_failure_keeps_empty_line(tmp_path, monkeypatch):
    _write(tmp_path, "lib.py", "def helper():\n    return 2\n")
    consumer = _write(tmp_path, "consumer.py", "from lib import helper\n\n\nhelper()\n")
    monkeypatch.setattr(lsp_provider, "Path", _FlakyPath)
    result = LSPProvider.find_references(str(consumer), 1, 18)
    assert result["found"] is True
    lib_lines = {
        ref["code_line"]
        for ref in result["references"]
        if str(ref["module_path"]).endswith("lib.py")
    }
    assert lib_lines == {""}
    assert any(
        ref["code_line"]
        for ref in result["references"]
        if str(ref["module_path"]).endswith("consumer.py")
    )


# ──────────────────────────────── get_hover ────────────────────────────────


def test_get_hover_reports_signature_and_help(tmp_path):
    target = _write(tmp_path, "greet.py", _GREET_SOURCE)
    result = LSPProvider.get_hover(str(target), 6, 6)
    assert result["success"] is True
    signature = result["signatures"][0]
    assert signature["name"] == "greet"
    assert len(signature["params"]) == 2
    assert signature["docstring"].startswith("Say hi")
    assert result["help"]
    assert "type_info" in result


def test_get_hover_infer_failure_falls_back_to_empty(tmp_path, monkeypatch):
    target = _write(tmp_path, "greet.py", _GREET_SOURCE)
    monkeypatch.setattr(lsp_provider.jedi, "Script", _HoverScript)
    result = LSPProvider.get_hover(str(target), 1, 0)
    assert result["success"] is True
    assert result["signatures"] == []
    assert result["type_info"] == []
    assert result["help"] == ""


def test_get_hover_jedi_error(tmp_path, monkeypatch):
    target = _write(tmp_path, "boom.py", "x = 1\n")
    monkeypatch.setattr(lsp_provider.jedi, "Script", _RaisingScript)
    result = LSPProvider.get_hover(str(target), 1, 0)
    assert result["success"] is False
    assert "分析失败" in result["error"]


# ────────────────────────────── get_diagnostics ──────────────────────────────


def test_get_diagnostics_clean_file_has_no_errors(tmp_path):
    target = _write(tmp_path, "clean.py", "x = 1\ny = x + 1\n")
    result = LSPProvider.get_diagnostics(str(target))
    assert result["success"] is True
    assert result["total_lines"] == 2
    assert result["error_count"] == 0
    assert result["warning_count"] == 0
    assert result["diagnostics"] == []


def test_get_diagnostics_syntax_error_real_jedi(tmp_path):
    # 真实语法错误：当前实现静默返回空诊断（见模块 docstring 的 finding 1），
    # 这里只断言不再抛错、结构完整；append 路径由下面的假 Script 用例覆盖。
    target = _write(tmp_path, "broken.py", "def broken(:\n    pass\n")
    result = LSPProvider.get_diagnostics(str(target))
    assert result["success"] is True
    assert result["total_lines"] == 2
    assert isinstance(result["diagnostics"], list)


def test_get_diagnostics_maps_error_fields(tmp_path, monkeypatch):
    target = _write(tmp_path, "broken.py", "x = 1\n")
    monkeypatch.setattr(lsp_provider.jedi, "Script", _SyntaxErrorScript)
    result = LSPProvider.get_diagnostics(str(target))
    assert result["success"] is True
    assert result["error_count"] == 2
    assert result["diagnostics"][0]["severity"] == "Error"
    assert result["diagnostics"][0]["message"] == "bad token"
    assert result["diagnostics"][0]["until_column"] == 2
    assert result["diagnostics"][1]["until_line"] == 3
    assert result["diagnostics"][1]["until_column"] == 5


def test_get_diagnostics_jedi_error_swallowed(tmp_path, monkeypatch):
    target = _write(tmp_path, "boom.py", "x = 1\n")
    monkeypatch.setattr(lsp_provider.jedi, "Script", _DiagnosticsRaisingScript)
    result = LSPProvider.get_diagnostics(str(target))
    assert result["success"] is True
    assert result["error_count"] == 0
    assert result["diagnostics"] == []


# ─────────────────────────────── get_symbols ───────────────────────────────


def test_get_symbols_classifies_definitions(tmp_path):
    target = _write(
        tmp_path,
        "symbols.py",
        "import os\n\n\nCONST = 1\n\n\ndef func():\n    pass\n\n\nclass Klass:\n    attr = 2\n",
    )
    result = LSPProvider.get_symbols(str(target))
    assert result["success"] is True
    assert result["by_type"].get("function") == 1
    assert result["by_type"].get("class") == 1
    assert result["total_symbols"] == sum(result["by_type"].values())
    assert result["symbols"]["class"][0]["name"] == "Klass"


def test_get_symbols_include_imports_toggle(tmp_path, monkeypatch):
    target = _write(tmp_path, "symbols.py", "import os\n")
    monkeypatch.setattr(lsp_provider.jedi, "Script", _SymbolsScript)
    excluded = LSPProvider.get_symbols(str(target), include_imports=False)
    assert "import" not in excluded["by_type"]
    assert excluded["total_symbols"] == 1

    included = LSPProvider.get_symbols(str(target), include_imports=True)
    assert included["by_type"].get("import") == 1
    assert included["total_symbols"] == 2


def test_get_symbols_jedi_error(tmp_path, monkeypatch):
    target = _write(tmp_path, "boom.py", "x = 1\n")
    monkeypatch.setattr(lsp_provider.jedi, "Script", _RaisingScript)
    result = LSPProvider.get_symbols(str(target))
    assert result["success"] is False
    assert "分析失败" in result["error"]
