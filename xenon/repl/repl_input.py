"""Terminal input handling for the REPL — extracted from repl.py.
"""
from __future__ import annotations

import sys

from xenon.repl.input_buffer import PastedTextStore, _ShiftTabSignal



def _read_input_windows(self) -> str:
    import ctypes
    import ctypes.wintypes as wt
    import msvcrt

    VK_SHIFT = 0x10
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    GetAsyncKeyState = user32.GetAsyncKeyState
    GetAsyncKeyState.argtypes = [ctypes.c_int]
    GetAsyncKeyState.restype = ctypes.c_short

    # Console API 用于可靠地删除字符
    STD_OUTPUT_HANDLE = -11
    h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

    class COORD(ctypes.Structure):
        _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [("Left", wt.SHORT), ("Top", wt.SHORT), ("Right", wt.SHORT), ("Bottom", wt.SHORT)]

    class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [
            ("dwSize", COORD),
            ("dwCursorPosition", COORD),
            ("wAttributes", wt.WORD),
            ("srWindow", SMALL_RECT),
            ("dwMaximumWindowSize", COORD),
        ]

    SetConsoleCursorPosition = kernel32.SetConsoleCursorPosition
    SetConsoleCursorPosition.argtypes = [wt.HANDLE, COORD]
    SetConsoleCursorPosition.restype = wt.BOOL

    FillConsoleOutputCharacterW = kernel32.FillConsoleOutputCharacterW
    FillConsoleOutputCharacterW.argtypes = [wt.HANDLE, wt.WCHAR, wt.DWORD, COORD, ctypes.POINTER(wt.DWORD)]
    FillConsoleOutputCharacterW.restype = wt.BOOL

    GetConsoleScreenBufferInfo = kernel32.GetConsoleScreenBufferInfo
    GetConsoleScreenBufferInfo.argtypes = [wt.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
    GetConsoleScreenBufferInfo.restype = wt.BOOL

    def shift_held() -> bool:
        return bool(GetAsyncKeyState(VK_SHIFT) & 0x8000)

    def get_cursor_pos() -> COORD:
        info = CONSOLE_SCREEN_BUFFER_INFO()
        GetConsoleScreenBufferInfo(h_stdout, ctypes.byref(info))
        return info.dwCursorPosition

    def move_cursor(pos: COORD) -> None:
        SetConsoleCursorPosition(h_stdout, pos)

    def erase_char(ch: str) -> None:
        """删除一个字符。ASCII 用 ANSI（快），CJK 用 Console API（正确覆盖 2 列宽）。"""
        if ord(ch) > 0x7F:
            # CJK 等宽字符：占 2 列，用 Console API 覆盖
            pos = get_cursor_pos()
            if pos.X >= 2:
                new_pos = COORD(pos.X - 2, pos.Y)
                move_cursor(new_pos)
                written = wt.DWORD(0)
                FillConsoleOutputCharacterW(h_stdout, ' ', 2, new_pos, ctypes.byref(written))
        else:
            # ASCII：ANSI 一次搞定
            sys.stdout.write("\b \b")
            sys.stdout.flush()

    sys.stdout.write("\n\033[1;36mYou\033[0m: ")
    sys.stdout.flush()

    lines: list[str] = []
    current_line: list[str] = []
    cursor_pos: int = 0  # 光标在 current_line 中的位置索引

    def _redraw_from_cursor() -> None:
        """从光标位置重绘到行尾。"""
        # 打印光标右侧的所有字符
        tail = "".join(current_line[cursor_pos:])
        if tail:
            sys.stdout.write(tail)
        # 清除行尾残留字符（多出一个空格用于覆盖）
        sys.stdout.write(" ")
        # 把光标移回到正确位置
        back = len(tail) + 1
        if back > 0:
            sys.stdout.write(f"\033[{back}D")
        sys.stdout.flush()

    while True:
        ch = msvcrt.getwch()

        if ch in ('\r', '\n'):
            if shift_held():
                # 多行模式：跳到行尾再换行
                if cursor_pos < len(current_line):
                    sys.stdout.write(f"\033[{len(current_line) - cursor_pos}C")
                    cursor_pos = len(current_line)
                lines.append("".join(current_line))
                current_line = []
                cursor_pos = 0
                sys.stdout.write("\n\033[90m...\033[0m ")
                sys.stdout.flush()
            else:
                # 跳到行尾再回车（避免残留）
                if cursor_pos < len(current_line):
                    sys.stdout.write(f"\033[{len(current_line) - cursor_pos}C")
                break

        elif ch == '\x03':
            raise KeyboardInterrupt

        elif ch in ('\x08', '\x7f'):
            # Backspace: 删除光标左侧字符
            if cursor_pos > 0:
                current_line.pop(cursor_pos - 1)
                cursor_pos -= 1
                # 光标左移一格
                sys.stdout.write('\033[1D')
                # 重绘后面的字符并清除行尾
                _redraw_from_cursor()

        elif ch in ('\x00', '\xe0'):
            # 扩展键序列（方向键、Home/End 等）
            second = msvcrt.getwch()
            if second == 'K':        # ← 左箭头
                if cursor_pos > 0:
                    cursor_pos -= 1
                    sys.stdout.write('\033[1D')
                    sys.stdout.flush()
            elif second == 'M':      # → 右箭头
                if cursor_pos < len(current_line):
                    cursor_pos += 1
                    sys.stdout.write('\033[1C')
                    sys.stdout.flush()
            elif second == 'H':      # Home
                if cursor_pos > 0:
                    sys.stdout.write(f"\033[{cursor_pos}D")
                    cursor_pos = 0
                    sys.stdout.flush()
            elif second == 'O':      # End
                if cursor_pos < len(current_line):
                    sys.stdout.write(f"\033[{len(current_line) - cursor_pos}C")
                    cursor_pos = len(current_line)
                    sys.stdout.flush()
            elif second == 'S':      # Delete
                if cursor_pos < len(current_line):
                    current_line.pop(cursor_pos)
                    _redraw_from_cursor()
            elif second == '\x0f':  # Shift+Tab
                self._handle_shift_tab()
                # 不向 current_line 插入任何字符

        elif ch and ord(ch) >= 0x20:
            # 可见字符：在光标位置插入
            if cursor_pos >= len(current_line):
                # 追加到末尾（常见情况，快速路径）
                current_line.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
            else:
                # 插入到中间位置
                current_line.insert(cursor_pos, ch)
                # 重绘插入点之后的内容
                _redraw_from_cursor()
            cursor_pos += 1

    if current_line:
        lines.append("".join(current_line))

    result = "\n".join(lines)
    sys.stdout.write("\n")
    return result.strip()


def _read_input_unix(self) -> str:
    """Linux/macOS 原始终端输入：支持 Alt+Enter 换行，方向键编辑。

    使用时将终端设为原始模式，逐字节读取并解析 ANSI 转义序列。
    粘贴多行文本会被自动检测并正确处理。
    """
    import sys
    import codecs
    import os
    import termios
    import tty
    import unicodedata
    from select import select

    PROMPT = "\033[1;36mYou\033[0m: "
    CONTINUATION = "\033[90m...\033[0m "

    # ── 显示宽度计算（CJK 字符占 2 列）────────────────
    def _char_width(ch: str) -> int:
        """返回单个字符的终端显示宽度。"""
        ea = unicodedata.east_asian_width(ch)
        if ea in ('W', 'F'):
            return 2
        return 1

    def _display_width(s: str) -> int:
        """计算字符串的终端显示宽度。"""
        return sum(_char_width(ch) for ch in s)

    def _prompt_printable(prompt_str: str) -> str:
        """剥离 ANSI 转义序列，得到 prompt 的可打印文本。"""
        import re
        return re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', prompt_str)

    def _line_width_upto(chars: list[str], upto: int) -> int:
        """计算 current_line 中前 upto 个字符的终端显示宽度。"""
        return _display_width("".join(chars[:upto]))

    fd = sys.stdin.fileno()
    decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")(
        errors="replace"
    )
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)

        # ── 启用终端粘贴括号模式 ────────────────────
        # 粘贴时终端发送 \x1b[200~ 开始 / \x1b[201~ 结束，
        # 从而在粘贴期间批量处理字符，避免每字符一次重绘。
        sys.stdout.write('\x1b[?2004h')
        sys.stdout.flush()

        lines: list[str] = []
        current_line: list[str] = []
        cursor_pos: int = 0
        prompt_active = True
        paste_store = PastedTextStore()

        # ── 粘贴模式状态 ─────────────────────────────
        paste_mode = False
        paste_buffer: list[str] = []
        # v0.3.0 修复（Bug：粘贴结束信号丢失时 paste_mode 死锁）：
        # 某些终端/网络/SSH 会把 \x1b[201~ 结束信号切碎或丢失，导致 paste_mode 永远 True，
        # 后续用户按键（空格/字母）进入 paste_mode 分支被插入 current_line 但不重绘，
        # 表现为"按空格不显示 + 字符累积成重复粘贴"。超时退出机制：paste_mode 期间
        # select 0.3s 无新字节 → 自动退出 paste_mode + 强制 _redraw_line()。
        # 通用机制改进，不针对特定任务/终端加白名单。
        import time as _time
        paste_last_byte_at: float | None = None
        PASTE_TIMEOUT_S = 0.3

        def _insert_text(text: str) -> None:
            """Insert text at the cursor without turning lines into turns."""
            nonlocal current_line, cursor_pos
            parts = text.split("\n")
            before = current_line[:cursor_pos]
            after = current_line[cursor_pos:]
            if len(parts) == 1:
                inserted = list(parts[0])
                current_line = before + inserted + after
                cursor_pos += len(inserted)
                return

            lines.append("".join(before) + parts[0])
            lines.extend(parts[1:-1])
            current_line = list(parts[-1]) + after
            cursor_pos = len(parts[-1])

        def _finish_paste() -> None:
            """Commit a paste to the editor as full text or one UI token."""
            nonlocal paste_mode, paste_buffer, paste_last_byte_at
            pasted = "".join(paste_buffer)
            occupied = "\n".join([*lines, "".join(current_line)])
            _insert_text(
                paste_store.compact(pasted, occupied_text=occupied)
            )
            paste_buffer = []
            paste_mode = False
            paste_last_byte_at = None
            _redraw_line()

        def _redraw_line() -> None:
            """清除当前行并重绘（正确处理 CJK 宽字符）。"""
            nonlocal prompt_active
            prompt_str = PROMPT if (prompt_active and not lines) else CONTINUATION
            # 光标移到行首并清除到行尾
            sys.stdout.write("\r\033[K")
            # 打印提示符和当前行内容
            sys.stdout.write(prompt_str)
            sys.stdout.write("".join(current_line))
            # 光标定位：回到行首 + 提示符宽度 + 光标前内容的显示宽度
            pw = _display_width(_prompt_printable(prompt_str))
            prefix_w = _display_width("".join(current_line[:cursor_pos]))
            sys.stdout.write(f"\r\033[{pw + prefix_w}C")
            sys.stdout.flush()

        def _prompt_width() -> int:
            """当前提示符的可打印宽度。"""
            prompt_str = PROMPT if (prompt_active and not lines) else CONTINUATION
            return _display_width(_prompt_printable(prompt_str))

        def _move_cursor_to(target: int) -> None:
            """移动光标到目标字符位置（正确处理 CJK 宽字符列偏移）。"""
            nonlocal cursor_pos
            cursor_pos = max(0, min(target, len(current_line)))
            pw = _prompt_width()
            prefix_w = _display_width("".join(current_line[:cursor_pos]))
            sys.stdout.write(f"\r\033[{pw + prefix_w}C")
            sys.stdout.flush()

        # 显示初始提示符
        sys.stdout.write(PROMPT)
        sys.stdout.flush()

        # 缓冲区，用于累积多字节序列
        seq_buffer = ""

        while True:
            # 用 select 检查是否有输入（超时处理粘贴检测 + paste_mode 超时退出）
            if select([sys.stdin], [], [], 0.01)[0]:
                # Do not combine select(fd) with TextIOWrapper.read(1): the
                # wrapper may prefetch the remaining bytes, leaving them in
                # Python's private buffer while select waits forever on an
                # already-empty fd.  Read the fd itself and decode Unicode
                # incrementally so batched paste/key sequences cannot hang.
                raw = os.read(fd, 1)
                if not raw:
                    raise KeyboardInterrupt
                ch = decoder.decode(raw)
                if not ch:
                    continue
            else:
                # v0.3.0 修复：paste_mode 期间 select 0.3s 无新字节 → 自动退出
                # 解决"结束信号 \x1b[201~ 丢失导致 paste_mode 死锁"问题。
                if (
                    paste_mode
                    and paste_last_byte_at is not None
                    and _time.monotonic() - paste_last_byte_at > PASTE_TIMEOUT_S
                ):
                    _finish_paste()
                continue

            # 处理转义序列
            # v0.3.0+ 修复（C-1）：转义序列累积器**总是**累积。
            # 但有双守卫保证 paste_mode 状态机不死锁：
            #   ① paste end \x1b[201~ **总是**优先识别并关闭 paste_mode
            #      （否则粘贴内容里的 ESC 字节会让累积器错位、最终丢失
            #      paste end → paste_mode 永远 True → REPL 挂死）
            #   ② paste_mode 期间累积到 8 字节**子串搜索** paste end：
            #      - 含 paste end → 前部分追加 buffer + 关闭 paste_mode
            #      - 不含 → 整批追加 buffer（保留用户主动复制的 ESC 字节）
            if seq_buffer or ch == '\x1b':
                seq_buffer += ch
                # 守卫 ①：paste end 总是截留（精确 6 字符匹配）
                if seq_buffer == '\x1b[201~':
                    seq_buffer = ''
                    _finish_paste()
                    continue
                if paste_mode:
                    # 守卫 ②：累积 8 字节时子串搜索 paste end
                    if '\x1b[201~' in seq_buffer:
                        idx = seq_buffer.index('\x1b[201~')
                        paste_buffer.extend(seq_buffer[:idx])
                        seq_buffer = ''
                        _finish_paste()
                        continue
                    if len(seq_buffer) >= 8:
                        paste_buffer.extend(seq_buffer)
                        seq_buffer = ''
                        paste_last_byte_at = _time.monotonic()
                    continue
                if len(seq_buffer) == 1 and ch == '\x1b':
                    continue  # 等待更多字节

                # ── 粘贴括号模式 ────────────────────
                if seq_buffer == '\x1b[200~':
                    # 开始粘贴 — 暂停逐字符重绘
                    paste_mode = True
                    paste_buffer = []
                    paste_last_byte_at = _time.monotonic()
                    seq_buffer = ""
                    continue
                if seq_buffer == '\x1b[201~':
                    # 粘贴结束 — 一次性重绘
                    seq_buffer = ""
                    _finish_paste()
                    continue

                # 尝试匹配已知序列
                # Alt+Enter: \x1b\r
                if seq_buffer == '\x1b\r':
                    # 插入换行
                    lines.append("".join(current_line))
                    current_line = []
                    cursor_pos = 0
                    sys.stdout.write("\r\n")
                    sys.stdout.write(CONTINUATION)
                    sys.stdout.flush()
                    seq_buffer = ""
                    continue

                # Shift+Enter（kitty 键盘协议）: \x1b[13;2u
                if seq_buffer == '\x1b[13;2u':
                    lines.append("".join(current_line))
                    current_line = []
                    cursor_pos = 0
                    sys.stdout.write("\r\n")
                    sys.stdout.write(CONTINUATION)
                    sys.stdout.flush()
                    seq_buffer = ""
                    continue

                # Shift+Enter（xterm modifyOtherKeys）: \x1b[27;2;13~
                if seq_buffer == '\x1b[27;2;13~':
                    lines.append("".join(current_line))
                    current_line = []
                    cursor_pos = 0
                    sys.stdout.write("\r\n")
                    sys.stdout.write(CONTINUATION)
                    sys.stdout.flush()
                    seq_buffer = ""
                    continue
                if '\x1b[27;2;13~'.startswith(seq_buffer):
                    continue

                # 方向键: \x1b[A (上), \x1b[B (下), \x1b[C (右), \x1b[D (左)
                if seq_buffer == '\x1b[A':    # Up — 忽略
                    seq_buffer = ""
                    continue
                if seq_buffer == '\x1b[B':    # Down — 忽略
                    seq_buffer = ""
                    continue
                if seq_buffer == '\x1b[C':    # Right
                    if cursor_pos < len(current_line):
                        cursor_pos += 1
                        _move_cursor_to(cursor_pos)
                    seq_buffer = ""
                    continue
                if seq_buffer == '\x1b[D':    # Left
                    if cursor_pos > 0:
                        cursor_pos -= 1
                        _move_cursor_to(cursor_pos)
                    seq_buffer = ""
                    continue

                # Home: \x1b[H 或 \x1b[1~
                if seq_buffer in ('\x1b[H', '\x1b[1~', '\x1bOH'):
                    _move_cursor_to(0)
                    seq_buffer = ""
                    continue

                # End: \x1b[F 或 \x1b[4~ 或 \x1bOF
                if seq_buffer in ('\x1b[F', '\x1b[4~', '\x1bOF'):
                    _move_cursor_to(len(current_line))
                    seq_buffer = ""
                    continue

                # Delete: \x1b[3~
                if seq_buffer == '\x1b[3~':
                    if cursor_pos < len(current_line):
                        current_line.pop(cursor_pos)
                        _redraw_line()
                    seq_buffer = ""
                    continue

                # Shift+Tab: \x1b[Z → 切换思考范式
                if seq_buffer == '\x1b[Z':
                    seq_buffer = ""
                    raise _ShiftTabSignal()

                # 未知转义序列 — 静默丢弃或超时后当作普通字符
                # 如果序列长度 >= 8 或超时，丢弃
                if len(seq_buffer) >= 8:
                    seq_buffer = ""
                    continue
                # 否则继续累积
                continue

            # ── 粘贴模式：缓冲修改，不重绘 ──
            if paste_mode:
                if ch in ('\r', '\n'):
                    paste_buffer.append(ch)
                elif ch == '\x03':   # Ctrl+C during paste
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt
                elif ch in ('\x7f', '\x08'):  # Backspace
                    if paste_buffer:
                        paste_buffer.pop()
                elif ch == '\x1b':
                    # v0.3.0+ 修复（C-1 配套）：粘贴期间遇到 ESC 字节
                    # 不再走转义序列累积器（已在上方 if 屏蔽），改当普通
                    # 字符插入 buffer——用户主动复制粘贴含 ANSI 转义序列
                    # 的代码（如 `echo -e "\033[31m红色\033[0m"`）应保留 ESC
                    paste_buffer.append(ch)
                elif ord(ch) >= 0x20:
                    paste_buffer.append(ch)
                # v0.3.0 修复：每次粘贴期间字节都要刷新 last_byte_at，
                # 否则 select 0.3s 超时检查会误判 paste_mode 空闲
                paste_last_byte_at = _time.monotonic()
                continue

            # ── 普通字符处理 ──

            if ch in ('\r', '\n'):
                # Enter → 提交
                # 将光标移到行尾
                _move_cursor_to(len(current_line))
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                break

            elif ch == '\x03':   # Ctrl+C
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                raise KeyboardInterrupt

            elif ch == '\x04':   # Ctrl+D
                if not current_line and not lines:
                    # 空行 Ctrl+D → EOF
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt
                # 否则当 Delete 处理
                if cursor_pos < len(current_line):
                    current_line.pop(cursor_pos)
                    _redraw_line()

            elif ch == '\x0f':   # Ctrl+O：展开/折叠最近一次执行详情
                # This path is used when prompt_toolkit is unavailable or
                # disabled.  Previously Ctrl+O was only registered in the
                # prompt_toolkit key map, so the fallback editor silently
                # ignored it.  Render above the raw editor and redraw the
                # current prompt so input is not lost.
                self._toggle_thinking_details()
                _redraw_line()

            elif ch in ('\x7f', '\x08'):  # Backspace
                if cursor_pos > 0:
                    current_line.pop(cursor_pos - 1)
                    _move_cursor_to(cursor_pos - 1)
                    _redraw_line()

            elif ch == '\t':     # Tab → 4 空格
                for _ in range(4):
                    current_line.insert(cursor_pos, ' ')
                cursor_pos += 4
                _move_cursor_to(cursor_pos)
                _redraw_line()

            elif ord(ch) >= 0x20:
                # 可见字符
                current_line.insert(cursor_pos, ch)
                cursor_pos += 1
                _redraw_line()

        if current_line:
            lines.append("".join(current_line))

        return paste_store.expand("\n".join(lines))

    finally:
        # 禁用粘贴括号模式，恢复终端设置
        sys.stdout.write('\x1b[?2004l')
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


