#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
户型图编号标注 —— 节点 1 的「带编号副本图」

在户型平面图上按给定相对坐标打编号徽标 + 右侧图例，方便后续逐区对照。
坐标用 0~1 相对值（左上角为 0,0），这样换分辨率也不用重算。

用法：
  python annotate_plan.py --src plan.png --out plan_标注.png \
      --rooms "[{\"no\":1,\"name\":\"客厅\",\"x\":0.33,\"y\":0.46}, ...]"

  # 或从文件读：
  python annotate_plan.py --src plan.png --out plan_标注.png --rooms-file rooms.json

rooms.json 可以是数组，也可以是 {"rooms":[...]}。
可选字段：color（徽标底色，默认红）、note（图例里的补充说明）。
"""

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PIL import Image, ImageDraw, ImageFont

RED = (211, 47, 47)
INK = (33, 33, 33)
GREY = (120, 120, 120)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\SourceHanSansCN-Bold.otf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def text_size(draw, txt, font):
    box = draw.textbbox((0, 0), txt, font=font)
    return box[2] - box[0], box[3] - box[1]


def main():
    ap = argparse.ArgumentParser(description="户型图编号标注")
    ap.add_argument("--src", required=True, help="原始户型图")
    ap.add_argument("--out", required=True, help="输出标注图")
    ap.add_argument("--rooms", help="房间数组 JSON 字符串")
    ap.add_argument("--rooms-file", help="房间数组 JSON 文件")
    ap.add_argument("--title", default="", help="图例标题（如 3室2厅2卫 128㎡）")
    ap.add_argument("--no-legend", action="store_true", help="不画右侧图例")
    a = ap.parse_args()

    raw = a.rooms
    if a.rooms_file:
        raw = Path(a.rooms_file).read_text(encoding="utf-8")
    if not raw:
        sys.exit("✗ 需要 --rooms 或 --rooms-file")
    data = json.loads(raw)
    rooms = data["rooms"] if isinstance(data, dict) else data
    if not rooms:
        sys.exit("✗ rooms 为空")

    src = Path(a.src)
    if not src.exists():
        sys.exit(f"✗ 原图不存在：{src}")
    im = Image.open(src).convert("RGB")
    W, H = im.size
    draw = ImageDraw.Draw(im)

    base = min(W, H)
    r = max(14, int(base * 0.026))          # 徽标半径
    f_no = load_font(int(r * 1.15))
    f_name = load_font(int(r * 0.85))

    for it in rooms:
        no = str(it.get("no", "?"))
        x = float(it.get("x", 0.5)) * W
        y = float(it.get("y", 0.5)) * H
        col = tuple(it.get("color", RED))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=col, outline=(255, 255, 255), width=max(2, r // 7))
        tw, th = text_size(draw, no, f_no)
        draw.text((x - tw / 2, y - th / 2 - r * 0.18), no, font=f_no, fill=(255, 255, 255))
        name = it.get("name", "")
        if name:
            nw, nh = text_size(draw, name, f_name)
            ty = y + r + 2
            draw.rectangle([x - nw / 2 - 3, ty - 1, x + nw / 2 + 3, ty + nh + 5],
                           fill=(255, 255, 255))
            draw.text((x - nw / 2, ty), name, font=f_name, fill=INK)

    if not a.no_legend:
        # 右侧追加图例条
        pad = int(base * 0.02)
        f_lg = load_font(max(13, int(base * 0.022)))
        f_hd = load_font(max(15, int(base * 0.026)))
        line_h = int(base * 0.042)
        width = int(W * 0.30)
        canvas = Image.new("RGB", (W + width, max(H, line_h * (len(rooms) + 3) + pad * 2)), (255, 255, 255))
        canvas.paste(im, (0, 0))
        d2 = ImageDraw.Draw(canvas)
        x0 = W + pad
        y = pad
        if a.title:
            d2.text((x0, y), a.title, font=f_hd, fill=INK)
            y += line_h
        for it in rooms:
            no = str(it.get("no", "?"))
            col = tuple(it.get("color", RED))
            rr = int(base * 0.014)
            d2.ellipse([x0, y + line_h * 0.28 - rr, x0 + 2 * rr, y + line_h * 0.28 + rr], fill=col)
            label = f"{no}  {it.get('name','')}"
            note = it.get("note")
            if note:
                label += f"（{note}）"
            d2.text((x0 + 2 * rr + pad * 0.6, y + line_h * 0.08), label, font=f_lg, fill=INK)
            y += line_h
        im = canvas

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    print(f"✓ {out}  （{len(rooms)} 个编号，{im.size[0]}x{im.size[1]}）")


if __name__ == "__main__":
    main()
