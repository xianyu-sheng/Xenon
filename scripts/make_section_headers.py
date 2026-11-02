"""Generate a unified set of polished section-header PNGs for the README.

Each header is a self-contained dark rounded bar with a numbered color
chip, a Chinese title and an English subtitle, so it renders identically
in GitHub light and dark themes.

Outputs: docs/sections/sec-01.png ... sec-12.png
Run:     python scripts/make_section_headers.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "sections"
OUT.mkdir(parents=True, exist_ok=True)

W, H = 1200, 96
FONT = Path(r"C:\Windows\Fonts")

# (number, Chinese title, English subtitle, chip color)
SECTIONS = [
    ("01", "它解决什么问题", "The problem", (248, 81, 73)),
    ("02", "30 秒快速开始", "Quick start", (63, 185, 80)),
    ("03", "工作演示", "See it in action", (88, 166, 255)),
    ("04", "它和 Claude Code 有什么不同", "Why not just use a coding agent", (188, 140, 255)),
    ("05", "三个核心机制", "Core mechanisms", (57, 197, 207)),
    ("06", "假完成拦截基准", "False-completion benchmark", (210, 153, 34)),
    ("07", "作为 Python 库使用", "Python API", (88, 166, 255)),
    ("08", "评测", "Evaluation", (188, 140, 255)),
    ("09", "路线图与社区", "Roadmap & community", (63, 185, 80)),
    ("10", "常见问题", "FAQ", (210, 153, 34)),
    ("11", "文档", "Documentation", (57, 197, 207)),
    ("12", "诚实边界", "Honest limits", (248, 81, 73)),
]


def vgrad(size, top, bottom):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = row
    return img


def make(num, title, subtitle, color):
    bar = vgrad((W, H), (22, 27, 34), (28, 33, 40)).convert("RGBA")
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=16, fill=255)

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.paste(bar, (0, 0), mask)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=16,
                        outline=(*color, 70), width=2)

    # numbered chip
    cx0, cy0, chip = 24, (H - 52) // 2, 52
    d.rounded_rectangle([cx0, cy0, cx0 + chip, cy0 + chip], radius=12, fill=(*color, 255))
    fnum = ImageFont.truetype(str(FONT / "consolab.ttf"), 24)
    bbox = d.textbbox((0, 0), num, font=fnum)
    d.text((cx0 + chip / 2 - (bbox[2] - bbox[0]) / 2,
            cy0 + chip / 2 - (bbox[3] - bbox[1]) / 2 - 2),
           num, font=fnum, fill=(255, 255, 255, 255))

    # titles
    tx = cx0 + chip + 22
    ftitle = ImageFont.truetype(str(FONT / "msyhbd.ttc"), 32)
    fsub = ImageFont.truetype(str(FONT / "consola.ttf"), 17)
    d.text((tx, 16), title, font=ftitle, fill=(235, 240, 246, 255))
    d.text((tx, 58), subtitle, font=fsub, fill=(139, 148, 158, 255))

    # right-side decorative dots
    for i, c in enumerate([(88, 166, 255), (188, 140, 255), color]):
        d.ellipse([W - 86 + i * 26, H / 2 - 4, W - 78 + i * 26, H / 2 + 4],
                  fill=(*c, 200))

    out = OUT / f"sec-{num}.png"
    canvas.save(out)
    return out


if __name__ == "__main__":
    for spec in SECTIONS:
        print("saved", make(*spec).relative_to(ROOT))
