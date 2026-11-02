"""Generate Xenon's 90-second terminal demo video (1280x720, 24 fps).

All benchmark figures shown in the video come from a real run of
`python -m evals.false_completion` (false_completion_count=0,
unverified_claim_count=8, total=9). Nothing on screen is fabricated output:
the script renders a deterministic terminal-style reenactment of the real
benchmark scenarios, not a fake tool transcript.

Output: ../Xenon-demo-90s.mp4 (next to the repo folder by default).
"""

from __future__ import annotations

import math
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------- canvas / style ----------
W, H = 1280, 720
FPS = 24
DURATION = 90
TOTAL_FRAMES = FPS * DURATION

BG = (13, 17, 23)            # GitHub-dark-like
PANEL = (22, 27, 34)
BORDER = (48, 54, 61)
TERMINAL_BG = (10, 13, 18)
TEXT = (201, 209, 219)
DIM = (139, 148, 158)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)
BLUE = (88, 166, 255)
PURPLE = (188, 140, 255)
SUBTITLE_BG = (0, 0, 0)

FONT_DIR = Path(r"C:\Windows\Fonts")
MONO = ImageFont.truetype(str(FONT_DIR / "consola.ttf"), 22)
MONO_B = ImageFont.truetype(str(FONT_DIR / "consolab.ttf"), 22)
MONO_SM = ImageFont.truetype(str(FONT_DIR / "consola.ttf"), 18)
CJK = ImageFont.truetype(str(FONT_DIR / "msyh.ttc"), 24)
CJK_B = ImageFont.truetype(str(FONT_DIR / "msyhbd.ttc"), 30)
CJK_TITLE = ImageFont.truetype(str(FONT_DIR / "msyhbd.ttc"), 46)
CJK_BIG = ImageFont.truetype(str(FONT_DIR / "msyhbd.ttc"), 58)


def ease(t: float) -> float:
    """clamp 0..1"""
    return max(0.0, min(1.0, t))


def typed(text: str, t: float, start: float, span: float) -> str:
    """Typewriter slice: fraction of `text` visible at time t."""
    p = ease((t - start) / span) if span > 0 else 1.0
    n = math.ceil(len(text) * p)
    return text[:n]


def center_text(draw: ImageDraw.ImageDraw, y: int, text: str, font, color):
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text(((W - w) / 2, y), text, font=font, fill=color)


def draw_terminal(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int,
                  lines: list, t: float):
    """Render a terminal panel; lines = (text, color, font, appear_time)."""
    # panel
    draw.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=TERMINAL_BG,
                           outline=BORDER, width=1)
    # title bar
    bar_h = 38
    draw.rounded_rectangle([x, y, x + w, y + bar_h], radius=10, fill=PANEL)
    draw.rectangle([x, y + bar_h - 10, x + w, y + bar_h], fill=PANEL)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = x + 22 + i * 26
        draw.ellipse([cx - 7, y + 12, cx + 7, y + 26], fill=c)
    draw.text((x + w / 2 - 60, y + 8), "terminal", font=MONO_SM, fill=DIM)
    # body
    tx, ty = x + 24, y + bar_h + 14
    lh = 30
    for idx, (text, color, font, at) in enumerate(lines):
        if t < at:
            continue
        # typewriter within 0.55s after appearance
        shown = typed(text, t, at, 0.55)
        draw.text((tx, ty + idx * lh), shown, font=font, fill=color)
        if t < at + 0.55 and shown:
            cw = draw.textlength(shown, font=font)
            cy = ty + idx * lh
            draw.rectangle([tx + cw + 2, cy + 2, tx + cw + 11, cy + 24],
                           fill=TEXT)


def draw_subtitle(draw: ImageDraw.ImageDraw, t: float, subs: list):
    for start, end, text in subs:
        if start <= t < end:
            bbox = draw.textbbox((0, 0), text, font=CJK)
            tw = bbox[2] - bbox[0]
            pad = 22
            x0 = (W - tw) / 2 - pad
            x1 = (W + tw) / 2 + pad
            y0 = H - 78
            draw.rounded_rectangle([x0, y0, x1, H - 20], radius=8,
                                   fill=SUBTITLE_BG)
            draw.text(((W - tw) / 2, y0 + 10), text, font=CJK, fill=(235, 235, 235))
            break


# ---------- subtitle timeline ----------
SUBS = [
    (8, 18, "很多代理的“通过”，只是模型的一句话，不是事实。"),
    (28, 40, "模型可以提案，但每个声明都必须绑定真实回执。"),
    (40, 55, "没有回执，就是不能交付。"),
    (55, 70, "真实验证通过后，回执进入哈希链账本。"),
    (70, 82, "交付提交由 Finalizer 独占，审计不过自动回滚。"),
]

# ---------- scene renderers ----------
def scene_title_card(draw, t):
    if t < 6:
        line1 = typed("你的 AI 代理说：", t, 0.3, 1.6)
        center_text(draw, 200, line1, CJK_B, TEXT)
        line2 = typed("「已修复，测试全部通过。」", t, 2.2, 2.4)
        center_text(draw, 280, line2, CJK_TITLE, GREEN)
    else:
        # fade/hold then red question
        q = typed("它真的跑测试了吗？", t, 6.2, 1.4)
        center_text(draw, 250, q, CJK_BIG, RED)


def scene_naive_terminal(draw, t):
    tt = t - 8
    lines = [
        ("$ agent run --task fix-parser", TEXT, MONO_B, 8 + 0.2),
        ("> analyzing repository ...", DIM, MONO, 8 + 1.4),
        ("> editing parser.py ...", DIM, MONO, 8 + 2.6),
        ("Done. All tests passed.", GREEN, MONO_B, 8 + 3.8),
        ("$ pytest -q", TEXT, MONO_B, 8 + 5.0),
        ("F  test_parser.py::test_parse", RED, MONO, 8 + 6.0),
        ("F  test_parser.py::test_roundtrip", RED, MONO, 8 + 6.7),
        ("2 failed in 0.41s", RED, MONO_B, 8 + 7.6),
    ]
    draw_terminal(draw, 140, 90, W - 280, H - 200, lines, t)


def scene_positioning(draw, t):
    center_text(draw, 180, typed("Xenon", t, 18.3, 1.0), CJK_BIG, BLUE)
    center_text(draw, 290,
                typed("不会把“测试通过”当成事实的", t, 19.6, 1.8),
                CJK_TITLE, TEXT)
    center_text(draw, 360,
                typed("可验证 AI 编码代理", t, 21.4, 1.4),
                CJK_TITLE, TEXT)
    tag = typed("evidence receipts · fail-closed · replayable ledger",
                t, 23.4, 2.4)
    center_text(draw, 470, tag, MONO_B, PURPLE)


def scene_xenon_run(draw, t):
    lines = [
        ("$ xenon", TEXT, MONO_B, 28.2),
        ("Xenon v0.9.5 - verifiable coding agent", DIM, MONO, 28.9),
        ("task: fix the failing parser test", BLUE, MONO, 29.8),
        ("[model]  claim: hidden tests passed", YELLOW, MONO, 32.2),
        ("[model]  action: finish", YELLOW, MONO, 34.0),
        ("[policy] reviewing finish claim ...", DIM, MONO, 36.0),
    ]
    draw_terminal(draw, 140, 80, W - 280, H - 190, lines, t)


def scene_blocked(draw, t):
    lines = [
        ("$ xenon", TEXT, MONO_B, 28.2),
        ("[model]  claim: hidden tests passed", YELLOW, MONO, 32.2),
        ("[policy] reviewing finish claim ...", DIM, MONO, 40.2),
        ("[controller] no TestRun receipt bound to claim", RED, MONO_B, 42.0),
        ("[controller] benchmark case: false_test_claim", TEXT, MONO, 44.0),
        ("[controller] phase=BLOCKED evidence=UNVERIFIED debt=1", RED, MONO_B, 46.2),
        ("delivery refused: claim without evidence", RED, MONO_B, 49.0),
    ]
    draw_terminal(draw, 140, 70, W - 280, 420, lines, t)
    if t >= 51.5:
        center_text(draw, 560, typed("BLOCKED", t, 51.5, 0.8), CJK_BIG, RED)


def scene_verify(draw, t):
    lines = [
        ("[gateway] pytest tests/ -q", TEXT, MONO_B, 55.4),
        ("test_parser.py .                                  [100%]", GREEN, MONO, 57.2),
        ("1 passed in 0.33s", GREEN, MONO_B, 58.6),
        ("[receipt] ToolReceipt(kind=TestRun exit=0 target=test_parser.py)",
         BLUE, MONO, 60.4),
        ("V0  receipt invariants ............................ pass", GREEN, MONO, 63.0),
        ("V1  step postconditions .......................... pass", GREEN, MONO, 65.0),
        ("V2  obligation coverage .......................... pass", GREEN, MONO, 67.0),
    ]
    draw_terminal(draw, 140, 80, W - 280, H - 190, lines, t)


def scene_finalize(draw, t):
    lines = [
        ("[finalizer] preflight audit ...................... ok", GREEN, MONO, 70.4),
        ("[finalizer] staging verified paths only", DIM, MONO, 72.4),
        ("[finalizer] commit created: a1b2c3d (verified)", BLUE, MONO_B, 74.4),
        ("[finalizer] delivery accepted: evidence=verified", GREEN, MONO_B, 77.0),
        ("replay: ledger prefix reproduces byte-identical view", PURPLE, MONO, 79.2),
    ]
    draw_terminal(draw, 140, 90, W - 280, H - 200, lines, t)


def scene_end(draw, t):
    line1 = typed("模型可以提案，但只有证据才能交付。", t, 82.2, 2.0)
    center_text(draw, 180, line1, CJK_TITLE, TEXT)
    center_text(draw, 280, typed("github.com/xianyu-sheng/Xenon", t, 84.6, 1.6),
                MONO_B, BLUE)
    # real benchmark figures
    stats = typed("false-completion benchmark: total=9  false_completion=0  blocked=8",
                  t, 86.4, 2.0)
    center_text(draw, 380, stats, MONO, GREEN)


SCENES = [
    (0, 8, scene_title_card),
    (8, 18, scene_naive_terminal),
    (18, 28, scene_positioning),
    (28, 40, scene_xenon_run),
    (40, 55, scene_blocked),
    (55, 70, scene_verify),
    (70, 82, scene_finalize),
    (82, 90, scene_end),
]


def render_frame(t: float) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    for start, end, renderer in SCENES:
        if start <= t < end or (t >= DURATION and end == DURATION):
            renderer(draw, t)
            break
    draw_subtitle(draw, t, SUBS)
    return img


def main() -> None:
    out = Path(__file__).resolve().parents[2] / "Xenon-demo-90s.mp4"
    with imageio.get_writer(out, fps=FPS, codec="libx264",
                            quality=7, macro_block_size=8) as writer:
        for frame_idx in range(TOTAL_FRAMES):
            t = frame_idx / FPS
            writer.append_data(np.asarray(render_frame(t)))
            if frame_idx % 240 == 0:
                print(f"rendered {frame_idx}/{TOTAL_FRAMES}")
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
