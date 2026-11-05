#!/usr/bin/env python3
"""Extract + chroma-key the 4 green-screen material clips into transparent frame
sequences (WebP lossless w/ alpha) and emit clips.json. Mirrors deskpet-python's
frame-sequence layout so the Tauri frontend can load CLIPS the same way pet.py does.
"""
import os, subprocess, json, shutil
from PIL import Image
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)                       # deskpet-tauri/
ROOT = os.path.dirname(PROJECT)                       # zhiling/
FF = os.path.join(ROOT, r"deskpet-python", ".venv", "Lib", "site-packages",
                  "imageio_ffmpeg", "binaries", "ffmpeg-win-x86_64-v9.0.2.exe")
MAT = os.path.join(ROOT, "material")
# 无损原帧只作为中间产物，留在 frontend/ 之外，避免被 Tauri 打进 exe。
OUT = os.path.join(PROJECT, "archive", "raw-lossless-frames", "assets")
FPS = {"待机": 24, "工作中": 24, "不高兴": 24, "真开心": 24}
# type + priority drive how the player chooses clips. idle loops; action clips play once.
META = {
    "待机":   {"type": "idle",   "name": "待机",   "priority": 0, "loop": True},
    "工作中": {"type": "action", "name": "工作中", "priority": 1, "loop": False},
    "不高兴": {"type": "action", "name": "不高兴", "priority": 2, "loop": False},
    "真开心": {"type": "action", "name": "真开心", "priority": 3, "loop": False},
}
TMP = os.path.join(ROOT, "deskpet-tauri", "build", "raw")

def border_key(im):
    """Estimate green-screen key color from the frame border (always background)."""
    a = np.asarray(im.convert("RGB"), dtype=np.float64)
    h, w = a.shape[:2]
    b = np.concatenate([
        a[0, :, :], a[h-1, :, :], a[:, 0, :], a[:, w-1, :]
    ], axis=0)
    return b.mean(axis=0)  # [r,g,b]

def fg_mask(a, key, lo=70.0, hi=140.0):
    d = np.sqrt(((a - key) ** 2).sum(axis=2))
    mask = np.zeros(a.shape[:2], dtype=np.float64)
    mask[d > hi] = 1.0
    band = (d > lo) & (d <= hi)
    mask[band] = (d[band] - lo) / (hi - lo)
    return mask

def key_frame(im, key, lo=70.0, hi=140.0):
    a = np.asarray(im.convert("RGB"), dtype=np.float64)
    mask = fg_mask(a, key, lo, hi)
    # despill: pull green of semi-transparent pixels toward avg of r,b
    out = np.empty((a.shape[0], a.shape[1], 4), dtype=np.uint8)
    rgb = a.astype(np.uint8)
    out[..., :3] = rgb
    alpha = (mask * 255).astype(np.uint8)
    # soften spill on edges only
    spill = (1.0 - mask) * (mask > 0)  # 0..1 near-edge strength
    g = a[..., 1]
    avg_rb = (a[..., 0] + a[..., 2]) / 2.0
    newg = g - spill * np.clip(g - avg_rb, 0, None) * 0.8
    out[..., 1] = newg.astype(np.uint8)
    out[..., 3] = alpha
    return Image.fromarray(out, "RGBA")

def main():
    clips = {}
    for clip in META:
        print(f"\n=== {clip} ===")
        raw = os.path.join(TMP, clip)
        if os.path.exists(raw):
            shutil.rmtree(raw)
        os.makedirs(raw, exist_ok=True)
        src = os.path.join(MAT, clip + ".mp4")
        # dump every frame as PNG
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", src,
                        os.path.join(raw, "f%04d.png")], check=True)
        files = sorted(os.listdir(raw))
        files = [f for f in files if f.endswith(".png")]
        n = len(files)
        # pass 1: key color + union bbox
        im0 = Image.open(os.path.join(raw, files[0]))
        key = border_key(im0)
        print(f"  frames={n} key={key.round(1).tolist()}")
        union = None
        h, w = im0.size[1], im0.size[0]
        for f in files:
            im = Image.open(os.path.join(raw, f)).convert("RGB")
            a = np.asarray(im, dtype=np.float64)
            m = fg_mask(a, key)
            ys, xs = np.where(m > 0.05)
            if len(xs) == 0:
                continue
            bb = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
            if union is None:
                union = bb
            else:
                union = (min(union[0], bb[0]), min(union[1], bb[1]),
                         max(union[2], bb[2]), max(union[3], bb[3]))
        if union is None:
            union = (0, 0, w, h)
        print(f"  bbox={union}")
        # pass 2: key + crop + save webp
        odir = os.path.join(OUT, clip)
        if os.path.exists(odir):
            shutil.rmtree(odir)
        os.makedirs(odir, exist_ok=True)
        count = 0
        for i, f in enumerate(files):
            im = Image.open(os.path.join(raw, f))
            ki = key_frame(im, key)
            ki = ki.crop(union)
            ki.save(os.path.join(odir, f"f{i:04d}.webp"), "WEBP", lossless=True)
            count += 1
        clips[clip] = {
            "dir": clip,
            "fps": FPS[clip],
            "count": count,
            "format": "webp",
            "name": META[clip]["name"],
            "type": META[clip]["type"],
            "loop": META[clip]["loop"],
            "priority": META[clip]["priority"],
            "bbox": union,
            "size": [union[2]-union[0], union[3]-union[1]],
        }
        print(f"  wrote {count} frames -> {odir}")
    with open(os.path.join(OUT, "clips.json"), "w", encoding="utf-8") as fp:
        json.dump(clips, fp, ensure_ascii=False, indent=2)
    print(f"\nclips.json written with {len(clips)} clips")

if __name__ == "__main__":
    main()
