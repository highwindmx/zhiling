"""11 段素材的质检总表（与上一轮同口径）：残留绿边 %、半透明雾像素比、脚底锚点 y、主体高度。

残留绿：alpha>40 的像素里，色相落在该段背景色 ±30° 且饱和度>0.3 的比例。
半透明雾：整张画布里 16<alpha<200 的像素比例（边缘羽化 + 光晕）。
脚底锚点：alpha>40 的最大 y（画布 530 高）。
"""
import glob, json, os
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
SHOT = os.path.join(REPO, "docs", "screenshots")

HUE = {"dsh_idle": 135, "dsh_happy": 148, "dsh_work": 135, "dsh_unhappy": 124,
       "dsh_proud": 138, "dsh_think": 98, "dsh_yawn": 120, "dsh_helpless": 141,
       "dsh_phone": 111, "dsh_rice": 151, "dsh_shock": 96,
       "idle": None, "action": None, "action2": None, "action3": None}


def qc(cid, meta):
    d = os.path.join(ROOT, meta.get("dir", "frames_" + cid))
    fs = sorted(glob.glob(os.path.join(d, "f*.webp"))) or sorted(glob.glob(os.path.join(d, "f*.png")))
    if not fs:
        return None
    step = max(1, len(fs) // 12)
    resid, haze, feet, body = [], [], [], []
    sizes = set()
    for p in fs[::step]:
        im = Image.open(p).convert("RGBA")
        sizes.add(im.size)
        a = np.asarray(im)[..., 3]
        hsv = np.asarray(im.convert("HSV"))
        h = hsv[..., 0].astype(np.int32) * 360 // 255
        s = hsv[..., 1].astype(np.float32) / 255
        fg = a > 40
        if fg.any():
            hue = HUE.get(cid)
            if hue is not None:
                dh = np.abs(((h - hue + 180) % 360) - 180)
                resid.append((((dh < 30) & (s > 0.3) & fg).sum()) / fg.sum())
            ys, xs = np.where(fg)
            feet.append(int(ys.max()))
            body.append(int(ys.max() - ys.min() + 1))
        haze.append(float(((a > 16) & (a < 200)).mean()))
    return dict(id=cid, frames=len(fs), size=str(sorted(sizes)[0]),
                mb=sum(os.path.getsize(p) for p in fs) / 1e6,
                resid=float(np.median(resid)) * 100 if resid else 0.0,
                resid_max=float(np.max(resid)) * 100 if resid else 0.0,
                haze=float(np.median(haze)) * 100 if haze else 0.0,
                feet=int(np.median(feet)) if feet else 0,
                body=int(np.median(body)) if body else 0)


def main():
    meta = json.load(open(os.path.join(ROOT, "meta.json"), encoding="utf-8"))["clips"]
    clips = json.load(open(os.path.join(ROOT, "clips.json"), encoding="utf-8"))["clips"]
    rows = []
    for c in clips:
        r = qc(c["id"], meta.get(c["id"], {}))
        if r:
            m = meta.get(c["id"], {})
            r.update(displayName=c["displayName"], scale=m.get("scale"), offset=m.get("offset"))
            rows.append(r)
    print("%-13s %-8s %5s %9s %8s %7s %6s %5s %5s %6s %s"
          % ("clip", "名称", "帧数", "尺寸", "MB", "残留绿%", "最大%", "雾%", "bodyH", "foot", "scale/offset"))
    total = 0
    for r in rows:
        total += r["mb"]
        print("%-13s %-8s %5d %9s %8.2f %7.2f %6.2f %5.1f %5d %5d %s %s"
              % (r["id"], r["displayName"], r["frames"], r["size"], r["mb"], r["resid"],
                 r["resid_max"], r["haze"], r["body"], r["feet"], r["scale"], r["offset"]))
    print("合计 %.1f MB" % total)
    json.dump(rows, open(os.path.join(ROOT, "build", "_probe2", "qc.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # 11 段预览拼图（取中段帧，棋盘格背景看半透明）
    tiles = []
    for r in rows:
        d = meta.get(r["id"], {}).get("dir", "frames_" + r["id"])
        fs = sorted(glob.glob(os.path.join(ROOT, d, "f*.webp"))) or \
             sorted(glob.glob(os.path.join(ROOT, d, "f*.png")))
        im = Image.open(fs[len(fs) // 2]).convert("RGBA")
        board = Image.new("RGBA", im.size, (255, 255, 255, 255))
        for y in range(0, im.height, 24):
            for x in range(0, im.width, 24):
                if (x // 24 + y // 24) % 2 == 0:
                    ImageDraw.Draw(board).rectangle([x, y, x + 23, y + 23], fill=(208, 208, 208, 255))
        board.alpha_composite(im)
        tiles.append((r["id"], board.convert("RGB")))
    cols = 4
    tw, th = tiles[0][1].size
    rows_n = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows_n * (th + 18)), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    for i, (name, t) in enumerate(tiles):
        cx, cy = (i % cols) * tw, (i // cols) * (th + 18)
        d.text((cx + 4, cy + 3), name, fill=(230, 230, 230))
        sheet.paste(t, (cx, cy + 18))
    os.makedirs(SHOT, exist_ok=True)
    out = os.path.join(SHOT, "dsmaid-11-preview.png")
    sheet.save(out)
    print("preview:", out)


if __name__ == "__main__":
    main()
