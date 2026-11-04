"""判断新素材能不能接：抽 3 帧、拼对比图、量角色高度占比与背景色。"""
import os, sys, glob, subprocess
import numpy as np
from PIL import Image, ImageDraw
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imageio_ffmpeg

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
NEW = os.path.join(ROOT, "assets", "HappyHorse 1.1_1793774480874.mp4")
RAW = os.path.join(BASE, "action_raw")
IDLE_RAW = os.path.join(BASE, "f24")
os.makedirs(RAW, exist_ok=True)

ff = imageio_ffmpeg.get_ffmpeg_exe()
if not glob.glob(os.path.join(RAW, "*.png")):
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", NEW,
                    "-vf", "fps=24", "-pix_fmt", "rgb24",
                    os.path.join(RAW, "a%04d.png")], check=True)

a_files = sorted(glob.glob(os.path.join(RAW, "*.png")))
i_files = sorted(glob.glob(os.path.join(IDLE_RAW, "*.png")))
print("旧素材帧数", len(i_files), " 新素材帧数", len(a_files))


def fg_bbox(img):
    """粗略前景框：去绿（色相 60~180 且饱和>0.12 判为背景）"""
    a = np.asarray(img.convert("RGB")).astype(np.float32) / 255.0
    mx, mn = a.max(2), a.min(2)
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    d = mx - mn + 1e-6
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    hue = np.where(mx == r, ((g - b) / d) % 6,
                   np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    bg = (hue > 60) & (hue < 180) & (s > 0.12)
    fg = ~bg
    ys, xs = np.where(fg)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def bg_hsv(img):
    a = np.asarray(img.convert("RGB")).astype(np.float32) / 255.0
    corners = np.concatenate([a[:40, :40].reshape(-1, 3), a[:40, -40:].reshape(-1, 3)])
    med = np.median(corners, axis=0)
    mx, mn = med.max(), med.min()
    s = (mx - mn) / max(mx, 1e-6)
    d = mx - mn + 1e-6
    r, g, b = med
    hue = ((g - b) / d % 6 if mx == r else (b - r) / d + 2) * 60
    return tuple(int(round(x * 255)) for x in med), round(float(hue), 1), round(float(s), 3), round(float(mx), 3)


picks_i = [i_files[0], i_files[len(i_files) // 2], i_files[-1]]
picks_a = [a_files[0], a_files[len(a_files) // 2], a_files[-1]]

print("旧素材背景(角落中位) RGB/hue/sat/val:", bg_hsv(Image.open(picks_i[0])))
for p in picks_a:
    print(os.path.basename(p), "背景(角落中位) RGB/hue/sat/val:", bg_hsv(Image.open(p)))

for p in picks_i + picks_a:
    bb = fg_bbox(Image.open(p))
    h = bb[3] - bb[1] + 1
    print(os.path.basename(p), "前景框", bb, "高", h, "高度占比 %.1f%%" % (h / 720 * 100),
          "宽占比 %.1f%%" % ((bb[2] - bb[0] + 1) / 1280 * 100))

# 拼对比图：上排旧素材，下排新素材
TH = 360
tiles = []
for p in picks_i + picks_a:
    im = Image.open(p).convert("RGB")
    tiles.append(im.resize((int(im.width * TH / im.height), TH), Image.LANCZOS))
W = sum(t.width for t in tiles[:3])
canvas = Image.new("RGB", (W, TH * 2 + 26), (24, 24, 24))
d = ImageDraw.Draw(canvas)
d.text((6, 4), "SEG1 idle: first / mid / last", fill=(255, 255, 255))
d.text((6, TH + 14), "SEG2 action: first / mid / last", fill=(255, 255, 255))
for row, group in enumerate((tiles[:3], tiles[3:])):
    x = 0
    for t in group:
        canvas.paste(t, (x, row * TH + (26 if row else 0)))
        x += t.width
canvas.save(os.path.join(BASE, "clip_compare.png"))
print("对比图 ->", os.path.join(BASE, "clip_compare.png"), canvas.size)
