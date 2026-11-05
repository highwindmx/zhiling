"""为每段素材搜索一组能抠干净的色域参数（外层边框 alpha 应为 0，主体面积合理）。

打分：边框残留 alpha 均值（希望接近 0） + 面积不在 12%~65% 区间时的惩罚 +
      主体是否贴着画面边缘（贴边说明背景没抠净）。
"""
import glob, json, os, sys
import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keyer import key_frame

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "build", "_probe2")
REPORT = json.load(open(os.path.join(PROBE, "report.json"), encoding="utf-8"))
NAMES = ["待机", "真开心", "工作中", "不高兴", "得意状", "思考状", "打哈欠",
         "无奈状", "看手机", "吃米饭", "炸毛"]
IDS = {"待机": "dsh_idle", "真开心": "dsh_happy", "工作中": "dsh_work",
       "不高兴": "dsh_unhappy", "得意状": "dsh_proud", "思考状": "dsh_think",
       "打哈欠": "dsh_yawn", "无奈状": "dsh_helpless", "看手机": "dsh_phone",
       "吃米饭": "dsh_rice", "炸毛": "dsh_shock"}


def score(rgb, p):
    a = np.asarray(key_frame(np.asarray(rgb), **p))[:, :, 3]
    h, w = a.shape
    ring = np.concatenate([a[:6].ravel(), a[-6:].ravel(), a[:, :6].ravel(), a[:, -6:].ravel()])
    ring_a = float(ring.mean()) / 255.0
    fg = a > 20
    area = float(fg.mean())
    pen = 0.0 if 0.10 <= area <= 0.65 else 0.5
    ys, xs = np.where(a > 8)
    if len(xs):
        touches = (xs.min() <= 1) + (ys.min() <= 1) + (xs.max() >= w - 2) + (ys.max() >= h - 2)
        pen += 0.08 * touches
    return ring_a + pen, ring_a, area, (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())) if len(xs) else None


def main():
    best_all = {}
    for name in NAMES:
        row = [r for r in REPORT if r["name"] == name][0]
        tag = "v%02d" % (sorted(r["name"] for r in REPORT).index(name) + 1)
        fs = sorted(glob.glob(os.path.join(PROBE, tag, "*.png")))
        rgb = Image.open(fs[len(fs) // 2]).convert("RGB")
        crop = row["crop"] if row["black"]["left"] or row["black"]["right"] else None
        if crop:
            rgb = rgb.crop(tuple(crop))
        s10, s90 = row["sat_p10"], row["sat_p90"]
        v10, v90 = row["val_p10"], row["val_p90"]
        best = None
        for k in (0.5, 0.75, 1.0):
            sat_lo = round(max(0.03, s10 * k), 3)
            for add in (0.02, 0.05, 0.09):
                sat_hi = round(sat_lo + add, 3)
                for (vl, vh) in ((max(0.12, v10 * 0.6), max(0.12, v10 * 0.6) + 0.12),
                                 (0.35, 0.55)):
                    p = dict(hue_ref=row["hue"], hue_in=14.0, hue_out=28.0,
                             sat_lo=sat_lo, sat_hi=sat_hi, val_lo=round(vl, 3), val_hi=round(vh, 3),
                             erode=1, blur=1.0, floor=0.16, despill=0.7,
                             keep_frac=0.05, watermarks=[])
                    sc, ring_a, area, box = score(rgb, p)
                    if best is None or sc < best[0]:
                        best = (sc, ring_a, area, box, p)
        sc, ring_a, area, box, p = best
        print("%-13s %-6s score=%.3f 边框alpha=%.3f 面积=%.1f%% 框=%s" %
              (IDS[name], name, sc, ring_a, area * 100, box))
        print("    params=%s crop=%s" % (json.dumps(p, ensure_ascii=False), crop))
        best_all[IDS[name]] = dict(params=p, crop=crop, area=round(area, 3))
    json.dump(best_all, open(os.path.join(PROBE, "best.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
