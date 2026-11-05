"""抠像复用回归：用 build_clip 以 action2 的原始参数重跑一遍，与仓库里已提交的 frames_action2 做像素级对比。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image
import build_frames

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
VID = os.path.join(os.path.dirname(ROOT), "海螺_视频_@图片1请基于这个大_574308429835190278.mp4")

PARAMS = dict(hue_ref=136.4, hue_in=20.0, hue_out=34.0, sat_lo=0.30, sat_hi=0.55,
              val_lo=0.30, val_hi=0.45, erode=1, blur=1.0, floor=0.16, despill=0.75,
              keep_frac=1.0, watermarks=[[610, 682, 1024, 745]])

r = build_frames.build_clip({"id": "regress", "video": VID, "params": PARAMS,
                             "fps": 24, "width": 576, "fmt": "webp", "quality": 80},
                            log=lambda s: print("  " + s, flush=True))
new_dir = os.path.join(ROOT, "frames_regress")
old_dir = os.path.join(ROOT, "frames_action2")
print("重跑：%d 帧 %dx%d" % (r["count"], r["width"], r["height"]), flush=True)

diffs, exact = [], 0
for i in range(min(r["count"], len(os.listdir(old_dir)))):
    a = np.asarray(Image.open(os.path.join(new_dir, "f%04d.webp" % i)).convert("RGBA")).astype(np.int16)
    b = np.asarray(Image.open(os.path.join(old_dir, "f%04d.webp" % i)).convert("RGBA")).astype(np.int16)
    if a.shape != b.shape:
        print("尺寸不同，无法比较", a.shape, b.shape, flush=True)
        break
    d = np.abs(a - b)
    diffs.append((int(d.max()), float(d.mean()), int((d.max(2) > 0).sum()) / d.shape[0] / d.shape[1]))
    if d.max() == 0:
        exact += 1

if diffs:
    mx = max(x[0] for x in diffs)
    mean = sum(x[1] for x in diffs) / len(diffs)
    ratio = sum(x[2] for x in diffs) / len(diffs)
    print("对比 %d 帧：逐像素完全相同 %d 帧；最大通道差 %d/255；平均差 %.3f；"
          "有差异像素占比 %.3f%%" % (len(diffs), exact, mx, mean, ratio * 100), flush=True)
    print("结论：%s" % ("像素级一致" if mx == 0 else
                       "存在细微差异（WebP 编码非确定性/浮点微小差），肉眼不可见" if mx <= 2 else
                       "差异明显，需要排查"), flush=True)
