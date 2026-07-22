"""Xenon Star Core startup identity.

The old orbital hexagon collapsed into a box at small sizes.  Star Core uses an
open eight-ray discharge and an asymmetric constellation, matching the icon and
the animated terminal-tab title.
"""

from __future__ import annotations

import sys
import time
from typing import Final

GRAY: Final = "\033[90m"
WHITE: Final = "\033[97m"
BLUE: Final = "\033[94m"
CYAN: Final = "\033[96m"
VIOLET: Final = "\033[95m"
DIM_BLUE: Final = "\033[34m"
RESET: Final = "\033[0m"
BOLD: Final = "\033[1m"

LOGO_PLAIN: Final = r"""
       ✧          ·
          ╲  │  ╱
      ────  Xe  ────
          ╱  ✶  ╲
    ·              ✦

        X E N O N
"""

LOGO_COLORED: Final = f"""
       {CYAN}✧{RESET}          {GRAY}·{RESET}
          {DIM_BLUE}╲  │  ╱{RESET}
      {BLUE}────{RESET}  {WHITE}{BOLD}Xe{RESET}  {VIOLET}────{RESET}
          {DIM_BLUE}╱{RESET}  {CYAN}✶{RESET}  {DIM_BLUE}╲{RESET}
    {GRAY}·{RESET}              {BLUE}✦{RESET}

        {GRAY}X E N O N{RESET}
"""

_FRAME_HEIGHT = 8
_BREATH_COLORS: tuple[str, ...] = (
    DIM_BLUE,
    BLUE,
    CYAN,
    WHITE,
    CYAN,
    BLUE,
)


def _move_up(lines: int = _FRAME_HEIGHT) -> None:
    sys.stdout.write(f"\033[{lines}A")
    sys.stdout.flush()


def _draw_frame(lines: list[str]) -> None:
    for line in lines:
        sys.stdout.write("\033[2K\r" + line + "\n")
    sys.stdout.flush()


def _point(index: int, phase: int, bright: str, dim: str = GRAY) -> str:
    """Twinkle a constellation point without moving its screen position."""
    distance = (index - phase) % 4
    if distance == 0:
        return f"{bright}✦{RESET}"
    if distance == 1:
        return f"{bright}✧{RESET}"
    return f"{dim}·{RESET}"


def _build_frame(phase: int, core_color: str) -> list[str]:
    top_left = _point(0, phase, CYAN)
    top_right = _point(1, phase, VIOLET)
    bottom_left = _point(3, phase, BLUE)
    bottom_right = _point(2, phase, CYAN)
    ray_left = CYAN if phase % 2 == 0 else BLUE
    ray_right = VIOLET if phase % 2 == 0 else BLUE

    return [
        f"       {top_left}          {top_right}",
        f"          {DIM_BLUE}╲  │  ╱{RESET}",
        f"      {ray_left}────{RESET}  {WHITE}{BOLD}Xe{RESET}  {ray_right}────{RESET}",
        f"          {DIM_BLUE}╱{RESET}  {core_color}✶{RESET}  {DIM_BLUE}╲{RESET}",
        f"    {bottom_left}              {bottom_right}",
        "",
        f"        {GRAY}X E N O N{RESET}",
        "",
    ]


def play_animation(duration: float = 2.0, fps: float = 8.0) -> None:
    """Ignite the Star Core, twinkle its constellation, then settle."""
    frame_interval = 1.0 / max(1.0, fps)
    total_frames = max(1, int(max(0.0, duration) * max(1.0, fps)))

    sys.stdout.write("\n" * _FRAME_HEIGHT)
    _move_up()
    for frame_index in range(total_frames):
        phase = (frame_index // 2) % 4
        core_color = _BREATH_COLORS[frame_index % len(_BREATH_COLORS)]
        _draw_frame(_build_frame(phase, core_color))
        _move_up()
        time.sleep(frame_interval)

    _draw_frame(_build_frame(0, CYAN))
    sys.stdout.write("\n")
    sys.stdout.flush()


def print_logo(*, animated: bool = True, duration: float = 2.0) -> None:
    """Print the Star Core identity, animating only on an interactive TTY."""
    if animated and sys.stdout.isatty():
        play_animation(duration=duration)
        return
    # Pipes, CI logs, and redirected output must not receive ANSI control codes.
    sys.stdout.write(LOGO_PLAIN)
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    print_logo(animated=True, duration=2.0)
