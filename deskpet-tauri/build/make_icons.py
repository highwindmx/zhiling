"""生成 Tauri 图标集：紫色圆角矩形 + 黑体白色「灵」字（与 Python 版 make_icon.py 同一套参数）。

每个尺寸独立渲染（小尺寸 8× 超采样后 LANCZOS 落回），不从小图放大，也不从 512 缩到底，
保证 16×16 托盘图标里「灵」仍然可辨。多尺寸 .ico 由本脚本手写容器，每个档位写入自己渲染的 PNG。

用法（需要 Pillow，可用 Python 版的项目环境）：
    uv run --project ../deskpet-python python build/make_icons.py

产出（src-tauri/icons/）：
    16/20/24/32/48/64/128/256/512 的 PNG、128x128@2x.png(256)、icon.png(512)
    Windows 打包用的 Square*Logo.png / StoreLogo.png
    icon.ico（16/20/24/32/48/64/128/256 多尺寸）
"""
import io
import os
import struct

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # deskpet-tauri
ICONS = os.path.join(ROOT, "src-tauri", "icons")

BG_COLOR = "#6D4BD6"       # 与 Python 版一致
RADIUS_RATIO = 0.22
TEXT = "灵"
TEXT_COLOR = "#FFFFFF"
TEXT_HEIGHT_RATIO = 0.74

ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
PNG_SIZES = (16, 20, 24, 32, 48, 64, 128, 256, 512)
SQUARE_LOGOS = (30, 44, 71, 89, 107, 142, 150, 284, 310)
STORE_LOGO = 50

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
)


def find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def supersample(size):
    if size <= 32:
        return 8
    if size <= 64:
        return 4
    if size <= 128:
        return 2
    return 1


def render(size, font_path):
    """在 size×size 的透明画布上独立绘制圆角矩形 + 居中「灵」。"""
    ss = supersample(size)
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = max(2, round(s * RADIUS_RATIO))
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=BG_COLOR)

    target_h = s * TEXT_HEIGHT_RATIO
    max_w = s * 0.80
    fs = max(4, int(target_h))
    while fs > 4:
        font = (ImageFont.truetype(font_path, fs) if font_path
                else ImageFont.load_default())
        bbox = d.textbbox((0, 0), TEXT, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if th <= target_h and tw <= max_w:
            break
        fs -= 1
    stroke = max(0, round(s / 128))
    bbox = d.textbbox((0, 0), TEXT, font=font, stroke_width=stroke)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (s - tw) / 2 - bbox[0]
    y = (s - th) / 2 - bbox[1]
    d.text((x, y), TEXT, font=font, fill=TEXT_COLOR, stroke_width=stroke, stroke_fill=TEXT_COLOR)
    return img.resize((size, size), Image.LANCZOS), fs


def save_ico(images, path):
    """手写 ICO 容器：每个尺寸写入自己渲染的 PNG，避免 Pillow 再缩一次。"""
    blobs = []
    for im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        blobs.append(buf.getvalue())
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for im, blob in zip(images, blobs):
        w = 0 if im.width >= 256 else im.width
        h = 0 if im.height >= 256 else im.height
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(header + entries)
        for blob in blobs:
            f.write(blob)


def main():
    font_path = find_font()
    os.makedirs(ICONS, exist_ok=True)
    cache = {}

    def get(size):
        if size not in cache:
            cache[size] = render(size, font_path)
        return cache[size][0]

    for size in PNG_SIZES:
        im, fs = cache[size] if size in cache else render(size, font_path)
        cache[size] = (im, fs)
        im.save(os.path.join(ICONS, "%dx%d.png" % (size, size)), format="PNG")

    im, _ = cache[256]
    im.save(os.path.join(ICONS, "128x128@2x.png"), format="PNG")
    im512, _ = cache[512]
    im512.save(os.path.join(ICONS, "icon.png"), format="PNG")

    for size in SQUARE_LOGOS:
        get(size).save(os.path.join(ICONS, "Square%dx%dLogo.png" % (size, size)), format="PNG")
    get(STORE_LOGO).save(os.path.join(ICONS, "StoreLogo.png"), format="PNG")

    save_ico([get(s) for s in ICO_SIZES], os.path.join(ICONS, "icon.ico"))

    print("icons -> %s" % ICONS)
    print("字体 %s" % (os.path.basename(font_path) if font_path else "<default>"))
    for size in sorted(cache):
        im, fs = cache[size]
        a = im.getchannel("A")
        nz = sum(1 for v in a.getdata() if v > 16)
        print("  %3dx%-3d 字号(super)=%3d 非透明 %.1f%%" % (size, size, fs, 100.0 * nz / (size * size)))


if __name__ == "__main__":
    main()
