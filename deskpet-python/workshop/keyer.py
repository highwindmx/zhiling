"""抠像核心：色相+饱和度判定背景（比单看绿度更稳），再按连通块清掉水印和气泡。"""
import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage

# 水印/角标区域，直接置为透明（都在四角，不压角色）
WATERMARKS = [(0, 0, 210, 100), (1000, 620, 1280, 720)]


def key_frame(rgb: np.ndarray, hue_ref=135.0, hue_in=12.0, hue_out=30.0,
              sat_lo=0.12, sat_hi=0.18, val_lo=0.45, val_hi=0.55,
              erode=1, blur=0.8, despill=0.6, floor=0.16, keep_frac=0.08,
              watermarks=WATERMARKS):
    a = rgb.astype(np.float32) / 255.0
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mx, mn = a.max(2), a.min(2)
    v = mx
    s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    d = mx - mn + 1e-6
    hue = np.where(mx == r, ((g - b) / d) % 6,
          np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    dh = np.abs(((hue - hue_ref + 180) % 360) - 180)          # 与绿幕色相的距离(度)

    # 三个条件同时满足才算“背景”：色相靠绿、够饱和、够亮
    kh = np.clip((hue_out - dh) / max(hue_out - hue_in, 1e-6), 0, 1)
    ks = np.clip((s - sat_lo) / max(sat_hi - sat_lo, 1e-6), 0, 1)
    kv = np.clip((v - val_lo) / max(val_hi - val_lo, 1e-6), 0, 1)
    bg = np.minimum(np.minimum(kh, ks), kv)
    # 补一刀：暗且灰的像素（压缩噪点/暗角/水印底）也算背景；只打中间亮度，避免啃掉角色黑描边
    kd = (np.clip((0.48 - v) / 0.10, 0, 1) * np.clip((v - 0.22) / 0.06, 0, 1)
          * np.clip((0.08 - s) / 0.05, 0, 1))
    bg = np.maximum(bg, kd)
    alpha = 1.0 - bg
    # 软地板：<16% 的残留一律掐掉，避免桌面背景上留一层淡绿雾
    alpha = np.clip((alpha - floor) / (1.0 - floor), 0, 1)

    # 去溢色：按比例削掉“绿比红蓝高”的那部分，保留角色本身的青蓝发色
    excess = np.maximum(0.0, g - np.maximum(r, b))
    rgb_out = np.stack([r, g - despill * excess, b], axis=2)

    am = (alpha * 255).astype(np.uint8)
    for (x0, y0, x1, y1) in watermarks:
        am[y0:y1, x0:x1] = 0

    # 连通块清理：只留主体，气泡/碎边全部丢掉
    mask = am > 20
    lab, n = ndimage.label(mask)
    if n > 1:
        areas = ndimage.sum(mask, lab, range(1, n + 1))
        big = areas.max()
        keep = {i + 1 for i, ar in enumerate(areas) if ar >= big * keep_frac}
        mask2 = np.isin(lab, list(keep))
        am = np.where(mask2, am, 0).astype(np.uint8)

    im = Image.fromarray(am, "L")
    for _ in range(erode):
        im = im.filter(ImageFilter.MinFilter(3))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))

    out = Image.fromarray((np.clip(rgb_out, 0, 1) * 255).astype(np.uint8), "RGB").convert("RGBA")
    out.putalpha(im)
    return out
