"""DSMaid 第二批素材体检：黑边（逐帧是否稳定）、绿幕色相、光晕、主体占比。

输出：
  * build/_probe2/report.json —— 每段的原始数字
  * docs/screenshots/dsmaid-seven-contact-sheet.png —— 7 段首/中/尾帧拼图（带黑边框标注）

用法：
    .venv\\Scripts\\python.exe build\\analyze_pack.py
"""
import glob, json, os, subprocess, sys, time
import numpy as np
from PIL import Image, ImageDraw
import imageio_ffmpeg

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.path.dirname(ROOT)
SRC = os.path.join(REPO, "material", "DSMaid")
OUT = os.path.join(ROOT, "build", "cache", "probe")
SHOT = os.path.join(REPO, "docs", "screenshots")
DARK = 38          # 亮度低于它算"近黑"
DARK_STD = 14      # 且这一行/列足够"均匀"（不是角色自身的黑发）
ff = imageio_ffmpeg.get_ffmpeg_exe()


def probe(path):
    gen = imageio_ffmpeg.read_frames(path)
    meta = next(gen)
    gen.close()
    fps, dur = meta.get("fps"), meta.get("duration")
    return {"size": tuple(meta["size"]), "fps": round(fps, 2) if fps else None,
            "dur": round(dur, 2) if dur else None}


def extract(src, tag, fps=1):
    # 抽帧目录用 ASCII 名（v01/v02…）：本机杀软对"新建中文目录 + ffmpeg 立刻写入"
    # 的组合会偶发 WinError 5/拒绝访问，换成 ASCII 目录就稳定了
    d = os.path.join(OUT, tag)
    # 本机杀软对新目录有短暂占用，偶发 WinError 5，重试几次即可
    for i in range(5):
        try:
            os.makedirs(d, exist_ok=True)
            break
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.3)
    for f in glob.glob(os.path.join(d, "*.png")):
        os.remove(f)
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-vf", "fps=%d" % fps, "-pix_fmt", "rgb24",
                    os.path.join(d, "p%04d.png")], check=True)
    return sorted(glob.glob(os.path.join(d, "*.png")))


def dark_band(gray, axis, from_start=True, limit=220):
    """从一条边往里扫，返回连续"近黑且均匀"的行/列数。axis=0 表示按行（上下边）。"""
    n = 0
    total = gray.shape[0] if axis == 0 else gray.shape[1]
    idxs = range(min(limit, total)) if from_start else range(total - 1, max(-1, total - 1 - limit), -1)
    for i in idxs:
        line = gray[i, :] if axis == 0 else gray[:, i]
        if float(line.mean()) < DARK and float(line.std()) < DARK_STD:
            n += 1
        else:
            break
    return n


def borders(path):
    rgb = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    gray = rgb.mean(axis=2)
    return {
        "top": dark_band(gray, 0, True), "bottom": dark_band(gray, 0, False),
        "left": dark_band(gray, 1, True), "right": dark_band(gray, 1, False),
    }


def crop_union(imgs):
    """全片黑边的并集（裁多少由最宽的一帧决定），保证裁完不再逐帧抖动。"""
    u = {"top": 0, "bottom": 0, "left": 0, "right": 0}
    for b in imgs:
        for k in u:
            u[k] = max(u[k], b[k])
    return u


def bg_stats(img, crop):
    im = np.asarray(img.crop(crop)).astype(np.float32)
    hsv = np.asarray(Image.fromarray(im.clip(0, 255).astype(np.uint8)).convert("HSV")).astype(np.int32)
    h = hsv[..., 0] * 360 // 255
    ring = np.concatenate([h[:10].ravel(), h[-10:].ravel(), h[:, :10].ravel(), h[:, -10:].ravel()])
    s = (hsv[..., 1] / 255.0)
    ring_s = np.concatenate([s[:10].ravel(), s[-10:].ravel(), s[:, :10].ravel(), s[:, -10:].ravel()])
    v = (hsv[..., 2] / 255.0)
    ring_v = np.concatenate([v[:10].ravel(), v[-10:].ravel(), v[:, :10].ravel(), v[:, -10:].ravel()])
    return {"hue": float(np.median(ring)), "sat_p10": float(np.percentile(ring_s, 10)),
            "sat_p90": float(np.percentile(ring_s, 90)),
            "val_p10": float(np.percentile(ring_v, 10)),
            "val_p90": float(np.percentile(ring_v, 90))}


def main():
    os.makedirs(OUT, exist_ok=True)
    files = sorted(glob.glob(os.path.join(SRC, "*.mp4")))
    rows, sheet_rows = [], []
    for vi, path in enumerate(files, 1):
        name = os.path.splitext(os.path.basename(path))[0]
        m = probe(path)
        frames = extract(path, "v%02d" % vi)
        ims = [Image.open(p).convert("RGB") for p in frames]
        bs = [borders(p) for p in frames]
        u = crop_union(bs)
        W, H = m["size"]
        crop = (u["left"], u["top"], W - u["right"], H - u["bottom"])
        stats = [bg_stats(im, crop) for im in ims]
        hue = float(np.median([s["hue"] for s in stats]))
        row = {"name": name, "size": "%dx%d" % (W, H), "fps": m["fps"], "dur": m["dur"],
               "samples": len(ims), "black": u,
               "black_var": {k: (min(b[k] for b in bs), max(b[k] for b in bs)) for k in u},
               "hue": round(hue, 1),
               "sat_p10": round(float(np.median([s["sat_p10"] for s in stats])), 3),
               "sat_p90": round(float(np.median([s["sat_p90"] for s in stats])), 3),
               "val_p10": round(float(np.median([s["val_p10"] for s in stats])), 3),
               "val_p90": round(float(np.median([s["val_p90"] for s in stats])), 3),
               "crop": list(crop)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
        # 拼图：首/中/尾三帧，缩到 240 宽
        tiles = []
        for i in (0, len(ims) // 2, len(ims) - 1):
            im = ims[i]
            tw = 240
            t = im.resize((tw, round(im.height * tw / im.width)), Image.LANCZOS)
            d = ImageDraw.Draw(t)
            k = tw / im.width
            d.rectangle([u["left"] * k, u["top"] * k,
                         t.width - u["right"] * k - 1, t.height - u["bottom"] * k - 1],
                        outline=(255, 200, 0))
            tiles.append(t)
        th = max(t.height for t in tiles)
        rowimg = Image.new("RGB", (sum(t.width for t in tiles) + 8, th), (30, 30, 30))
        x = 0
        for t in tiles:
            rowimg.paste(t, (x, 0)); x += t.width + 4
        sheet_rows.append((name, rowimg))

    label_w = 76
    maxw = max(r.width for _, r in sheet_rows) + label_w
    total_h = sum(r.height + 4 for _, r in sheet_rows)
    sheet = Image.new("RGB", (maxw, total_h), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    y = 0
    for name, r in sheet_rows:
        d.text((4, y + r.height // 2), name, fill=(230, 230, 230))
        sheet.paste(r, (label_w, y)); y += r.height + 4
    os.makedirs(SHOT, exist_ok=True)
    out = os.path.join(SHOT, "dsmaid-seven-contact-sheet.png")
    sheet.save(out)
    json.dump(rows, open(os.path.join(OUT, "report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("sheet:", out, sheet.size)


if __name__ == "__main__":
    main()
