"""Generate polished PNG visual assets for the README:

  docs/banner.png        1280x340  hero banner with positioning statement
  docs/video-cover.png   1280x720  clickable video cover with play button

Run: python scripts/make_visual_assets.py
"""

from __future__ import annotations

from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = Path(r"C:\Windows\Fonts")

# Palette (aligned with README/SVG)
C_BG0 = (13, 17, 23)
C_BG1 = (22, 27, 34)
C_BLUE = (88, 166, 255)
C_GREEN = (63, 185, 80)
C_PURPLE = (188, 140, 255)
C_CYAN = (57, 197, 207)
C_TEXT = (201, 209, 219)
C_WHITE = (235, 240, 246)
C_DIM = (139, 148, 158)


def vgradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = row
    return img


def font(name, size):
    return ImageFont.truetype(str(FONT / name), size)


def centered(draw, cx, y, text, fnt, fill):
    bbox = draw.textbbox((0, 0), text, font=fnt)
    draw.text((cx - (bbox[2] - bbox[0]) / 2, y), text, font=fnt, fill=fill)


# ---------------------------------------------------------------- banner
def make_banner():
    W, H = 1280, 340
    img = vgradient((W, H), C_BG0, C_BG1)
    d = ImageDraw.Draw(img, "RGBA")

    # subtle terminal grid dots
    for gx in range(40, W, 40):
        for gy in range(30, H, 34):
            d.ellipse([gx - 1, gy - 1, gx + 1, gy + 1], fill=(255, 255, 255, 14))

    # left/right pixel decorations
    rng = np.random.default_rng(7)
    for _ in range(46):
        px = int(rng.integers(0, W))
        py = int(rng.integers(0, H))
        c = rng.choice([C_BLUE, C_PURPLE, C_CYAN, C_GREEN])
        alpha = int(rng.integers(40, 110))
        d.rectangle([px, py, px + 5, py + 5], fill=(*c, alpha))

    # corner terminal prompt
    d.text((28, 22), "$ xenon --verify", font=font("consola.ttf", 18), fill=(*C_DIM, 220))

    # main statement
    centered(d, W / 2, 96, "不会把“测试通过”当成事实", font("msyhbd.ttc", 48), C_WHITE)
    centered(d, W / 2, 168, "的可验证 AI 编码代理", font("msyhbd.ttc", 48), C_WHITE)

    # divider line
    d.line([W / 2 - 230, 240, W / 2 + 230, 240], fill=(*C_BLUE, 180), width=2)
    centered(d, W / 2, 258,
             "evidence receipts  ·  fail-closed verification  ·  replayable ledger",
             font("consolab.ttf", 20), C_PURPLE)

    img.save(ROOT / "docs" / "banner.png")
    print("saved docs/banner.png")


# ------------------------------------------------------------ video cover
def make_cover():
    video = ROOT.parent / "Xenon-demo-90s.mp4"
    if not video.exists():
        video = ROOT / "docs" / "xenon-demo-90s.mp4"
    reader = imageio.get_reader(video)
    frame = reader.get_data(int(48 * 24))  # blocked scene
    img = Image.fromarray(frame).convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")

    # dark gradient overlay over the whole frame, plus a fully opaque
    # bottom band that completely hides the video's own hard subtitle
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    w, h = img.size
    for y in range(h):
        a = int(120 * (abs(y - h / 2) / (h / 2)) ** 1.3) + 26
        od.line([(0, y), (w, y)], fill=(8, 11, 16, min(180, a)))
    # solid opaque band (hard edge guarantees no subtitle bleed-through)
    band_top = h - 140
    od.rectangle([0, band_top, w, h], fill=(8, 11, 16, 255))
    od.line([0, band_top, w, band_top], fill=(*C_BLUE, 200), width=2)
    img = Image.alpha_composite(img.convert("RGBA"), overlay)
    d = ImageDraw.Draw(img, "RGBA")

    # play button
    cx, cy, r = w / 2, h / 2 - 34, 58
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*C_BLUE, 205),
              outline=(255, 255, 255, 235), width=3)
    d.polygon([(cx - 16, cy - 24), (cx - 16, cy + 24), (cx + 26, cy)],
              fill=(255, 255, 255, 245))

    # single clean title on the opaque band
    centered(d, w / 2, band_top + 42, "90 秒看懂 Xenon 如何拒绝“假完成”",
             font("msyhbd.ttc", 36), C_WHITE)

    img.convert("RGB").save(ROOT / "docs" / "video-cover.png")
    print("saved docs/video-cover.png")


if __name__ == "__main__":
    make_banner()
    make_cover()
