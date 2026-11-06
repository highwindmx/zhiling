"""单帧抠像预览：不重跑整段，改参数后 1 秒内看效果。

用法（在 deskpet-python 目录下）：
  .venv\\Scripts\\python.exe build\\tools\\preview_key.py --clip dsh_rice --frame 62
  .venv\\Scripts\\python.exe build\\tools\\preview_key.py --clip dsh_rice --frame 62 --despill 1.0 --sat_lo 0.06
  .venv\\Scripts\\python.exe build\\tools\\preview_key.py --clip dsh_rice --frame 62 --raw build\\cache\\tmp\\rice_raw\\r0062.png

参数默认取 packs\\<packId>\\manifest.json 里该段的 keyer；命令行给的同名参数覆盖它。
输出：build\\cache\\probe\\preview\\<clipId>_f####.png（棋盘格合成图，1024 宽拼接原图/结果）
      并打印该帧的定量指标（可见绿 / 绿雾 / 亮白被啃 / 边框 alpha）。
"""
import argparse
import glob
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
REPO = os.path.dirname(ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "workshop"))   # keyer 已迁到 workshop/
from keyer import key_frame  # noqa: E402
import pack as packlib       # noqa: E402 - 素材目录统一解析（deskpet-python / 仓库根 / 用户目录）

KEYS = ("hue_ref", "hue_in", "hue_out", "sat_lo", "sat_hi", "val_lo", "val_hi",
        "erode", "blur", "floor", "despill", "keep_frac")


def load_entry(pack_id, clip):
    mf = packlib.manifest_path(pack_id)
    if not os.path.exists(mf):
        raise SystemExit("找不到 manifest：%s（素材目录见 packlib.packs_root()）" % mf)
    data = json.load(open(mf, encoding="utf-8"))
    for c in data["clips"]:
        if c["clipId"] == clip:
            return data, c
    raise SystemExit("pack %s 里没有 %s" % (pack_id, clip))


def find_video(name):
    for p in glob.glob(os.path.join(REPO, "material", "**", os.path.basename(name)), recursive=True):
        return p
    raise SystemExit("找不到素材视频：%s（在 material\\ 下搜）" % name)


def grab_frame(video, fps, idx, dst):
    """按时间抽一帧（不整段解码），落在该帧中点，避免边界偏差。"""
    ff = None
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ff = "ffmpeg"
    t = (idx + 0.5) / max(1e-6, float(fps))
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-ss", "%.4f" % t,
           "-i", video, "-frames:v", "1", "-y", dst]
    subprocess.run(cmd, check=True)
    return dst


def metrics(src_rgb, rgba, hue_ref):
    a = np.asarray(rgba)[..., 3]
    rgb = np.asarray(rgba)[..., :3]
    hsv = np.asarray(Image.fromarray(rgb).convert("HSV"))
    h = hsv[..., 0].astype(np.int32) * 360 // 255
    s = hsv[..., 1].astype(np.float32) / 255
    dh = np.abs(((h - hue_ref + 180) % 360) - 180)
    vis = a >= 60
    haze = (a > 16) & (a < 200)
    hsv0 = np.asarray(Image.fromarray(np.asarray(src_rgb)).convert("HSV"))
    s0 = hsv0[..., 1].astype(np.float32) / 255
    v0 = hsv0[..., 2].astype(np.float32) / 255
    white = (v0 > 0.88) & (s0 < 0.10)
    eaten = float((white & (a < 128)).sum()) / max(1, int(white.sum())) * 100
    border = np.concatenate([a[:6].ravel(), a[-6:].ravel(), a[:, :6].ravel(), a[:, -6:].ravel()])
    return dict(
        visible_green=float((vis & (dh < 25) & (s > 0.12)).mean() * 100),
        green_haze=float((haze & (dh < 25) & (s > 0.05)).mean() * 100),
        white_eaten=eaten,
        border_alpha=float(border.mean()),
        fg_pct=float((a > 40).mean() * 100),
    )


def checker(w, h, s=12):
    y, x = np.mgrid[0:h, 0:w]
    c = (((x // s) + (y // s)) % 2).astype(np.float32)
    v = np.where(c[..., None] > 0, 210.0, 240.0) / 255.0
    return np.repeat(v, 3, axis=2)


def comp(rgba):
    im = np.asarray(rgba).astype(np.float32) / 255.0
    bg = checker(im.shape[1], im.shape[0])
    out = np.clip(im[..., :3] * im[..., 3:] + bg * (1 - im[..., 3:]), 0, 1) * 255
    return out.astype(np.uint8)


def main():
    ap = argparse.ArgumentParser(description="单帧抠像预览（改参数不重跑整段）")
    ap.add_argument("--pack", default="maid-dsh")
    ap.add_argument("--clip", required=True, help="manifest 里的 clipId，如 dsh_rice")
    ap.add_argument("--frame", type=int, default=-1, help="帧号（默认取中间帧）")
    ap.add_argument("--raw", default="", help="已有原始帧 png（跳过抽帧）")
    for k in KEYS:
        ap.add_argument("--" + k, type=float if k not in ("erode",) else int, default=None)
    a = ap.parse_args()

    manifest, entry = load_entry(a.pack, a.clip)
    crop = entry.get("crop")
    fps = entry.get("fps", 24)
    idx = a.frame if a.frame >= 0 else int(entry.get("count", 1) // 2)

    tmp = os.path.join(ROOT, "build", "cache", "tmp")
    os.makedirs(tmp, exist_ok=True)
    if a.raw:
        src = a.raw
    else:
        name = entry.get("source") or (a.clip + ".mp4")
        src = grab_frame(find_video(name), fps, idx, os.path.join(tmp, "preview_src.png"))

    im = Image.open(src).convert("RGB")
    if crop:
        im = im.crop(tuple(int(v) for v in crop))
    params = {k: json.loads(v) for k, v in entry["keyer"].items()}
    for k in KEYS:
        v = getattr(a, k)
        if v is not None:
            params[k] = float(v) if k != "erode" else int(v)

    rgba = key_frame(np.asarray(im), **params)
    out_dir = os.path.join(ROOT, "build", "cache", "probe", "preview")
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, "%s_f%04d.png" % (a.clip, idx))
    ab = np.concatenate([np.asarray(im), comp(rgba)], axis=1)
    Image.fromarray(ab).save(dst)

    m = metrics(im, rgba, float(params.get("hue_ref", 135)))
    print("参数: " + " ".join("%s=%s" % (k, params.get(k)) for k in KEYS))
    print("裁剪: %s 帧: %d/%d 输出: %s" % (crop, idx, entry.get("count"), dst))
    print("可见绿 %.3f%%（阈值 ≤0.30）| 绿雾 %.2f%%（阈值 ≤5）| 亮白被啃 %.2f%%（阈值 ≤1.0）"
          "| 边框alpha %.1f（阈值 ≤5）| 前景 %.1f%%"
          % (m["visible_green"], m["green_haze"], m["white_eaten"],
             m["border_alpha"], m["fg_pct"]))


if __name__ == "__main__":
    main()
