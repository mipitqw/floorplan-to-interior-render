#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Seedream v5.5 (TokenHub / 腾讯云) 生图客户端 —— 户型图生装修效果图专用

只依赖标准库 + Pillow（Pillow 可选，装了就用来压缩参考图）。
接口文档：references/api-seedream.md

常用：
  # 1) 单张：户型图 → 鸟瞰顶视图
  python seedream.py --prompt-file p_bird.txt --image plan.png \
      --size auto --out out/01_鸟瞰.png

  # 2) 单张：分区实景（户型图 + 鸟瞰 双参考）。不给 --size 就是默认 3:4
  python seedream.py --prompt-file p_living.txt \
      --image plan.png --image out/01_鸟瞰.png \
      --out out/02_客厅.png

  # 2b) 明确要横构图时才写尺寸（2048x1152 = 236 万像素，仍在上限内）
  python seedream.py --prompt-file p_living.txt \
      --size 16:9 --out out/02_客厅.png

  # 3) 整批（10 个房间并行 3）
  python seedream.py --manifest rooms.json --parallel 3

  # 4) 不花钱：只校验提示词长度/图片可读
  python seedream.py --manifest rooms.json --validate-only

  # 5) 想看真实发出的请求
  python seedream.py --prompt "..." --image plan.png --print-curl
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API_URL = "https://tokenhub.tencentmaas.com/v1/wand/si-image/generation"
API_KEY_ENV = "TOKENHUB_API_KEY"

# 本地配置：~/.floorplan-render/config.json，内容 {"api_key": "sk-..."}
CONFIG_DIR = Path.home() / ".floorplan-render"
CONFIG_FILE = CONFIG_DIR / "config.json"

KEY_HELP = """\
未检测到 TokenHub API Key，无法出图。

【申请步骤】约 5 分钟
  1. 注册腾讯云并完成实名认证，开通大模型服务平台 TokenHub
     https://console.cloud.tencent.com/tokenhub/overview
  2. 进「模型广场」，找到 seedream-image-v5.5：领取免费体验额度，或直接开启按量付费
     https://console.cloud.tencent.com/tokenhub/models
     （未开按量付费且免费额度用完时，接口返回 402 / code 401007）
  3. 进「API Key 管理」，创建 Key（可访问范围选全选，或勾上该模型）
     https://console.cloud.tencent.com/tokenhub/apikey
     Key 只在创建时完整显示一次，记得复制保存。

【配置方式】任选一种，优先级从上到下
  A. 写入本地配置（推荐，一次搞定，之后所有命令自动读取）
       python seedream.py --set-key sk-你的key
  B. 环境变量
       Windows : setx TOKENHUB_API_KEY "sk-你的key"    （需重开终端）
       macOS   : export TOKENHUB_API_KEY=sk-你的key    （可写进 ~/.zshrc）
       Linux   : export TOKENHUB_API_KEY=sk-你的key    （可写进 ~/.bashrc）
  C. 单次命令带上
       python seedream.py --key sk-你的key --prompt "..." --out a.png

配置文件位置：{config}
"""


def load_saved_key():
    """读本地配置里的 api_key，没有就返回空串。"""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return str(data.get("api_key", "")).strip()
    except Exception:
        return ""


def save_key(key):
    """写入本地配置文件，并尽量收紧权限。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({"api_key": key, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    try:
        os.chmod(CONFIG_FILE, 0o600)   # 仅 POSIX 生效，Windows 忽略
    except Exception:
        pass


def resolve_key(cli_key=None):
    """按 --key > 环境变量 > 本地配置 的顺序取 key。"""
    for k in (cli_key, os.environ.get(API_KEY_ENV), load_saved_key()):
        if k and str(k).strip():
            return str(k).strip()
    return ""


def mask_key(k):
    """脱敏显示：前 6 位 + 后 4 位。"""
    k = (k or "").strip()
    if not k:
        return "(未配置)"
    if len(k) <= 12:
        return k[:3] + "***"
    return k[:6] + "…" + k[-4:]


DEFAULT_MODEL = "seedream-image-v5.5"
PROMPT_HARD_LIMIT = 600          # 接口硬限制（字符）
PROMPT_SOFT_LIMIT_CJK = 300      # 中文提示词建议上限
BODY_LIMIT = 20 * 1024 * 1024    # POST body 上限 20MB
IMG_LIMIT = 30 * 1024 * 1024     # 单张参考图上限 30MB
MAX_IMAGES = 10                  # v5.5 最多 10 张参考图
MIN_PIXELS = 921600              # 1280x720
MAX_PIXELS_HARD = 4624220        # 接口硬上限（2048x2048x1.1025）
MAX_PIXELS_USER = 2600000        # 默认产出上限：260 万像素（用户约定，除非 --allow-big）
AUTO_AREA = 2359296              # auto 尺寸目标面积（≈1536²，1.5K 档）

# 默认画布：3:4 竖构图。fit_size 会算成 1392x1856（258 万像素，未超上限）
DEFAULT_RATIO = 3 / 4
RATIOS = {
    "3:4": 3 / 4, "4:3": 4 / 3, "2:3": 2 / 3, "3:2": 3 / 2,
    "16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0,
}

ERR_BILLING = "401007"
ERR_MODEL = "401006"


# ---------------------------------------------------------------- 工具

def die(msg, code=1):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg):
    print(f"! {msg}", file=sys.stderr)


def cjk_ratio(s):
    if not s:
        return 0.0
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff") / len(s)


def round16(v):
    return max(16, int(round(v / 16.0)) * 16)


def size_from_ratio(ratio, area=AUTO_AREA):
    """按宽高比 + 目标面积算 WxH（16 的倍数，落在接口像素区间内）"""
    h = (area / ratio) ** 0.5
    w = h * ratio
    w, h = round16(w), round16(h)
    px = w * h
    if px < MIN_PIXELS or px > MAX_PIXELS_HARD:
        k = (MIN_PIXELS / px) ** 0.5 if px < MIN_PIXELS else (MAX_PIXELS_HARD / px) ** 0.5
        w, h = round16(w * k), round16(h * k)
    return f"{w}x{h}"


def fit_size(ratio, max_px=MAX_PIXELS_USER):
    """按宽高比撑到 max_px 像素上限以内（16 对齐，不越界）。返回 (WxH, w, h)"""
    h = (max_px / ratio) ** 0.5
    w = round16(h * ratio)
    h = round16(w / ratio)
    while w * h > max_px and w > 16:      # 取整可能刚好越界，逐格回退
        w -= 16
        h = round16(w / ratio)
    return f"{w}x{h}", w, h


def image_size(path):
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def prepare_image(path, max_side=2048, quality=88):
    """读图 → 压到 max_side、转 JPEG、压白底去 alpha → base64"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"参考图不存在：{p}")
    raw = p.read_bytes()
    if len(raw) > IMG_LIMIT:
        raise ValueError(f"参考图超过 30MB：{p}（{len(raw)/1048576:.1f}MB）")

    try:
        from PIL import Image
    except ImportError:
        return base64.b64encode(raw).decode(), "原样（未装 Pillow，未压缩）"

    with Image.open(p) as im:
        im.load()
        ow, oh = im.size
        scale = min(1.0, max_side / max(ow, oh))
        nw, nh = max(1, int(ow * scale)), max(1, int(oh * scale))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        if scale < 1.0:
            im = im.resize((nw, nh), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        note = f"{ow}x{oh} → {nw}x{nh} jpeg {len(data)/1024:.0f}KB"
        return base64.b64encode(data).decode(), note


# ---------------------------------------------------------------- 校验

def check_prompt(prompt):
    if not prompt or not prompt.strip():
        raise ValueError("prompt 为空")
    n = len(prompt)
    if n > PROMPT_HARD_LIMIT:
        raise ValueError(f"prompt {n} 字符，超过接口硬上限 {PROMPT_HARD_LIMIT}，请精简")
    if cjk_ratio(prompt) > 0.3 and n > PROMPT_SOFT_LIMIT_CJK:
        warn(f"prompt {n} 字，中文建议 ≤{PROMPT_SOFT_LIMIT_CJK} 字，过长会丢细节（不阻断）")
    return n


def check_size(size, ref_ratio=None, max_px=MAX_PIXELS_USER, allow_big=False):
    """校验并规范化 size；返回 (size字符串, 说明)

    两条默认规矩（用户 2026-11-19 定）：
      1. 不指定尺寸时一律 3:4 竖构图；
      2. 总像素不超过 260 万（MAX_PIXELS_USER），除非显式 --allow-big。
    """
    cap = MAX_PIXELS_HARD if allow_big else max_px
    if not size:
        wh, w, h = fit_size(DEFAULT_RATIO, cap)
        return wh, f"默认 3:4 竖构图（{w*h/1e4:.0f} 万像素）"
    s = str(size).strip()

    if s.lower() == "auto":
        if ref_ratio is None:
            wh, w, h = fit_size(DEFAULT_RATIO, cap)
            return wh, f"auto→无参考图，按 3:4 兜底（{w*h/1e4:.0f} 万像素）"
        area = min(AUTO_AREA, cap)
        return size_from_ratio(ref_ratio, area), f"auto→按参考图比例 {ref_ratio:.3f}"

    if s in RATIOS:
        wh, w, h = fit_size(RATIOS[s], cap)
        return wh, f"比例 {s}（{w*h/1e4:.0f} 万像素）"

    if s.upper() in ("1K", "1.5K", "2K"):
        tag = s.upper()
        if tag == "2K" and not allow_big:
            wh, _, _ = fit_size(DEFAULT_RATIO, cap)
            raise ValueError(
                f"2K 档位由模型自定比例且像素通常超 260 万，与默认规矩冲突。"
                f"改用显式尺寸（默认 3:4 = {wh}），确实要 2K 请加 --allow-big")
        warn(f"档位 {tag} 的比例由模型自行判断，与默认 3:4 不保证一致")
        return tag, f"档位 {tag}（比例由模型按提示词判断）"

    m = re.fullmatch(r"(\d+)\s*[xX*×]\s*(\d+)", s)
    if not m:
        raise ValueError(f"size 无法识别：{s}（用 auto/3:4/4:3/16:9/9:16/1:1/宽x高）")
    w, h = int(m.group(1)), int(m.group(2))
    px = w * h
    if not (MIN_PIXELS <= px <= MAX_PIXELS_HARD):
        raise ValueError(f"size {w}x{h} 总像素 {px} 越界，需在 {MIN_PIXELS}~{MAX_PIXELS_HARD}")
    if px > cap:
        sug, sw, sh = fit_size(w / h, cap)
        raise ValueError(
            f"size {w}x{h} 共 {px/1e4:.0f} 万像素，超过默认上限 {cap/1e4:.0f} 万。"
            f"同比例改小到 {sug}（{sw*sh/1e4:.0f} 万）即可；"
            f"确实要大图请加 --allow-big")
    r = w / h
    if not (1 / 16 <= r <= 16):
        raise ValueError(f"宽高比 {r:.2f} 越界，需在 1/16~16")
    if w % 16 or h % 16:
        warn(f"{w}x{h} 不是 16 的倍数，建议 {round16(w)}x{round16(h)}")
    return f"{w}x{h}", f"显式像素（{px/1e4:.0f} 万）"


# ---------------------------------------------------------------- 调用

def build_payload(prompt, images_b64, size, model, output_format, watermark,
                  img_enc="dataurl"):
    p = {
        "model": model,
        "prompt": prompt,
        "watermark": bool(watermark),
        "response_format": "url",
    }
    if images_b64:
        if img_enc == "dataurl":
            p["images"] = [f"data:image/jpeg;base64,{b}" for b in images_b64]
        else:
            p["images"] = list(images_b64)
    if size:
        p["size"] = size
    if output_format:
        p["output_format"] = output_format
    return p


def api_call(payload, key, timeout=240, retries=3):
    body = json.dumps(payload).encode()
    if len(body) > BODY_LIMIT:
        raise ValueError(
            f"请求体 {len(body)/1048576:.1f}MB 超过 20MB 上限，"
            f"请降低 --max-side 或减少参考图")
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            API_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        retryable = False
        try:
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            tok = (data.get("tokenhub_usage") or {}).get("total_tokens", "?")
            print(f"  ✓ {time.time()-t0:.1f}s  tokens={tok}")
            return data
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                err = json.loads(raw).get("error", {})
            except Exception:
                err = {"message": raw[:400]}
            code = str(err.get("code", ""))
            msg = err.get("message_zh") or err.get("message") or raw[:300]
            if code == ERR_BILLING:
                raise SystemExit(
                    "✗ 账号无可用额度且未开启后付费，接口不可用。\n"
                    "  到控制台开通后付费后重试：\n"
                    "  https://console.cloud.tencent.com/tokenhub/inference")
            if code == ERR_MODEL:
                raise SystemExit(
                    f"✗ 该 Key 不允许用此模型：{payload['model']}\n"
                    f"  白名单里目前只有 seedream-image-v5.5，别换 flash/lite。")
            last = f"HTTP {e.code} [{code}] {msg}"
            retryable = e.code in (429, 500, 502, 503, 504)
        except urllib.error.URLError as e:
            last, retryable = f"网络错误 {e.reason}", True
        except TimeoutError:
            last, retryable = "请求超时", True
        if not retryable or attempt == retries:
            break
        wait = 2 ** attempt
        warn(f"{last} → {wait}s 后重试（{attempt}/{retries}）")
        time.sleep(wait)
    raise RuntimeError(last or "未知错误")


def download(url, out_path, timeout=120, retries=3):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                out_path.write_bytes(r.read())
            return out_path
        except Exception as e:
            if attempt == retries:
                raise RuntimeError(f"下载失败（链接 12 小时过期）：{e}\n{url}")
            time.sleep(2 * attempt)


def save_results(data, out_path):
    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"返回里没有图：{json.dumps(data, ensure_ascii=False)[:300]}")
    out_path = Path(out_path)
    if out_path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        out_path = out_path.with_suffix(".png")
    saved = []
    for i, it in enumerate(items, 1):
        p = out_path if len(items) == 1 else out_path.with_name(
            f"{out_path.stem}-{i}{out_path.suffix}")
        if it.get("b64_json"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(it["b64_json"]))
            saved.append(p)
        elif it.get("url"):
            saved.append(download(it["url"], p))
        else:
            warn("返回条目里既无 url 也无 b64_json，跳过")
    return saved


# ---------------------------------------------------------------- 单条执行

def run_one(job, defaults, key, out_path, max_side=2048):
    prompt = job["prompt"]
    n = check_prompt(prompt)
    model = job.get("model") or defaults["model"]
    imgs = job.get("images") or []
    if len(imgs) > MAX_IMAGES:
        raise ValueError(f"参考图 {len(imgs)} 张，超过上限 {MAX_IMAGES}")

    ratio = None
    if imgs:
        wh = image_size(imgs[0])
        if wh:
            ratio = wh[0] / wh[1]
    size, size_note = check_size(job.get("size") or defaults["size"], ratio,
                                 max_px=defaults.get("max_px", MAX_PIXELS_USER),
                                 allow_big=defaults.get("allow_big", False))

    imgs_b64, notes = [], []
    for ip in imgs:
        if not Path(ip).exists():
            raise FileNotFoundError(f"参考图不存在：{ip}")
        b64, note = prepare_image(ip, max_side=max_side)
        imgs_b64.append(b64)
        notes.append(f"{Path(ip).name}: {note}")

    payload = build_payload(prompt, imgs_b64, size, model,
                            defaults["format"], defaults["watermark"],
                            defaults["img_enc"])

    print(f"→ {job['id']}  {size}（{size_note}）  prompt {n}字  参考图 {len(imgs_b64)}张")
    for x in notes:
        print(f"    · {x}")

    if defaults["print_curl"]:
        short = dict(payload)
        if imgs_b64:
            short["images"] = [f"<base64 约{len(imgs_b64[0])//1024}KB/张，共{len(imgs_b64)}张>"]
        print("\ncurl -X POST '%s' \\" % API_URL)
        print("  -H 'Authorization: Bearer %s' \\" % mask_key(key))
        print("  -H 'Content-Type: application/json' \\")
        print("  -d '%s'\n" % json.dumps(short, ensure_ascii=False))
        return []

    # 参考图编码兜底：先 dataurl，被拒（InvalidParameter）再退裸 base64
    encs = [defaults["img_enc"]]
    if defaults["img_enc"] == "auto":
        encs = ["dataurl", "raw"]
    data = None
    for i, enc in enumerate(encs):
        if imgs_b64:
            payload = build_payload(prompt, imgs_b64, size, model,
                                    defaults["format"], defaults["watermark"], enc)
        try:
            data = api_call(payload, key, timeout=defaults["timeout"],
                            retries=defaults["retries"])
            break
        except RuntimeError as e:
            more = i + 1 < len(encs)
            if more and "InvalidParameter" in str(e):
                warn(f"参考图编码 {enc} 被拒，改用 {encs[i+1]} 重试")
                continue
            raise
    files = save_results(data, out_path)
    for f in files:
        print(f"  → {f}")
    return files


# ---------------------------------------------------------------- 清单

def load_manifest(path):
    mp = Path(path)
    if not mp.exists():
        die(f"清单不存在：{mp}")
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"清单不是合法 JSON：{e}")
    base = mp.resolve().parent

    def resolve(v):
        p = Path(v)
        return str(p if p.is_absolute() else (base / p).resolve())

    refs = {k: resolve(v) for k, v in (m.get("refs") or {}).items()}
    jobs = []
    for j in m.get("jobs", []):
        if not j.get("id") or not j.get("prompt"):
            die(f"job 缺 id 或 prompt：{j}")
        imgs = []
        for it in j.get("images", []):
            imgs.append(refs[it] if it in refs else resolve(it))
        jobs.append({"id": j["id"], "prompt": j["prompt"], "images": imgs,
                     "size": j.get("size"), "model": j.get("model")})
    if not jobs:
        die("清单里没有 jobs")
    return m, jobs


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(
        description="Seedream v5.5 生图（TokenHub）· 户型图 → 装修效果图",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    g = ap.add_argument_group("输入")
    g.add_argument("--prompt", help="提示词")
    g.add_argument("--prompt-file", help="从文件读提示词（推荐，免转义）")
    g.add_argument("--image", action="append", default=[], help="参考图，可重复")
    g.add_argument("--manifest", help="批量清单 JSON")
    g.add_argument("--only", help="批量时只跑指定 id（逗号分隔）")

    g = ap.add_argument_group("输出")
    g.add_argument("--out", help="单张输出路径")
    g.add_argument("--outdir", help="批量输出目录（默认用清单的 outdir）")

    g = ap.add_argument_group("参数")
    g.add_argument("--size", default="",
                   help="默认 3:4（1392x1856，258 万像素）；可选 auto/3:4/4:3/16:9/9:16/1:1/宽x高")
    g.add_argument("--max-pixels", type=int, default=MAX_PIXELS_USER,
                   help=f"总像素上限，默认 {MAX_PIXELS_USER}（260 万）")
    g.add_argument("--allow-big", action="store_true",
                   help="解除 260 万像素上限（仅在你明确要求大图时用）")
    g.add_argument("--model", default=DEFAULT_MODEL)
    g.add_argument("--format", default="png", choices=["png", "jpeg"])
    g.add_argument("--watermark", action="store_true", help="加水印（默认不加）")
    g.add_argument("--key", default=None,
                   help="临时指定 API Key（优先级最高；长期配置请用 --set-key）")
    g.add_argument("--set-key", metavar="KEY",
                   help="把 API Key 写入本地配置后退出（推荐，一次搞定）")
    g.add_argument("--show-key", action="store_true",
                   help="显示当前生效的 Key 来源（脱敏）后退出")
    g.add_argument("--max-side", type=int, default=2048, help="参考图长边上限")
    g.add_argument("--img-enc", default="auto", choices=["auto", "dataurl", "raw"],
                   help="参考图 Base64 编码方式：auto 先 data:image/jpeg;base64, 前缀，被拒再退裸 base64")
    g.add_argument("--parallel", type=int, default=3, help="批量并发数")
    g.add_argument("--timeout", type=int, default=240)
    g.add_argument("--retries", type=int, default=3)
    g.add_argument("--validate-only", action="store_true", help="只校验不发请求")
    g.add_argument("--print-curl", action="store_true", help="只打印请求不发送")
    a = ap.parse_args()

    # ---------- API Key
    if a.set_key:
        k = a.set_key.strip()
        save_key(k)
        print(f"✓ API Key 已写入 {CONFIG_FILE}")
        print(f"  当前生效：{mask_key(k)}")
        if not k.startswith("sk-"):
            print("  ⚠ TokenHub 的 Key 通常以 sk- 开头，请确认没贴错。")
        print("  以后直接跑出图命令即可，不用再带 --key。")
        return

    api_key = resolve_key(a.key)

    if a.show_key:
        src = ("--key 参数" if a.key else
               f"环境变量 {API_KEY_ENV}" if os.environ.get(API_KEY_ENV) else
               f"配置文件 {CONFIG_FILE}" if load_saved_key() else "未配置")
        print(f"Key：{mask_key(api_key)}    来源：{src}")
        if api_key and not api_key.startswith("sk-"):
            print("⚠ TokenHub 的 Key 通常以 sk- 开头，请确认。")
        return

    if not api_key:
        if a.validate_only or a.print_curl:
            print("! 未配置 API Key：本次只做校验/打印请求，真要出图请先 --set-key\n")
        else:
            print(KEY_HELP.format(config=CONFIG_FILE))
            sys.exit(2)

    defaults = {"model": a.model, "size": a.size, "format": a.format,
                "watermark": a.watermark, "print_curl": a.print_curl,
                "timeout": a.timeout, "retries": a.retries,
                "img_enc": a.img_enc,
                "max_px": a.max_pixels, "allow_big": a.allow_big}

    # ---------- 批量
    if a.manifest:
        m, jobs = load_manifest(a.manifest)
        if m.get("model") and a.model == DEFAULT_MODEL:
            defaults["model"] = m["model"]
        if m.get("size") and not a.size:
            defaults["size"] = m["size"]
        if m.get("format") and a.format == "png":
            defaults["format"] = m["format"]
        if a.only:
            keep = {s.strip() for s in a.only.split(",") if s.strip()}
            jobs = [j for j in jobs if j["id"] in keep]
            if not jobs:
                die(f"--only 没匹配到任何 job：{a.only}")
        outdir = Path(a.outdir) if a.outdir else (
            Path(a.manifest).parent / m.get("outdir", "渲染"))

        print(f"清单 {len(jobs)} 个 job · 并发 {a.parallel} · 输出 → {outdir}")

        if a.validate_only:
            bad = 0
            for j in jobs:
                try:
                    check_prompt(j["prompt"])
                    for ip in j["images"]:
                        if not Path(ip).exists():
                            raise FileNotFoundError(f"参考图不存在：{ip}")
                    ratio = None
                    if j["images"]:
                        wh = image_size(j["images"][0])
                        if wh:
                            ratio = wh[0] / wh[1]
                    size, note = check_size(j.get("size") or defaults["size"], ratio,
                                            max_px=defaults["max_px"],
                                            allow_big=defaults["allow_big"])
                    print(f"  ✓ {j['id']}  {size}（{note}）")
                except Exception as e:
                    bad += 1
                    print(f"  ✗ {j['id']}: {e}")
            print(f"\n校验完成：{len(jobs)-bad} 通过 / {bad} 有问题（未发请求，不计费）")
            return

        if a.print_curl:
            run_one(jobs[0], defaults, api_key, outdir / f"{jobs[0]['id']}.png", a.max_side)
            return

        fails = []
        with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
            futs = {ex.submit(run_one, j, defaults, api_key,
                              outdir / f"{j['id']}.png", a.max_side): j
                    for j in jobs}
            for f in as_completed(futs):
                j = futs[f]
                try:
                    f.result()
                except SystemExit:
                    raise
                except BaseException as e:
                    msg = str(e) or e.__class__.__name__
                    fails.append((j["id"], msg))
                    print(f"  ✗ {j['id']}: {msg}")
        print(f"\n完成 {len(jobs)-len(fails)}/{len(jobs)}")
        if fails:
            print("失败项（可用 --only 单独重跑）：")
            for i, msg in fails:
                print(f"  · {i}: {msg}")
            sys.exit(2)
        return

    # ---------- 单张
    prompt = a.prompt
    if a.prompt_file:
        prompt = Path(a.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        die("需要 --prompt / --prompt-file / --manifest 之一")

    out_path = Path(a.out) if a.out else Path("out") / (time.strftime("render_%H%M%S") + ".png")
    job = {"id": Path(out_path).stem, "prompt": prompt, "images": a.image,
           "size": a.size, "model": a.model}

    if a.validate_only:
        n = check_prompt(prompt)
        for ip in job["images"]:
            if not Path(ip).exists():
                die(f"参考图不存在：{ip}")
        ratio = None
        if job["images"]:
            wh = image_size(job["images"][0])
            if wh:
                ratio = wh[0] / wh[1]
        size, note = check_size(a.size, ratio, max_px=a.max_pixels, allow_big=a.allow_big)
        print(f"✓ prompt {n} 字 · 参考图 {len(job['images'])} 张 · size={size}（{note}）")
        return

    run_one(job, defaults, api_key, out_path, a.max_side)


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:          # 尺寸越界 / 提示词超限：给干净提示，不甩 traceback
        die(str(e), 2)
