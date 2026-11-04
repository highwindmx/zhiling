"""生成两段素材的透明帧序列：待机(idle) + 动作(action)，构建期就缩到最终显示尺寸上限。"""
import glob, json, os, subprocess, sys
import numpy as np
from PIL import Image
from scipy import ndimage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keyer import key_frame
import imageio_ffmpeg

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
OUT_W = 576                      # 磁盘上一档：大档宽度；运行时再按所选档位缩小
OUT_FMT = "webp"                 # 输出格式：webp(有损+alpha，约为 png 的 1/7) / png
WEBP_Q = 80                      # 有损质量；q80 在 576px 下与 png 肉眼无差
ALPHA_Q = 80                     # alpha 平面质量，100 会让 method=6 慢 20 倍
IDLE_SRC = os.path.join(ROOT, "assets",
                        "jimeng-2026-11-04-5883-帮我生成桌面桌宠，绿幕背景，方便进行透明视频转码.mp4")
ACT_SRC = os.path.join(ROOT, "assets", "HappyHorse 1.1_1793774480874.mp4")

IDLE_P = dict(hue_in=18, hue_out=36, sat_lo=0.10, sat_hi=0.16, erode=1, blur=1.0,
              keep_frac=1.0, floor=0.16, despill=0.6)
ACT_P = dict(hue_ref=148.0, hue_in=20.0, hue_out=34.0, sat_lo=0.30, sat_hi=0.55,
             val_lo=0.30, val_hi=0.45, erode=1, blur=1.0, floor=0.16, despill=0.75,
             keep_frac=1.0, watermarks=[(0, 0, 230, 90), (1080, 630, 1280, 720)])
# 动作段缩放：头部白区宽比 1.357、可见高度比 1.228。取 1.30，并把画布顶部加高 30px，
# 否则放大后的呆毛会顶到画布上沿被切掉
ACT_SCALE = 1.26
CANVAS_H = 760                   # 画布高度：原 720 + 顶部 30px 透明余量
FOOT = (414, 759)                # 画布内脚底锚点
CROP = (284, 0, 1110, 720)       # 原图裁切范围，宽 827

ACTION_RAW = os.path.join(BASE, "action_raw")
IDLE_RAW = os.path.join(BASE, "f24")
ff = imageio_ffmpeg.get_ffmpeg_exe()


def extract(src, dst, prefix):
    os.makedirs(dst, exist_ok=True)
    if glob.glob(os.path.join(dst, "*.png")):
        return sorted(glob.glob(os.path.join(dst, "*.png")))
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-vf", "fps=24", "-pix_fmt", "rgb24",
                    os.path.join(dst, prefix + "%04d.png")], check=True)
    return sorted(glob.glob(os.path.join(dst, "*.png")))


def resize_rgba(im, size):
    """带 alpha 的高质量缩放：先把颜色乘上 alpha，避免边缘出现暗边。"""
    a = np.asarray(im).astype(np.float32)
    al = a[..., 3:4] / 255.0
    pm = np.concatenate([a[..., :3] * al, a[..., 3:4]], axis=2)
    s = np.asarray(Image.fromarray(pm.astype(np.uint8), "RGBA").resize(size, Image.LANCZOS)).astype(np.float32)
    a2 = np.maximum(s[..., 3:4] / 255.0, 1e-3)
    rgb = np.clip(s[..., :3] / a2, 0, 255)
    return Image.fromarray(np.concatenate([rgb, s[..., 3:4]], 2).astype(np.uint8), "RGBA")


def clean_largest(rgba):
    a = np.asarray(rgba)[:, :, 3]
    lab, n = ndimage.label(a > 24)
    if n > 1:
        areas = ndimage.sum(a > 24, lab, range(1, n + 1))
        a = np.where(lab == (int(np.argmax(areas)) + 1), a, 0).astype(np.uint8)
        rgba.putalpha(Image.fromarray(a, "L"))
    return rgba


def key_clip(files, params):
    return [clean_largest(key_frame(np.asarray(Image.open(p).convert("RGB")), **params))
            for p in files]


def save_clip(frames, out_dir, canvas_size):
    if os.path.isdir(out_dir):
        for f in glob.glob(os.path.join(out_dir, "*.png")) + glob.glob(os.path.join(out_dir, "*.webp")):
            os.remove(f)
    os.makedirs(out_dir, exist_ok=True)
    w = OUT_W
    h = round(canvas_size[1] * w / canvas_size[0])
    for i, im in enumerate(frames):
        p = os.path.join(out_dir, f"f{i:04d}.{OUT_FMT}")
        if OUT_FMT == "webp":
            resize_rgba(im, (w, h)).save(p, "WEBP", quality=WEBP_Q, method=6,
                                         alpha_quality=ALPHA_Q, exact=False)
        else:
            resize_rgba(im, (w, h)).save(p, optimize=True)
    return w, h


def main():
    idle_files = extract(IDLE_SRC, IDLE_RAW, "r")
    act_files = extract(ACT_SRC, ACTION_RAW, "a")
    print("待机帧", len(idle_files), "动作帧", len(act_files))

    idle_keyed = key_clip(idle_files, IDLE_P)
    act_keyed = key_clip(act_files, ACT_P)

    cw, ch = CROP[2] - CROP[0], CANVAS_H
    # 待机段：原裁切图贴到画布底部（顶部让出 30px 透明余量，与动作段共用同一套锚点）
    idle_canvas = []
    for im in idle_keyed:
        c = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        c.alpha_composite(im.crop(CROP), (0, ch - (CROP[3] - CROP[1])))
        idle_canvas.append(c)

    # 动作段：按 1.30 缩放后贴到同一张画布，脚底对齐 FOOT
    canvas = []
    for im in act_keyed:
        c = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        s = im.resize((round(im.width * ACT_SCALE), round(im.height * ACT_SCALE)), Image.LANCZOS)
        # 动作段角色脚底在缩放前 y=642、中心 x=617.5
        ox = round(FOOT[0] - 617.5 * ACT_SCALE)
        oy = round(FOOT[1] - 642 * ACT_SCALE)
        c.alpha_composite(s, (ox, oy))
        canvas.append(c)

    w, h = save_clip(idle_canvas, os.path.join(ROOT, "frames_idle"), (cw, ch))
    save_clip(canvas, os.path.join(ROOT, "frames_action"), (cw, ch))
    print("输出尺寸", w, "x", h)

    meta = {
        "canvas": {"w": w, "h": h, "src_w": cw, "src_h": ch, "crop": list(CROP)},
        "clips": {
            "idle": {"dir": "frames_idle", "fps": 24, "count": len(idle_canvas), "format": OUT_FMT,
                     "foot": list(FOOT), "source": os.path.basename(IDLE_SRC),
                     "keyer": {k: str(v) for k, v in IDLE_P.items()}},
            "action": {"dir": "frames_action", "fps": 24, "count": len(canvas), "format": OUT_FMT,
                       "scale": ACT_SCALE, "offset": [round(FOOT[0] - 617.5 * ACT_SCALE),
                                                      round(FOOT[1] - 642 * ACT_SCALE)],
                       "foot": list(FOOT), "source": os.path.basename(ACT_SRC),
                       "keyer": {k: str(v) for k, v in ACT_P.items()}},
        },
    }
    with open(os.path.join(ROOT, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    total = sum(os.path.getsize(os.path.join(ROOT, d, x)) for d in ("frames_idle", "frames_action")
                for x in os.listdir(os.path.join(ROOT, d)))
    print("帧总大小 %.1f MB（%s）" % (total / 1e6, OUT_FMT))


if __name__ == "__main__":
    main()

