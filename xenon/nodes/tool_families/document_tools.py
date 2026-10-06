"""Read-only document parsing tools (stdlib-first).

``read_document`` complements ``read_file``: the latter reads text, this one
understands container formats.

Supported without extra dependencies:

* ``xlsx`` / ``xlsm`` — ZIP + SpreadsheetML
* ``docx`` — ZIP + WordprocessingML
* ``xls`` / ``et`` — OLE2 + BIFF8 (WPS ``.et`` tables share the BIFF8 layout)
* ``csv`` / ``tsv`` / ``json`` / ``markdown`` / plain text

Optional (used when importable): ``pypdf`` / ``PyPDF2`` for PDF, ``xlrd`` for
BIFF5 workbooks.  Everything is bounded so a giant workbook cannot flood the
model context.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import struct
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from xenon.engine.context import AgentContext

logger = logging.getLogger(__name__)

MAX_ROWS = 200
MAX_COLS = 40
MAX_CHARS = 24000
MAX_DOCX_PARAGRAPHS = 400

_OLE2_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"


# ──────────────────────────────────────────────────────────────
# OLE2 复合文档（CFB）读取
# ──────────────────────────────────────────────────────────────
class _Ole2Container:
    """Minimal CFB (Compound File Binary) reader, enough for Workbook streams."""

    FREE = 0xFFFFFFFF
    END = 0xFFFFFFFE

    def __init__(self, data: bytes) -> None:
        if data[:8] != _OLE2_MAGIC:
            raise ValueError("不是 OLE2 复合文档")
        self.data = data
        sector_shift = struct.unpack_from("<H", data, 0x1E)[0]
        self.mini_shift = struct.unpack_from("<H", data, 0x20)[0]
        if not 7 <= sector_shift <= 14:
            raise ValueError("OLE2 sector shift 非法")
        self.sector_size = 1 << sector_shift
        self.mini_size = 1 << self.mini_shift
        self.mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
        self._fat = self._read_fat()
        self._dir = self._read_directory()
        self._mini_fat: list[int] | None = None
        self._mini_stream: bytes | None = None

    # -- helpers -------------------------------------------------
    def _sector_offset(self, sid: int) -> int:
        return (sid + 1) << (self.sector_size.bit_length() - 1)

    def _read_fat(self) -> list[int]:
        data = self.data
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        first_difat = struct.unpack_from("<I", data, 0x44)[0]
        sid = first_difat
        guard = 0
        while sid not in (self.FREE, self.END) and guard < 4096:
            off = self._sector_offset(sid)
            entries = struct.unpack_from(
                "<%dI" % (self.sector_size // 4), data, off
            )
            difat.extend(entries[:-1])
            sid = entries[-1]
            guard += 1
        fat: list[int] = []
        for fsid in difat:
            if fsid in (self.FREE, self.END):
                continue
            off = self._sector_offset(fsid)
            if off + self.sector_size > len(data):
                break
            fat.extend(
                struct.unpack_from("<%dI" % (self.sector_size // 4), data, off)
            )
        return fat

    def _read_chain(self, start: int, size: int, *, mini: bool = False) -> bytes:
        out = bytearray()
        sid = start
        guard = 0
        table = self._mini_fat if mini else self._fat
        step = self.mini_size if mini else self.sector_size
        if table is None:
            raise ValueError("mini FAT 尚未加载")
        while sid not in (self.FREE, self.END) and len(out) < size:
            if guard > 4_000_000:
                break
            if mini:
                if self._mini_stream is None:
                    raise ValueError("mini stream 尚未加载")
                out += self._mini_stream[sid * step : (sid + 1) * step]
            else:
                off = self._sector_offset(sid)
                out += self.data[off : off + step]
            sid = table[sid] if sid < len(table) else self.END
            guard += 1
        return bytes(out[:size])

    def _read_directory(self) -> list[dict[str, Any]]:
        first = struct.unpack_from("<I", self.data, 0x30)[0]
        raw = self._read_chain(first, 1 << 30)
        entries: list[dict[str, Any]] = []
        for i in range(len(raw) // 128):
            e = raw[i * 128 : (i + 1) * 128]
            nlen = struct.unpack_from("<H", e, 0x40)[0]
            if nlen == 0:
                continue
            name = e[: max(0, nlen - 2)].decode("utf-16-le", errors="replace")
            entries.append(
                {
                    "name": name,
                    "type": e[0x42],
                    "start": struct.unpack_from("<I", e, 0x74)[0],
                    "size": struct.unpack_from("<Q", e, 0x78)[0],
                }
            )
        return entries

    def _load_mini(self) -> None:
        if self._mini_fat is not None:
            return
        root = next((e for e in self._dir if e["type"] == 5), None)
        if root is None:
            raise ValueError("OLE2 缺少 Root Entry")
        self._mini_stream = self._read_chain(root["start"], root["size"])
        first = struct.unpack_from("<I", self.data, 0x3C)[0]
        if first in (self.FREE, self.END):
            self._mini_fat = []
            return
        raw = self._read_chain(first, 1 << 30)
        self._mini_fat = list(struct.unpack_from("<%dI" % (len(raw) // 4), raw))

    def get_stream(self, *names: str) -> bytes | None:
        lookup = {n.lower() for n in names}
        for entry in self._dir:
            if entry["type"] != 2 or entry["name"].lower() not in lookup:
                continue
            size = int(entry["size"])
            if size < self.mini_cutoff and size > 0:
                self._load_mini()
                return self._read_chain(entry["start"], size, mini=True)
            return self._read_chain(entry["start"], size)
        return None

    def stream_names(self) -> list[str]:
        return [e["name"] for e in self._dir if e["type"] == 2]


# ──────────────────────────────────────────────────────────────
# BIFF8 解析（.xls / .et）
# ──────────────────────────────────────────────────────────────
def _iter_biff_records(data: bytes) -> list[tuple[int, bytes]]:
    records: list[tuple[int, bytes]] = []
    off = 0
    while off + 4 <= len(data):
        rid, rlen = struct.unpack_from("<HH", data, off)
        start = off + 4
        end = start + rlen
        if end > len(data):
            break
        records.append((rid, data[start:end]))
        off = end
    return records


class _SstCursor:
    """Cursor over the SST record plus its CONTINUE chunks.

    BIFF8 may split a string's character data across records; the continuation
    record then starts with a one-byte option flag telling whether the rest is
    compressed.  Headers/rich-run/ext data continuations carry no flag.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.ci = 0
        self.pos = 0

    def _at_end(self) -> bool:
        return self.pos >= len(self.chunks[self.ci])

    def _next_chunk(self) -> None:
        self.ci += 1
        if self.ci >= len(self.chunks):
            raise ValueError("SST CONTINUE 数据缺失")
        self.pos = 0

    def u8(self) -> int:
        if self._at_end():
            self._next_chunk()
        value = self.chunks[self.ci][self.pos]
        self.pos += 1
        return value

    def u16(self) -> int:
        return struct.unpack("<H", self.raw(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.raw(4))[0]

    def raw(self, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            if self._at_end():
                self._next_chunk()
            take = min(n, len(self.chunks[self.ci]) - self.pos)
            out += self.chunks[self.ci][self.pos : self.pos + take]
            self.pos += take
            n -= take
        return bytes(out)

    def read_string(self) -> str:
        cch = self.u16()
        flags = self.u8()
        rich = bool(flags & 0x08)
        ext = bool(flags & 0x04)
        compressed = not (flags & 0x01)
        runs = self.u16() if rich else 0
        ext_len = self.u32() if ext else 0

        chars: list[str] = []
        remaining = cch
        while remaining > 0:
            if self._at_end():
                self._next_chunk()
                # 字符数据跨记录：CONTINUE 头部是编码标记
                flags2 = self.u8()
                compressed = not (flags2 & 0x01)
            avail = len(self.chunks[self.ci]) - self.pos
            width = 1 if compressed else 2
            take = min(remaining, avail // width)
            if take == 0:
                continue
            chunk = self.raw(take * width)
            chars.append(
                chunk.decode("gbk" if compressed else "utf-16-le", errors="replace")
            )
            remaining -= take
        if rich:
            self.raw(4 * runs)
        if ext:
            self.raw(ext_len)
        return "".join(chars)


def _parse_sst(records: list[tuple[int, bytes]]) -> list[str]:
    start = next((i for i, (rid, _) in enumerate(records) if rid == 0x00FC), None)
    if start is None:
        return []
    chunks = [records[start][1]]
    i = start + 1
    while i < len(records) and records[i][0] == 0x003C:
        chunks.append(records[i][1])
        i += 1
    cursor = _SstCursor(chunks)
    cursor.u32()  # cstTotal
    unique = cursor.u32()
    strings: list[str] = []
    for _ in range(unique):
        try:
            strings.append(cursor.read_string())
        except (ValueError, struct.error, IndexError):
            break
    return strings


def _decode_rk(rk: int) -> float | int:
    if rk & 0x02:
        value: float | int = rk >> 2
        if value & 0x20000000:
            value -= 0x40000000
    else:
        value = struct.unpack("<d", struct.pack("<Q", (rk & 0xFFFFFFFC) << 32))[0]
    if rk & 0x01:
        value = value / 100.0
    return value


def _parse_sheet(
    records: list[tuple[int, bytes]],
    start: int,
    sst: list[str],
    rows: dict[int, dict[int, Any]],
) -> None:
    i = start
    while i < len(records):
        rid, data = records[i]
        if rid == 0x000A:  # EOF
            break
        try:
            if rid == 0x00FD:  # LABELSST
                row, col, _xf, isst = struct.unpack_from("<HHHI", data, 0)
                if isst < len(sst):
                    rows.setdefault(row, {})[col] = sst[isst]
            elif rid == 0x0203:  # NUMBER
                row, col, _xf = struct.unpack_from("<HHH", data, 0)
                value = struct.unpack_from("<d", data, 6)[0]
                rows.setdefault(row, {})[col] = value
            elif rid == 0x027E:  # RK
                row, col, _xf, rk = struct.unpack_from("<HHHI", data, 0)
                rows.setdefault(row, {})[col] = _decode_rk(rk)
            elif rid == 0x00BD:  # MULRK
                row, first = struct.unpack_from("<HH", data, 0)
                pos = 4
                col = first
                while pos + 6 <= len(data) - 2:
                    _xf, rk = struct.unpack_from("<HI", data, pos)
                    rows.setdefault(row, {})[col] = _decode_rk(rk)
                    pos += 6
                    col += 1
            elif rid == 0x0205:  # BOOLERR
                row, col, _xf, value, is_error = struct.unpack_from("<HHHBB", data, 0)
                rows.setdefault(row, {})[col] = (
                    f"#ERR{value}" if is_error else bool(value)
                )
            elif rid == 0x0006:  # FORMULA
                row, col, _xf = struct.unpack_from("<HHH", data, 0)
                result = data[6:14]
                if result[6:8] == b"\xff\xff" and i + 1 < len(records):
                    nxt_id, nxt = records[i + 1]
                    if nxt_id == 0x0207:
                        rows.setdefault(row, {})[col] = _parse_unicode_string(nxt)
                    else:
                        rows.setdefault(row, {})[col] = "<公式>"
                else:
                    rows.setdefault(row, {})[col] = struct.unpack("<d", result)[0]
        except (struct.error, IndexError):
            pass
        i += 1


def _parse_unicode_string(data: bytes) -> str:
    try:
        cch = struct.unpack_from("<H", data, 0)[0]
        flags = data[2]
        compressed = not (flags & 0x01)
        body = data[3:]
        if compressed:
            return body[:cch].decode("gbk", errors="replace")
        return body[: cch * 2].decode("utf-16-le", errors="replace")
    except (struct.error, IndexError):
        return ""


def _render_table(
    rows: dict[int, dict[int, Any]],
    *,
    max_rows: int,
    max_cols: int,
) -> tuple[str, bool]:
    lines: list[str] = []
    truncated = False
    ordered = sorted(rows)
    for row_idx in ordered[:max_rows]:
        cells = rows[row_idx]
        width = min((max(cells) + 1) if cells else 0, max_cols)
        line = "\t".join(_cell_text(cells.get(c, "")) for c in range(width))
        lines.append(line.rstrip())
    if len(ordered) > max_rows:
        truncated = True
    return "\n".join(lines), truncated


def _cell_text(value: Any) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:g}"
    return str(value)


def _parse_biff8(
    workbook: bytes,
    *,
    max_rows: int,
    max_cols: int,
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    records = _iter_biff_records(workbook)
    if not records:
        raise ValueError("BIFF 记录为空")
    sst = _parse_sst(records)

    sheets: list[tuple[str, int]] = []
    for rid, data in records:
        if rid == 0x0085 and len(data) >= 8:  # BOUNDSHEET
            offset = struct.unpack_from("<I", data, 0)[0]
            cch = data[6]
            flags = data[7]
            raw = data[8:]
            name = (
                raw[:cch].decode("gbk", errors="replace")
                if not (flags & 0x01)
                else raw[: cch * 2].decode("utf-16-le", errors="replace")
            )
            sheets.append((name, offset))

    # offset → record index
    offsets: dict[int, int] = {}
    off = 0
    for idx, (rid, data) in enumerate(records):
        offsets[off] = idx
        off += 4 + len(data)

    parts: list[str] = [f"格式: XLS/BIFF8（OLE2），工作表 {len(sheets)} 个"]
    if sheets:
        parts.append("工作表: " + ", ".join(name for name, _ in sheets[:30]))

    truncated_any = False
    for name, sheet_offset in sheets:
        if sum(len(p) for p in parts) >= max_chars:
            truncated_any = True
            break
        start = offsets.get(sheet_offset)
        if start is None:
            continue
        rows: dict[int, dict[int, Any]] = {}
        _parse_sheet(records, start, sst, rows)
        body, truncated = _render_table(
            rows, max_rows=max_rows, max_cols=max_cols
        )
        truncated_any = truncated_any or truncated
        parts.append(f"\n## {name}\n{body if body else '(空表)'}")

    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（已截断）"
        truncated_any = True
    return text, {
        "format": "xls-biff8",
        "sheet_count": len(sheets),
        "sheets": [name for name, _ in sheets],
        "truncated": truncated_any,
    }


# ──────────────────────────────────────────────────────────────
# ZIP 容器：xlsx / docx
# ──────────────────────────────────────────────────────────────
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _parse_xlsx(
    path: Path,
    *,
    max_rows: int,
    max_cols: int,
    max_chars: int,
) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{_XLSX_NS}si"):
                shared.append("".join(node.text or "" for node in si.iter(f"{_XLSX_NS}t")))
        names = [
            n
            for n in zf.namelist()
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        ]
        names.sort()
        parts: list[str] = [f"格式: XLSX（{len(names)} 个工作表）"]
        for sheet_file in names:
            if sum(len(p) for p in parts) >= max_chars:
                break
            root = ElementTree.fromstring(zf.read(sheet_file))
            rows: dict[int, dict[int, Any]] = {}
            for row_el in root.iter(f"{_XLSX_NS}row"):
                r = int(row_el.get("r", "0")) - 1
                for cell in row_el.findall(f"{_XLSX_NS}c"):
                    ref = cell.get("r", "")
                    col = _xlsx_col_index(ref)
                    ctype = cell.get("t")
                    value_el = cell.find(f"{_XLSX_NS}v")
                    if value_el is None or value_el.text is None:
                        continue
                    if ctype == "s":
                        value: Any = shared[int(value_el.text)] if int(value_el.text) < len(shared) else ""
                    elif ctype == "b":
                        value = value_el.text == "1"
                    else:
                        try:
                            value = float(value_el.text)
                        except ValueError:
                            value = value_el.text
                    rows.setdefault(r, {})[col] = value
            body, _truncated = _render_table(rows, max_rows=max_rows, max_cols=max_cols)
            parts.append(f"\n## {sheet_file.rsplit('/', 1)[-1]}\n{body}")
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（已截断）"
    return text, {"format": "xlsx", "sheet_count": len(names)}


def _xlsx_col_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return max(0, index - 1)


def _parse_docx(path: Path, *, max_chars: int) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(path) as zf:
        if "word/document.xml" not in zf.namelist():
            raise ValueError("不是有效的 docx（缺少 word/document.xml）")
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    paragraphs: list[str] = []
    for para in root.iter(f"{_DOCX_NS}p"):
        text = "".join(node.text or "" for node in para.iter(f"{_DOCX_NS}t"))
        if text.strip():
            paragraphs.append(text)
    content = "\n".join(paragraphs[:MAX_DOCX_PARAGRAPHS])
    if len(content) > max_chars:
        content = content[:max_chars] + "\n…（已截断）"
    return content, {
        "format": "docx",
        "paragraph_count": len(paragraphs),
    }


# ──────────────────────────────────────────────────────────────
# PDF（可选依赖）
# ──────────────────────────────────────────────────────────────
def _parse_pdf(path: Path, *, max_chars: int) -> tuple[str, dict[str, Any]]:
    reader_cls = None
    for module_name, attr in (("pypdf", "PdfReader"), ("PyPDF2", "PdfReader")):
        try:
            module = __import__(module_name, fromlist=[attr])
            reader_cls = getattr(module, attr)
            break
        except (ImportError, AttributeError):
            continue
    if reader_cls is None:
        raise ValueError(
            "PDF 解析需要可选依赖 pypdf（pip install pypdf），或先转换为文本"
        )
    reader = reader_cls(str(path))
    texts: list[str] = []
    for page in reader.pages[:50]:
        texts.append(page.extract_text() or "")
    content = "\n".join(texts)
    if len(content) > max_chars:
        content = content[:max_chars] + "\n…（已截断）"
    return content, {"format": "pdf", "page_count": len(reader.pages)}


# ──────────────────────────────────────────────────────────────
# 文本类格式
# ──────────────────────────────────────────────────────────────
def _parse_text(path: Path, suffix: str, *, max_chars: int) -> tuple[str, dict[str, Any]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".json":
        try:
            data = json.loads(raw)
            raw = json.dumps(data, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    elif suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        rows = list(csv.reader(io.StringIO(raw), delimiter=delimiter))
        raw = "\n".join("\t".join(row) for row in rows[:MAX_ROWS])
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "\n…（已截断）"
    return raw, {"format": suffix.lstrip(".") or "text"}


# ──────────────────────────────────────────────────────────────
# 公共入口
# ──────────────────────────────────────────────────────────────
def parse_document(
    path: Path,
    *,
    max_rows: int = MAX_ROWS,
    max_cols: int = MAX_COLS,
    max_chars: int = MAX_CHARS,
) -> tuple[str, dict[str, Any]]:
    """Parse *path* into bounded plain text plus a small metadata dict."""

    head = path.read_bytes()[:8]
    suffix = path.suffix.lower()

    if head.startswith(_ZIP_MAGIC):
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        if "word/document.xml" in names:
            return _parse_docx(path, max_chars=max_chars)
        return _parse_xlsx(
            path, max_rows=max_rows, max_cols=max_cols, max_chars=max_chars
        )
    if head.startswith(_OLE2_MAGIC):
        container = _Ole2Container(path.read_bytes())
        workbook = container.get_stream("Workbook", "Book")
        if workbook is None:
            raise ValueError(
                "OLE2 文档中未找到 Workbook 流（可能是 WPS 专有扩展或 .doc）。"
                "如为 .doc，请另存为 .docx 后再读。"
            )
        return _parse_biff8(
            workbook, max_rows=max_rows, max_cols=max_cols, max_chars=max_chars
        )
    if head.startswith(_PDF_MAGIC):
        return _parse_pdf(path, max_chars=max_chars)
    if suffix in (".csv", ".tsv", ".json", ".md", ".markdown", ".txt", ".log", ".yaml", ".yml", ".xml", ".html"):
        return _parse_text(path, suffix, max_chars=max_chars)
    raise ValueError(f"暂不支持的文档格式: {suffix or '无扩展名'}")


class DocumentToolsMixin:
    """``read_document`` — parse container formats that ``read_file`` cannot."""

    def _read_document(self, context: AgentContext) -> dict[str, Any]:
        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            raise ValueError(f"[{self.id}] read_document 需要 file_path")

        path = self._validate_path(file_path, for_write=False)
        if not path.exists():
            return {
                "action_type": "read_document",
                "file_path": str(path),
                "content": "",
                "exists": False,
                "success": False,
                "error": f"文件不存在: {path}",
            }

        try:
            text, meta = parse_document(path)
        except Exception as exc:  # noqa: BLE001 — 解析失败要走结构化错误
            logger.info("[%s] read_document 解析失败 %s: %s", self.id, path, exc)
            return {
                "action_type": "read_document",
                "file_path": str(path),
                "content": "",
                "exists": True,
                "success": False,
                "error": f"无法解析文档: {exc}",
            }

        self._write_output(context, text)
        return {
            "action_type": "read_document",
            "file_path": str(path),
            "content": text,
            "size": len(text),
            "exists": True,
            "success": True,
            **meta,
        }
