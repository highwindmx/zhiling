"""新素材（纯绿底）抠像参数试探：3 帧 x 1 组参数，棋盘底看边缘。"""
import os, sys, glob
import numpy as np
from PIL import Image, ImageDraw
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workshop"))   # keyer 已迁到 workshop/
from keyer import key_frame

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "..", "cache", "raw", "action")
# 新素材水印：左上“AI生成”、右下“千问”
WM = [(0, 0, 230, 90), (1080, 630, 1280, 720)]

P = dict(hue_ref=148.0, hue_in=20.0, hue_out=34.0, sat_lo=0.30, sat_hi=0.55,
         val_lo=0.30, val_hi=0.45, erode=1, blur=1.0, floor=0.16, despill=0.75,
         keep_frac=1.0, watermarks=WM)

files = sorted(glob.glob(os.path.join(RAW, "*.png")))
picks = [files[0], files[len(files) // 2], files[-1]]

TH = 380
tiles = []
for p in picks:
    rgba = key_frame(np.asarray(Image.open(p).convert("RGB")), **P)
    a = np.asarray(rgba)[:, :, 3]
    print(os.path.basename(p), "alpha 均值 %.0f  不透明占比 %.1f%%  全透明占比 %.1f%%"
          % (a.mean(), (a > 250).mean() * 100, (a < 5).mean() * 100))
    bg = Image.new("RGB", rgba.size, (60, 60, 60))
    d = ImageDraw.Draw(bg)
    for y in range(0, bg.height, 16):
        for x in range(0, bg.width, 16):
            if (x // 16 + y // 16) % 2 == 0:
                d.rectangle([x, y, x + 15, y + 15], fill=(105, 105, 105))
    bg = bg.convert("RGBA")
    bg.alpha_composite(rgba)
    tiles.append(bg.convert("RGB").resize((int(bg.width * TH / bg.height), TH), Image.LANCZOS))

W = sum(t.width for t in tiles)
canvas = Image.new("RGB", (W, TH), (24, 24, 24))
x = 0
for t in tiles:
    canvas.paste(t, (x, 0))
    x += t.width
canvas.save(os.path.join(BASE, "action_check.png"))
print("->", os.path.join(BASE, "action_check.png"))
