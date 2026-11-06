"""默认值一次跑通验收：得意状（30fps）与炸毛（黑边+绿幕）。

只用工坊的默认值：default_params()（hue_ref=136 + 高级项默认）、自动裁黑边 ON、
24fps、宽 576、webp q80。走同一条 build_frames.build_clip 路径，输出到 build/_accept_out/，
不污染 packs/dsh。
"""
import glob, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image
import build_frames
import workshop

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "..", "cache", "tmp", "_accept_out")
CASES = [("得意状", "accept_proud"), ("炸毛", "accept_shock")]


def qc(out_dir, hue):
    """残留绿边 %：alpha 内、色相贴近背景色、饱和度够高的像素占比。"""
    fs = sorted(glob.glob(os.path.join(out_dir, "f*.webp")))
    if not fs:
        return 0.0, 0.0
    step = max(1, len(fs) // 8)
    vals = []
    for p in fs[::step]:
        im = Image.open(p).convert("RGBA")
        a = np.asarray(im)[..., 3]
        hsv = np.asarray(im.convert("HSV"))
        hh = hsv[..., 0].astype(np.int32) * 360 // 255
        ss = hsv[..., 1].astype(np.float32) / 255
        fg = a > 40
        if fg.any():
            dh = np.abs(((hh - hue + 180) % 360) - 180)
            vals.append(float(((dh < 30) & (ss > 0.3) & fg).sum()) / fg.sum() * 100)
    return (float(np.median(vals)) if vals else 0.0), (max(vals) if vals else 0.0)


def run(name, cid):
    src = os.path.join(workshop.REPO, "material", "DSMaid", name + ".mp4")
    if not os.path.exists(src):
        print("跳过 %s：找不到 %s" % (name, src))
        return
    t_all = time.monotonic()
    probe = os.path.join(BASE, "..", "cache", "probe", cid)
    for f in glob.glob(os.path.join(probe, "*.png")):
        os.remove(f)
    t0 = time.monotonic()
    pfiles = build_frames.extract_frames(src, probe, 1)
    crop, detail = workshop.detect_crop(pfiles)
    t_crop = time.monotonic() - t0
    src_fps = build_frames.probe_src_fps(src)
    # 四角取样只做记录：默认流程不会自动应用它
    rgb = np.asarray(Image.open(pfiles[len(pfiles) // 2]).convert("RGB")).astype(np.float32) / 255.0
    hue4, sat4 = workshop.sample_hue_from(rgb)

    spec = {"id": cid, "video": src, "params": workshop.default_params(),
            "fps": 24, "width": 576, "fmt": "webp", "quality": 80,
            "crop": list(crop) if crop else None,
            "out_dir": os.path.join(OUT, cid)}
    logs = []
    t0 = time.monotonic()
    info = build_frames.build_clip(spec, log=lambda s: logs.append(s))
    t_gen = time.monotonic() - t0
    resid, resid_max = qc(info["out_dir"], workshop.HUE_DEFAULT)
    print("=== %s (%s) ===" % (name, cid))
    print("  源片 %.2ffps → 24fps%s" % (src_fps or 0, "（重采样）" if src_fps and abs(src_fps - 24) > 0.5 else ""))
    print("  裁边 %s → crop=%s（%.1fs）" % (detail, list(crop) if crop else None, t_crop))
    print("  四角取样：色相 %.1f° 饱和 %.2f（默认这次没有应用，hue_ref=%.0f）" % (hue4, sat4, workshop.HUE_DEFAULT))
    print("  生成：%d 帧 %d×%d %.2f MB · scale %.3f offset %s · %.1fs"
          % (info["count"], info["width"], info["height"], info["bytes"] / 1e6,
             info["scale"], info["offset"], t_gen))
    print("  质检：残留绿 %.2f%%（最大 %.2f%%）· 脚底锚点 y=%d" % (resid, resid_max, info["foot"][1]))
    print("  总耗时 %.1fs" % (time.monotonic() - t_all))
    for s in logs:
        print("    " + s)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, cid in CASES:
        run(name, cid)
