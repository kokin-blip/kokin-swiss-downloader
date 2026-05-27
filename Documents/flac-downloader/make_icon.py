"""
Generates icon.ico — a Swiss army knife icon for the app.
Run once before building: python make_icon.py
"""

import io
import struct

try:
    from PIL import Image, ImageDraw
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow", "-q"])
    from PIL import Image, ImageDraw


def make_frame(size: int) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    RED    = (204, 0,   0,   255)
    WHITE  = (255, 255, 255, 255)
    SILVER = (185, 190, 200, 255)
    SHINE  = (215, 220, 230, 255)
    DARK   = (100, 105, 115, 255)

    # ── Handle body ───────────────────────────────────────────────────────────
    pad  = max(1, int(s * 0.06))
    hy1  = int(s * 0.28)
    hy2  = int(s * 0.68)
    hx2  = int(s * 0.76)
    r    = max(1, int(s * 0.10))
    d.rounded_rectangle([pad, hy1, hx2, hy2], radius=r, fill=RED)

    # ── Blade ─────────────────────────────────────────────────────────────────
    bx1  = hx2 - max(1, int(s * 0.02))
    by1  = int(s * 0.35)
    by2  = int(s * 0.58)
    tip  = (int(s * 0.97), int(s * 0.43))

    d.polygon([(bx1, by1), tip, (bx1, by2)], fill=SILVER)
    d.polygon([(bx1, by1), tip, (bx1, int(s * 0.42))], fill=SHINE)
    if s >= 32:
        d.line([(bx1, by1), tip], fill=DARK, width=max(1, s // 48))

    # ── Swiss cross ───────────────────────────────────────────────────────────
    cx  = int(s * 0.38)
    cy  = int((hy1 + hy2) / 2)
    arm = max(1, int(s * 0.10))
    bar = max(1, int(s * 0.045))

    d.rectangle([cx - bar, cy - arm, cx + bar, cy + arm], fill=WHITE)
    d.rectangle([cx - arm, cy - bar, cx + arm, cy + bar], fill=WHITE)

    return img


def write_ico(path: str, images: list[Image.Image]) -> None:
    """Write a proper multi-size ICO (each size stored as embedded PNG)."""
    num  = len(images)
    pngs = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pngs.append(buf.getvalue())

    # ICO header: reserved=0, type=1 (icon), count
    data = struct.pack("<HHH", 0, 1, num)

    # Directory: 16 bytes per entry
    offset = 6 + num * 16
    for img, png in zip(images, pngs):
        w, h = img.size
        data += struct.pack(
            "<BBBBHHII",
            w if w < 256 else 0,   # 0 means 256
            h if h < 256 else 0,
            0,                     # color count (0 = true-color)
            0,                     # reserved
            1,                     # planes
            32,                    # bits per pixel
            len(png),              # data size
            offset,                # data offset
        )
        offset += len(png)

    for png in pngs:
        data += png

    with open(path, "wb") as f:
        f.write(data)


def main():
    sizes  = [16, 24, 32, 48, 64, 128, 256]
    frames = [make_frame(s) for s in sizes]
    write_ico("icon.ico", frames)
    print(f"icon.ico saved ({len(sizes)} sizes: {sizes})")


if __name__ == "__main__":
    main()
