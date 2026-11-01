"""Tests for the read-only document parser and the ``read_document`` tool.

The OLE2 + BIFF8 path is exercised with a synthetic compound file written by
the test helper (header + FAT + directory + Workbook stream).  Real-world
``.et``/``.xls`` files were validated manually during development; no personal
document is committed here.
"""

from __future__ import annotations

import struct
import sys
import types
import zipfile

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes.tool_executor import required_execution_level
from xenon.nodes.tool_families.document_tools import _biff_version, parse_document
from xenon.nodes.tool_node import ToolNode
from xenon.nodes.tool_registry import BUILTIN_TOOL_REGISTRY

_OLE2_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_FREE = 0xFFFFFFFF
_END = 0xFFFFFFFE
_SECTOR = 512


# ── 测试夹具：合成 OLE2 + BIFF8 ──────────────────────────────
def _biff(rid: int, data: bytes = b"") -> bytes:
    return struct.pack("<HH", rid, len(data)) + data


def _biff_string(value: str) -> bytes:
    """BIFF8 string: ASCII → compressed, 非 ASCII → uncompressed UTF-16LE。"""
    if all(ord(ch) < 0x80 for ch in value):
        return struct.pack("<HB", len(value), 0x00) + value.encode("latin-1")
    return struct.pack("<HB", len(value), 0x01) + value.encode("utf-16-le")


def _biff_workbook() -> bytes:
    bof = _biff(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0x0DBB, 0x07CC, 0, 0))
    sst = _biff(
        0x00FC, struct.pack("<II", 3, 3) + _biff_string("name") + _biff_string("数值") + _biff_string("ok")
    )
    name = "Sheet1"
    header = struct.pack("<IHBB", 0, 0, len(name), 0) + name.encode("gbk")
    globals_len = len(bof) + len(sst) + len(_biff(0x0085, header)) + 4
    boundsheet = _biff(0x0085, struct.pack("<IHBB", globals_len, 0, len(name), 0) + name.encode("gbk"))
    globals_ = bof + sst + boundsheet + _biff(0x000A)
    assert len(globals_) == globals_len
    sheet = (
        bof
        + _biff(0x00FD, struct.pack("<HHHI", 0, 0, 0, 0))  # A1 = "name"
        + _biff(0x0203, struct.pack("<HHHd", 1, 0, 0, 42.5))  # A2 = 42.5
        + _biff(0x027E, struct.pack("<HHHI", 2, 0, 0, (7 << 2) | 0x02))  # A3 = 7
        + _biff(0x027E, struct.pack("<HHHI", 3, 0, 0, (0x3FFFFFFB << 2) | 0x02))  # A4 = -5
        + _biff(0x00FD, struct.pack("<HHHI", 4, 1, 0, 1))  # B5 = "数值"
        + _biff(0x000A)
    )
    data = globals_ + sheet
    # 保证 Workbook 流 >= mini cutoff(4096)，从而走主 FAT 路径。
    pad = (-len(data)) % _SECTOR
    data += b"\x00" * pad
    while len(data) < 4096:
        data += b"\x00" * _SECTOR
    return data


def _dir_entry(name: str, etype: int, start: int, size: int) -> bytes:
    entry = bytearray(128)
    raw = name.encode("utf-16-le") + b"\x00\x00"
    entry[: len(raw)] = raw
    struct.pack_into("<H", entry, 0x40, len(raw))
    entry[0x42] = etype
    entry[0x43] = 1
    struct.pack_into("<I", entry, 0x44, _FREE)
    struct.pack_into("<I", entry, 0x48, _FREE)
    struct.pack_into("<I", entry, 0x4C, _FREE)
    struct.pack_into("<I", entry, 0x74, start)
    struct.pack_into("<Q", entry, 0x78, size)
    return bytes(entry)


def _write_cfb(workbook: bytes) -> bytes:
    n_data = len(workbook) // _SECTOR
    n_sectors = 2 + n_data
    fat = [_FREE] * 128
    fat[0] = 0xFFFFFFFD  # FAT sector 标记
    fat[1] = _END  # 目录
    for i in range(n_data):
        sid = 2 + i
        fat[sid] = _END if i == n_data - 1 else sid + 1

    header = bytearray(512)
    header[0:8] = _OLE2_MAGIC
    struct.pack_into("<H", header, 0x18, 0x003E)
    struct.pack_into("<H", header, 0x1A, 0x0003)
    struct.pack_into("<H", header, 0x1C, 0xFFFE)
    struct.pack_into("<H", header, 0x1E, 9)
    struct.pack_into("<H", header, 0x20, 6)
    struct.pack_into("<I", header, 0x2C, 1)  # num FAT
    struct.pack_into("<I", header, 0x30, 1)  # first dir
    struct.pack_into("<I", header, 0x38, 4096)  # mini cutoff
    struct.pack_into("<I", header, 0x3C, _END)  # first miniFAT
    struct.pack_into("<I", header, 0x44, _END)  # first DIFAT
    for i in range(109):
        struct.pack_into("<I", header, 0x4C + 4 * i, 0 if i == 0 else _FREE)

    fat_sector = struct.pack("<128I", *fat)
    directory = _dir_entry("Root Entry", 5, _END, 0) + _dir_entry(
        "Workbook", 2, 2, len(workbook)
    )
    directory += b"\x00" * (_SECTOR - len(directory))
    assert n_sectors <= 128
    return bytes(header) + fat_sector + directory + workbook


def _make_ole2_et(path) -> None:
    path.write_bytes(_write_cfb(_biff_workbook()))


def _make_biff5_xls(path) -> None:
    bof = _biff(0x0809, struct.pack("<HHHHII", 0x0500, 0x0005, 0x0DBB, 0x07CC, 0, 0))
    bof += b"\x00" * ((-len(bof)) % _SECTOR)
    while len(bof) < 4096:
        bof += b"\x00" * _SECTOR
    path.write_bytes(_write_cfb(bof))


def _make_xlsx(path) -> None:
    shared = (
        '<?xml version="1.0"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>name</t></si><si><t>value</t></si></sst>"
    )
    sheet = (
        '<?xml version="1.0"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2"><v>42</v></c><c r="B2" t="s"><v>0</v></c></row>'
        "</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


def _make_docx(path) -> None:
    document = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>世界</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document)


# ── 解析测试 ─────────────────────────────────────────────────
def test_parse_xlsx(tmp_path) -> None:
    target = tmp_path / "book.xlsx"
    _make_xlsx(target)

    text, meta = parse_document(target)

    assert meta["format"] == "xlsx"
    assert "name\tvalue" in text
    assert "42" in text


def test_parse_docx(tmp_path) -> None:
    target = tmp_path / "doc.docx"
    _make_docx(target)

    text, meta = parse_document(target)

    assert meta["format"] == "docx"
    assert "Hello" in text
    assert "世界" in text


def test_parse_xlsx_respects_row_and_column_bounds(tmp_path) -> None:
    target = tmp_path / "bounded.xlsx"
    _make_xlsx(target)

    text, _meta = parse_document(target, max_rows=1, max_cols=1)

    assert "name" in text
    assert "value" not in text
    assert "42" not in text


def test_parse_biff8_respects_row_and_column_bounds(tmp_path) -> None:
    target = tmp_path / "bounded.et"
    _make_ole2_et(target)

    text, meta = parse_document(target, max_rows=1, max_cols=1)

    assert meta["format"] == "xls-biff8"
    assert meta["truncated"] is True
    assert "name" in text
    assert "数值" not in text
    assert "42.5" not in text


def test_parse_docx_appends_marker_when_character_bound_is_exceeded(tmp_path) -> None:
    target = tmp_path / "bounded.docx"
    _make_docx(target)

    text, _meta = parse_document(target, max_chars=3)

    assert text.startswith("Hel")
    assert text.endswith("\n…（已截断）")


def test_parse_csv_and_json(tmp_path) -> None:
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")
    json_path = tmp_path / "data.json"
    json_path.write_text('{"k": "v"}', encoding="utf-8")

    csv_text, csv_meta = parse_document(csv_path)
    json_text, json_meta = parse_document(json_path)

    assert csv_meta["format"] == "csv"
    assert "a\tb" in csv_text
    assert json_meta["format"] == "json"
    assert '"k": "v"' in json_text


def test_parse_biff8_ole2(tmp_path) -> None:
    target = tmp_path / "table.et"
    _make_ole2_et(target)

    text, meta = parse_document(target)

    assert meta["format"] == "xls-biff8"
    assert meta["sheets"] == ["Sheet1"]
    # SST 字符串（含中文）+ NUMBER + RK 都解析出来了
    assert "name" in text
    assert "数值" in text
    assert "42.5" in text
    assert "7" in text
    assert "-5" in text


def test_biff_version_rejects_short_or_non_bof_headers() -> None:
    assert _biff_version(b"") is None
    assert _biff_version(struct.pack("<HH", 0x1234, 4) + b"\x00\x00") is None


def test_parse_biff5_with_xlrd(tmp_path, monkeypatch) -> None:
    target = tmp_path / "legacy.xls"
    _make_biff5_xls(target)

    class _Sheet:
        name = "Legacy"
        nrows = 2
        ncols = 2

        @staticmethod
        def cell_value(row, col):
            return [["name", "value"], ["old", 7]][row][col]

    class _Book:
        def sheet_names(self):
            return ["Legacy"]

        @staticmethod
        def sheet_by_name(name):
            assert name == "Legacy"
            return _Sheet()

    fake_xlrd = types.SimpleNamespace(
        open_workbook=lambda *, filename, on_demand: _Book()
    )
    monkeypatch.setitem(sys.modules, "xlrd", fake_xlrd)

    text, meta = parse_document(target)

    assert meta["format"] == "xls-biff5"
    assert meta["sheets"] == ["Legacy"]
    assert "name\tvalue" in text
    assert "old\t7" in text


def test_parse_biff5_marks_tiny_output_as_truncated(tmp_path, monkeypatch) -> None:
    target = tmp_path / "legacy.xls"
    _make_biff5_xls(target)

    class _Book:
        def sheet_names(self):
            return ["Legacy"]

        @staticmethod
        def sheet_by_name(name):
            raise AssertionError(f"sheet should not be opened for tiny output: {name}")

    fake_xlrd = types.SimpleNamespace(
        open_workbook=lambda *, filename, on_demand: _Book()
    )
    monkeypatch.setitem(sys.modules, "xlrd", fake_xlrd)

    text, meta = parse_document(target, max_chars=1)

    assert meta["format"] == "xls-biff5"
    assert meta["truncated"] is True
    assert "已截断" in text



def test_parse_biff5_without_xlrd_has_actionable_error(tmp_path, monkeypatch) -> None:
    target = tmp_path / "legacy.xls"
    _make_biff5_xls(target)
    monkeypatch.setitem(sys.modules, "xlrd", None)

    with pytest.raises(
        ValueError,
        match=r"pip install xenon-harness\[office\] 或 pip install xlrd",
    ):
        parse_document(target)


def test_parse_unsupported_extension_raises(tmp_path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x01\x02\x03\x04" * 8)

    with pytest.raises(ValueError, match="暂不支持"):
        parse_document(target)


# ── 边界测试：max_rows / max_cols / max_chars ────────────────
def test_xlsx_max_rows_clamps_emitted_rows(tmp_path) -> None:
    target = tmp_path / "book.xlsx"
    _make_xlsx(target)

    text, _meta = parse_document(target, max_rows=1)

    assert "name" in text
    assert "42" not in text


def test_xlsx_max_cols_clamps_emitted_columns(tmp_path) -> None:
    target = tmp_path / "book.xlsx"
    _make_xlsx(target)

    text, _meta = parse_document(target, max_cols=1)

    assert "name" in text
    assert "42" in text
    assert "value" not in text


def test_biff8_max_rows_clamps_and_flags_truncated(tmp_path) -> None:
    target = tmp_path / "table.et"
    _make_ole2_et(target)

    text, meta = parse_document(target, max_rows=1)

    assert "name" in text
    assert "42.5" not in text
    assert meta["truncated"] is True


def test_biff8_max_cols_clamps_columns_without_row_truncation(tmp_path) -> None:
    target = tmp_path / "table.et"
    _make_ole2_et(target)

    text, meta = parse_document(target, max_cols=1)

    assert "name" in text
    assert "42.5" in text
    assert "数值" not in text
    assert meta["truncated"] is False


def test_max_chars_truncates_and_appends_marker(tmp_path) -> None:
    # xlsx / biff8 走 text[:max_chars] + marker；docx 走 content[:max_chars] + marker。
    cases = (
        ("book.xlsx", _make_xlsx),
        ("table.et", _make_ole2_et),
        ("doc.docx", _make_docx),
    )
    for name, make in cases:
        target = tmp_path / name
        make(target)

        text, _meta = parse_document(target, max_chars=3)

        assert text.endswith("\n…（已截断）"), name
        assert len(text) == 3 + len("\n…（已截断）"), name


# ── 工具注册 / schema / 真实调用 ─────────────────────────────
def test_read_document_is_registered_as_read_only() -> None:
    definition = BUILTIN_TOOL_REGISTRY.get("read_document")

    assert definition is not None
    assert definition.risk == "INFO"
    assert required_execution_level("read_document", {}) == 1

    from xenon.nodes.tool_registry import BUILTIN_TOOL_METHODS

    assert "read_document" in BUILTIN_TOOL_METHODS


def test_read_document_stays_read_only_under_policy() -> None:
    """A read-only turn must allow the document reader and refuse shell.

    The engine's schema filter is gone; the policy check is what actually
    decides, so that is what the test pins.
    """

    from xenon.nodes.tool_executor import execution_policy_denial

    read_only = AgentContext({"_execution_level": 1})

    assert execution_policy_denial("read_document", {"file_path": "a.xlsx"}, read_only) is None
    assert execution_policy_denial("command", {"action": "ls"}, read_only)


def test_read_document_tool_executes_on_xlsx(tmp_path) -> None:
    target = tmp_path / "book.xlsx"
    _make_xlsx(target)

    result = ToolNode(
        "doc", action_type="read_document", file_path=str(target), cwd=str(tmp_path)
    ).execute(AgentContext())

    assert result["success"] is True
    assert result["format"] == "xlsx"
    assert "name" in result["content"]


def test_read_document_tool_reports_parse_error(tmp_path) -> None:
    target = tmp_path / "broken.xlsx"
    target.write_bytes(b"PK\x03\x04 not-a-zip")

    result = ToolNode(
        "doc", action_type="read_document", file_path=str(target), cwd=str(tmp_path)
    ).execute(AgentContext())

    assert result["success"] is False
    assert "无法解析文档" in result["error"]
