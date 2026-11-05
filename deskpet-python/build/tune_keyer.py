"""用 analyze_pack 的实测背景值 + 只留最大连通块，检查每段抠像是否干净。

评价口径（与上一轮验收一致）：
  * 残留绿 %：alpha>20 的像素里，色相还在绿幕 ±25° 且饱和度>0.3 的比例
  * 主体框 / 面积：确认没有"整帧都是前景"（抠像失效）或"主体丢了"
  * 连通块数：只留最大块后还剩几个（水印/气泡是否被清掉）
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

# 段 id -> 素材名（顺序与 analyze_pack 的文件排序一致，用报告里的 crop 复用）
NAMES = {
    "dsh_idle": "待机", "dsh_happy": "真开心", "dsh_work": "工作中",
    "dsh_unhappy": "不高兴", "dsh_proud": "得意状", "dsh_think": "思考状",
    "dsh_yawn": "打哈欠", "dsh_helpless": "无奈状", "dsh_phone": "看手机",
    "dsh_rice": "吃米饭", "dsh_shock": "炸毛",
}


def params_of(row, keep_frac=0.05):
    hue = row["hue"]
    s10, s90 = row["sat_p10"], row["sat_p90"]
    v10, v90 = row["val_p10"], row["val_p90"]
    return dict(hue_ref=hue, hue_in=14.0, hue_out=28.0,
                sat_lo=round(max(0.03, s10 * 0.6), 3),
                sat_hi=round(max(s90 * 0.95, s10 * 0.6 + 0.04), 3),
                val_lo=round(max(0.12, v10 * 0.75), 3),
                val_hi=round(max(v90 * 0.98, v10 * 0.75 + 0.04), 3),
                erode=1, blur=1.0, floor=0.16, despill=0.7,
                keep_frac=keep_frac, watermarks=[])


def check(rgb, crop, p):
    im = rgb.crop(tuple(crop)) if crop and crop != [0, 0] else rgb
    rgba = key_frame(np.asarray(im), **p)
    a = np.asarray(rgba)[:, :, 3]
    hsl = np.asarray(im.convert("HSV")).astype(np.int32)
    h = hsl[..., 0] * 360 // 255
    s = hsl[..., 1] / 255.0
    dh = np.abs(((h - p["hue_ref"] + 180) % 360) - 180)
    fg = a > 20
    greenish = (dh < 25) & (s > 0.3)
    resid = float((fg & greenish).sum() / max(1, fg.sum()))
    ys, xs = np.where(a > 8)
    box = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())] if len(ys) else None
    lab, n = ndimage.label(a > 20)
    return resid, box, float(fg.mean()), n, im.size


def main():
    for cid, name in NAMES.items():
        rows = [r for r in REPORT if r["name"] == name]
        if not rows:
            print("缺报告", name); continue
        row = rows[0]
        tag = "v%02d" % (sorted(r["name"] for r in REPORT).index(name) + 1)
        fs = sorted(glob.glob(os.path.join(PROBE, tag, "*.png")))
        if not fs:
            print("缺样本", name); continue
        crop = row["crop"] if row["black"]["left"] or row["black"]["right"] else None
        p = params_of(row)
        res = []
        for f in (fs[0], fs[len(fs) // 2], fs[-1]):
            res.append(check(Image.open(f).convert("RGB"), crop, p))
        print("%-13s %-6s crop=%-22s 残留绿 %.2f%%  面积 %.1f%%  块数 %d 框 %s"
              % (cid, name, crop, max(r[0] for r in res) * 100,
                 res[-1][2] * 100, res[-1][3], res[-1][1]))
        print("   ", json.dumps(p, ensure_ascii=False))


if __name__ == "__main__":
    main()
