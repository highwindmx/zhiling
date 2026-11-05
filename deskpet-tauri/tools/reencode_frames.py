#!/usr/bin/env python3
"""把 extract.py 产出的无损 WebP 帧重编码为「统一画布 + 有损 WebP」帧序列。

extract.py 默认 lossless=True 且保留原始裁切尺寸（约 1000x712），577 帧合计
约 177MB，会被 Tauri 整个塞进 exe。本脚本做三件事：

- 统一画布 640x456（与待机段 1000x712 同比例），各段等比缩放后底边居中贴合，
  消除切换动作时人物大小/位置的跳变；
- 有损 WebP q82（带 alpha），与 deskpet-mini 的 q80 画质策略对齐；
- 输出新的 clips.json（附 canvas / size / quality 字段）。

只写 frontend/assets/（最终运行资源），无损原帧留在 archive/raw-lossless-frames/assets/
供对照；重跑本脚本是幂等的完整重建。
"""
import json
import os
import time

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "archive", "raw-lossless-frames", "assets")
DST = os.path.join(ROOT, "frontend", "assets")

CANVAS = (640, 456)
QUALITY = 82
METHOD = 4


def fit_to_canvas(im):
    """等比缩放，底边贴齐 + 水平居中，透明填充到统一画布。"""
    scale = min(CANVAS[0] / im.width, CANVAS[1] / im.height)
    nw = max(1, round(im.width * scale))
    nh = max(1, round(im.height * scale))
    if (nw, nh) != im.size:
        im = im.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    out.paste(im, ((CANVAS[0] - nw) // 2, CANVAS[1] - nh), im)
    return out


def main():
    if not os.path.isfile(os.path.join(SRC, "clips.json")):
        raise SystemExit(f"找不到无损原帧：{SRC}\n请先运行 python build/extract.py")
    with open(os.path.join(SRC, "clips.json"), encoding="utf-8") as f:
        raw = json.load(f)
    # 兼容 {id: clip} 与 {canvas, clips:{...}} 两种输入
    clips = raw.get("clips", raw) if isinstance(raw, dict) else raw

    os.makedirs(DST, exist_ok=True)
    total_before = total_after = 0
    t0 = time.time()

    for cid, clip in sorted(clips.items(), key=lambda kv: kv[1].get("priority", 99)):
        sdir = os.path.join(SRC, clip["dir"])
        ddir = os.path.join(DST, clip["dir"])
        os.makedirs(ddir, exist_ok=True)
        before = after = 0
        for i in range(clip["count"]):
            name = f"f{i:04d}.webp"
            src = os.path.join(sdir, name)
            dst = os.path.join(ddir, name)
            before += os.path.getsize(src)
            with Image.open(src) as raw:
                frame = fit_to_canvas(raw.convert("RGBA"))
                frame.save(dst, "WEBP", quality=QUALITY, method=METHOD)
            after += os.path.getsize(dst)
        total_before += before
        total_after += after
        clip["size"] = list(CANVAS)
        clip["quality"] = QUALITY
        clip["canvas"] = list(CANVAS)
        print(f"{cid:6s} {clip['count']:4d} frames  {before/1e6:7.2f} MB -> {after/1e6:6.2f} MB", flush=True)

    out = {
        "canvas": list(CANVAS),
        "quality": QUALITY,
        "clips": clips,
    }
    with open(os.path.join(DST, "clips.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\nTOTAL {total_before/1e6:.2f} MB -> {total_after/1e6:.2f} MB "
          f"({total_after/total_before*100:.1f}%), {time.time()-t0:.1f}s")
    print(f"OUT {DST}")


if __name__ == "__main__":
    main()
