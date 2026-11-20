# -*- coding: utf-8 -*-
"""把一批效果图拼成一张带标签的总览图，用于一次性推送到预览。

用法：
    python contact_sheet.py --dir 03_实景 --out 03_实景/_总览.png
    python contact_sheet.py --dir 03_实景 --out 03_实景/_总览.png \
        --title "项目名 · 原木奶油风 · 实景" --cols 3 --width 2400

    # 跨目录混排：鸟瞰单图 + 实景目录，用 --only 挑图
    python contact_sheet.py \
        --dir 02_鸟瞰/01_鸟瞰_A.png --dir 03_实景 \
        --only 01_鸟瞰_A.png 01_客厅.png 02_餐厅.png \
        --out 04_文案/_全案总览.png --cols 3

规则：
  - 只收 *.png / *.jpg / *.jpeg / *.webp
  - 自动跳过下划线开头的文件（_总览.png、_crop_xx.png 这类临时件）
  - 保持每张图的原始宽高比，底部加文件名标签，方便和 prompts 对照
"""
import argparse
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

EXTS = {".png", ".jpg", ".jpeg", ".webp"}
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def collect(folder, exclude=()):
    files = []
    for p in sorted(pathlib.Path(folder).iterdir()):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        if p.name.startswith("_") or p.name.startswith("."):
            continue
        if p.name in exclude:
            continue
        files.append(p)
    return files


def main():
    ap = argparse.ArgumentParser(description="效果图总览拼图")
    ap.add_argument("--dir", action="append", required=True,
                    help="图片目录或单个图片路径，可重复传多个（按传入顺序拼接）")
    ap.add_argument("--out", required=True, help="输出 png 路径")
    ap.add_argument("--title", default="", help="顶部标题，例如 项目名 · 风格")
    ap.add_argument("--cols", type=int, default=3, help="每行几张（默认 3）")
    ap.add_argument("--width", type=int, default=2400, help="总览图宽度像素（默认 2400）")
    ap.add_argument("--only", nargs="*", help="只拼这些文件名（可省略扩展名）")
    a = ap.parse_args()

    files = []
    for d in a.dir:
        p = pathlib.Path(d)
        if p.is_file() and p.suffix.lower() in EXTS:
            files.append(p)
        elif p.is_dir():
            files.extend(collect(p))

    if a.only:
        want = set(a.only)
        files = [f for f in files if f.name in want or f.stem in want]
    if not files:
        sys.exit(f"没有可拼的图片：{a.dir}")

    cols = max(1, a.cols)
    rows = (len(files) + cols - 1) // cols
    gap = 28
    pad = 36
    title_h = 92 if a.title else 0
    label_h = 46

    cell_w = (a.width - pad * 2 - gap * (cols - 1)) / cols

    # 第一遍：算出每格缩放后的尺寸
    cells = []
    for f in files:
        im = Image.open(f).convert("RGB")
        w, h = im.size
        nw = int(cell_w)
        nh = max(1, round(h * nw / w))
        cells.append((f, im.resize((nw, nh), Image.LANCZOS), nh))

    row_h = [0] * rows
    for i, (_, _, nh) in enumerate(cells):
        r = i // cols
        row_h[r] = max(row_h[r], nh)

    # 每行实际张数（末行可能不满），用于居中对齐
    row_n = [len(cells[r * cols:(r + 1) * cols]) for r in range(rows)]

    total_h = title_h + pad * 2 + sum(row_h) + label_h * rows + gap * (rows - 1)
    sheet = Image.new("RGB", (int(a.width), int(total_h)), (255, 255, 255))
    d = ImageDraw.Draw(sheet)

    if a.title:
        d.text((pad, 30), a.title, font=load_font(44), fill=(28, 28, 30))

    y = title_h + pad
    for i, (f, im, nh) in enumerate(cells):
        r, c = divmod(i, cols)
        if c == 0 and r > 0:
            y += row_h[r - 1] + label_h + gap
        # 行内居中（末行不满时也居中，不掉在左边）
        n = row_n[r]
        row_w = n * cell_w + (n - 1) * gap
        x = (a.width - row_w) / 2 + c * (cell_w + gap)
        # 垂直居中放进行高
        oy = y + int((row_h[r] - nh) / 2)
        x, oy = int(x), int(oy)
        sheet.paste(im, (x, oy))
        d.rectangle([x, oy, x + im.size[0], oy + im.size[1]],
                    outline=(226, 226, 230), width=2)
        label = f.stem
        fnt = load_font(28)
        bb = d.textbbox((0, 0), label, font=fnt)
        d.text((x + (im.size[0] - (bb[2] - bb[0])) / 2,
                oy + im.size[1] + 10), label, font=fnt, fill=(110, 110, 118))

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"✓ 总览 {len(files)} 张 → {out}  {sheet.size[0]}x{sheet.size[1]}")
    for f in files:
        print(f"    · {f.name}")


if __name__ == "__main__":
    main()
