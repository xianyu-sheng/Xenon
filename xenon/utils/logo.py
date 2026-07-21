"""Xenon 启动动画 Logo。

氙气轨道（Σ-3）：◇ 卫星环绕六边形核心，✦ 氙气放电呼吸灯。
仅在首次启动或 --version 时播放，每次命令不重复。
"""

from __future__ import annotations

import sys
import time
from typing import Final

# ── 颜色常量 ──
GRAY: Final = "\033[90m"
WHITE: Final = "\033[97m"
BLUE: Final = "\033[94m"
CYAN: Final = "\033[96m"
DIM_BLUE: Final = "\033[34m"
RESET: Final = "\033[0m"
BOLD: Final = "\033[1m"

# ── 静态 Logo（无 ANSI 兜底） ──
LOGO_PLAIN: Final = r"""
   ◇   ◇
    ╭───╮
   ╱ Xe ╲
  │  ✦  │
   ╲   ╱
    ╰───╯
 ◇       ◇
"""

LOGO_COLORED: Final = f"""
   {GRAY}◇{RESET}   {GRAY}◇{RESET}
    {GRAY}╭───╮{RESET}
   {GRAY}╱{RESET} {WHITE}{BOLD}Xe{RESET} {GRAY}╲{RESET}
  {GRAY}│{RESET}  {BLUE}✦{RESET}  {GRAY}│{RESET}
   {GRAY}╲{RESET}   {GRAY}╱{RESET}
    {GRAY}╰───╯{RESET}
 {GRAY}◇{RESET}       {GRAY}◇{RESET}
"""

# ── 动画帧 ──
# 帧序列：(延迟秒, 绘制函数)
# 每个绘制函数打印一帧，并上移光标覆盖上一帧

_FRAME_HEIGHT = 9  # Logo 占用行数（含上下留白）


def _move_up(n: int = _FRAME_HEIGHT) -> None:
    """光标上移 n 行。"""
    sys.stdout.write(f"\033[{n}A")
    sys.stdout.flush()


def _clear_line() -> None:
    sys.stdout.write("\033[2K")
    sys.stdout.flush()


def _draw_frame(lines: list[str]) -> None:
    """绘制一帧（行列表），左顶格。"""
    for line in lines:
        _clear_line()
        sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _satellite_positions(phase: int) -> list[tuple[int, int, str]]:
    """返回四个卫星 ◇ 的 (row, col, char)。phase 0-3 控制轨道相位。"""
    # 固定 4 个卫星位置（相对于 Logo 第一行）
    # 我们做"对向脉冲"：对面两颗变亮，交替
    positions = [
        (0, 3),   # 上方左
        (0, 7),   # 上方右
        (7, 1),   # 下方左
        (7, 9),   # 下方右
    ]
    diamonds = []
    for i, (r, c) in enumerate(positions):
        # 对向交替：phase 0/2 亮对角，phase 1/3 亮另一对角
        bright = (i + phase) % 4 < 2
        ch = f"{CYAN}◇{RESET}" if bright else f"{GRAY}◇{RESET}"
        diamonds.append((r, c, ch))
    return diamonds


def _build_frame(
    satellite_phase: int,
    spark_color: str,
    spark_char: str = "✦",
) -> list[str]:
    """构建一帧（9 行字符串列表）。"""
    diamonds = _satellite_positions(satellite_phase)

    # 空白画布 9 行
    canvas = [[" "] * 14 for _ in range(_FRAME_HEIGHT)]

    # 放置卫星
    for r, c, ch in diamonds:
        if 0 <= r < _FRAME_HEIGHT and 0 <= c < len(canvas[r]):
            # 注意：中文字符占 2 列
            canvas[r][c] = ch[0] if ch[0] != "\033" else ch

    # 这里简化：卫星放在固定位置用完整 ANSI 字符串
    # 实际构建用 line-based 方法
    d_top_left, d_top_right, d_bot_left, d_bot_right = [
        d[2] for d in diamonds
    ]

    lines = [
        f"   {d_top_left}   {d_top_right}",
        f"    {GRAY}╭───╮{RESET}",
        f"   {GRAY}╱{RESET} {WHITE}{BOLD}Xe{RESET} {GRAY}╲{RESET}",
        f"  {GRAY}│{RESET}  {spark_color}{spark_char}{RESET}  {GRAY}│{RESET}",
        f"   {GRAY}╲{RESET}   {GRAY}╱{RESET}",
        f"    {GRAY}╰───╯{RESET}",
        f" {d_bot_left}       {d_bot_right}",
        "",
        f"     {GRAY}X E N O N{RESET}",
    ]
    return lines


def play_animation(duration: float = 2.5, fps: float = 8.0) -> None:
    """播放启动动画。

    动画序列：
      1. 卫星入轨（◇ 从暗到亮交替，~1.5s）
      2. ✦ 呼吸脉冲（颜色循环暗蓝 ↔ 亮蓝 ↔ 青，~1.0s）
      3. 定格最终画面

    Args:
        duration: 动画总时长（秒）。
        fps: 每秒帧数。
    """
    frame_interval = 1.0 / fps
    total_frames = int(duration * fps)

    # 呼吸颜色循环表
    breath_colors = [DIM_BLUE, BLUE, CYAN, BLUE, DIM_BLUE, BLUE, BLUE, CYAN]

    sys.stdout.write("\n" * _FRAME_HEIGHT)  # 预留空间
    _move_up(_FRAME_HEIGHT)

    for frame_idx in range(total_frames):
        progress = frame_idx / max(total_frames - 1, 1)  # 0..1

        # 卫星相位：0..3 循环
        sat_phase = (frame_idx // 3) % 4

        # ✦ 呼吸：在动画后半段开始脉冲
        if progress < 0.5:
            spark = (BLUE, "✦")  # 稳定蓝光
        else:
            breath_idx = int((progress - 0.5) * 2 * len(breath_colors))
            breath_idx = min(breath_idx, len(breath_colors) - 1)
            spark = (breath_colors[breath_idx], "✦")

        lines = _build_frame(sat_phase, spark[0], spark[1])
        _draw_frame(lines)
        sys.stdout.flush()
        _move_up(_FRAME_HEIGHT)

        time.sleep(frame_interval)

    # 定格最终画面
    lines = _build_frame(0, BLUE, "✦")
    _draw_frame(lines)
    sys.stdout.write("\n" * 2)
    sys.stdout.flush()


def print_logo(*, animated: bool = True, duration: float = 2.5) -> None:
    """打印 Xenon Logo。

    Args:
        animated: 是否播放启动动画。
        duration: 动画时长（秒）。
    """
    if animated and sys.stdout.isatty():
        play_animation(duration=duration)
    else:
        # 非 TTY（管道、CI 等）直接打印静态版本
        sys.stdout.write(LOGO_COLORED)
        sys.stdout.write("\n")
        sys.stdout.flush()


# ── 自测 ──
if __name__ == "__main__":
    print_logo(animated=True, duration=2.0)
