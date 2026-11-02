"""Generate the polished false-completion benchmark data card PNG.

Output: docs/benchmark-card.png  (three dark metric tiles)
Run:    python scripts/make_data_card.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, H = 1200, 250
FONT = Path(r"C:\Windows\Fonts")

# (big number, Chinese label, English label, accent color)
METRICS = [
    ("0", "假完成", "false completions", (63, 185, 80)),
    ("8", "带债务被正确拦截", "blocked with debt", (210, 153, 34)),
    ("9", "总样本", "total cases", (88, 166, 255)),
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


def main():
    bg = vgrad((W, H), (22, 27, 34), (28, 33, 40)).convert("RGBA")
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=16, fill=255)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=16,
                        outline=(88, 166, 255, 70), width=2)

    fnum = ImageFont.truetype(str(FONT / "consolab.ttf"), 72)
    flabel = ImageFont.truetype(str(FONT / "msyhbd.ttc"), 24)
    fsub = ImageFont.truetype(str(FONT / "consola.ttf"), 16)

    col_w = W / 3
    for i, (num, label, sub, color) in enumerate(METRICS):
        cx = col_w * i + col_w / 2
        # accent top bar per tile
        d.rounded_rectangle([cx - 34, 26, cx + 34, 32], radius=3, fill=(*color, 255))
        # big number
        bbox = d.textbbox((0, 0), num, font=fnum)
        d.text((cx - (bbox[2] - bbox[0]) / 2, 46), num, font=fnum, fill=(*color, 255))
        # labels
        bbox = d.textbbox((0, 0), label, font=flabel)
        d.text((cx - (bbox[2] - bbox[0]) / 2, 140), label, font=flabel,
               fill=(235, 240, 246, 255))
        bbox = d.textbbox((0, 0), sub, font=fsub)
        d.text((cx - (bbox[2] - bbox[0]) / 2, 178), sub, font=fsub,
               fill=(139, 148, 158, 255))
        # divider between tiles
        if i < 2:
            x = col_w * (i + 1)
            d.line([x, 40, x, H - 40], fill=(255, 255, 255, 28), width=1)

    canvas.save(ROOT / "docs" / "benchmark-card.png")
    print("saved docs/benchmark-card.png")


if __name__ == "__main__":
    main()
