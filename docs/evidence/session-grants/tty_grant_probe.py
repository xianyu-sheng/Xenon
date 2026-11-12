"""以真实用户身份跑一次 xenon：ConPTY + 真实模型 + 真实仓库。

目的：验证 §9 的三档授权在真实任务里成立，而不是靠直接调用模块。

场景（同一个 shell 命令会被调用两次以上，才能看出"授权"是否生效）：
  1. 让模型两次执行同一条 shell 命令（`python -c ...`），中间回答 `a`；
  2. 观察第二次是否不再弹面板；
  3. 再让模型执行另一条不同的命令，观察是否重新弹面板。

用法：
    PYTHONIOENCODING=utf-8 python docs/evidence/session-grants/tty_grant_probe.py <model>
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

import winpty  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"
ANSWER = sys.argv[2] if len(sys.argv) > 2 else "a"
REPO = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(r"C:\Users\Administrator\AppData\Local\Temp\xenon-tty-grant")

PROMPT = (
    "请调用 command 工具执行这条命令：\n"
    "python -c \"print(123)\"\n"
    "执行完后再调用一次完全相同的命令（原样复制，一个字符都不要改），"
    "然后调用 finish。不要修改任何文件。"
)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r", "", text)


def main() -> int:
    REPO.mkdir(parents=True, exist_ok=True)
    (REPO / "README.md").write_text("# tty grant probe\n", encoding="utf-8")

    # No --config: read the user's real ~/.xenon/models.yaml, which is what a
    # real launch does — and what used to crash on a missing model_id.
    cmd = f"xenon --model {MODEL} --permission auto-edit"
    print(f"cwd: {REPO}")
    print(f"cmd: {cmd}")

    pty = winpty.PTY(120, 44)
    pty.spawn("powershell.exe", cwd=str(REPO))
    pty.set_size(120, 44)
    transcript: list[str] = []

    def read_for(seconds: float) -> str:
        end = time.time() + seconds
        chunk = ""
        while time.time() < end:
            try:
                data = pty.read()
            except EOFError:
                break
            if data:
                chunk += data
                transcript.append(data)
            else:
                time.sleep(0.05)
        return strip_ansi(chunk)

    def send(text: str, pause: float = 0.4) -> None:
        pty.write(text)
        time.sleep(pause)

    def type_line(text: str) -> None:
        """Type a line the way a person would: a character at a time.

        prompt_toolkit reads keystroke events; a single ``write`` of a long
        paste can be swallowed or reordered under ConPTY, which made the task
        text vanish while the prompt looked alive.
        """

        for char in text:
            pty.write(char)
            time.sleep(0.006)
        time.sleep(0.3)
        pty.write("\r")
        time.sleep(0.6)

    # PowerShell 起来
    read_for(3)
    # Claude Code exports ANTHROPIC_BASE_URL at its own localhost proxy; inheriting
    # it makes xenon send every models.yaml request there and get a 401.  A real
    # user's shell does not have this, so clear it before launching.
    send(
        "$env:ANTHROPIC_BASE_URL=''; $env:ANTHROPIC_API_KEY=''; "
        "$env:ANTHROPIC_AUTH_TOKEN=''\r",
        0.6,
    )
    send(cmd + "\r")

    # The REPL may open with a resume picker ("恢复会话 [0/1/2/…]").  A real
    # user would pick 0 for a new conversation; do the same before typing a task.
    picker = ""
    end = time.time() + 20
    while time.time() < end:
        chunk = read_for(1)
        picker += chunk
        if "选择会话" in picker or "恢复会话" in picker:
            send("0\r", 1.5)
            break
    banner = read_for(10)
    print("── 启动 ──")
    print((picker + banner)[-1500:])

    # The prompt box only accepts input once its frame is drawn.  Typing into a
    # half-initialised prompt_toolkit app is dropped silently, which is what
    # made the first attempt look like "the task never ran".
    ready = ""
    end = time.time() + 25
    while time.time() < end:
        ready += read_for(1)
        if "API configured" in ready or "❯" in ready:
            break
    send("\r", 0.5)  # settle the prompt
    read_for(1)

    panel_hits = 0
    type_line(PROMPT.replace("\n", " "))

    # 边读边答：第一次见到授权面板按 a，之后若再出现就说明授权没生效
    deadline = time.time() + 240
    answered = 0
    while time.time() < deadline:
        chunk = read_for(3)
        plain = strip_ansi(chunk)
        if "需要授权" in plain:
            panel_hits += 1
            answered += 1
            print(f"── 面板 #{panel_hits} ──")
            print(plain[-1200:])
            send(ANSWER + "\r", 0.8)
        if "已授权" in plain or "不再询问" in plain:
            print(">>> 授权已落地")
        if "完成" in plain or "finalized" in plain or "步骤完成" in plain:
            if time.time() + 60 > deadline:
                break

    final = read_for(6)
    print("── 末尾 ──")
    print(strip_ansi(final)[-2000:])

    full = strip_ansi("".join(transcript))
    print("\n" + "=" * 70)
    print(f"授权面板出现次数: {panel_hits}  （期望：1 —— 同一条命令第二次不该再问）")
    print(f"提到 '不再询问': {'不再询问' in full}")
    print(f"提到 'step-one': {'step-one' in full}")
    (REPO / "transcript.txt").write_text(full, encoding="utf-8")
    print(f"完整记录: {REPO / 'transcript.txt'}")

    try:
        send("exit\r", 0.3)
        pty.close() if hasattr(pty, "close") else None
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
