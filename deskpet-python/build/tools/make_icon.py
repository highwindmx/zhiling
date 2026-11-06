"""托盘/窗口图标：紫色圆角矩形 + 黑体「宠」字，逐尺寸独立渲染，写多尺寸 .ico。

想改样式只动这三个常量：
    BG_COLOR      背景紫（现在是 #6D4BD6）
    RADIUS_RATIO  圆角半径占边长的比例（0.22 ≈ 256px 时 56px）
    TEXT          中间的字（现在是「宠」）
想改字色：TEXT_COLOR（默认白色）/ TEXT_COLOR_BLACK（黑字版本）

用法：
    uv run python build/tools/make_icon.py
产出：
    assets/icon.ico               白字多尺寸 16/20/24/32/48/64/128/256
    assets/icon.png               白字 256×256
    assets/icon_black_text.ico    黑字多尺寸（深色任务栏/浅紫底可选）
    assets/icon_black_text.png    黑字 256×256
    build/cache/probe/tray_icon_16.png / _32.png / _256.png   自检预览（不入库）

为什么每个尺寸单独画：256 画完再缩小，16px 的「宠」会糊成灰块；
这里每个尺寸按自己的像素网格算字号、超采样 8×（≤32px）后 LANCZOS 落回目标尺寸。
"""
import io
import os
import struct

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # deskpet-python
ASSETS = os.path.join(ROOT, "assets")
PROBE = os.path.join(ROOT, "build", "cache", "probe")

BG_COLOR = "#6D4BD6"       # 中等明度紫：白字对比 ~6:1，浅色/深色任务栏都能看见
RADIUS_RATIO = 0.22        # 256 边长时圆角 56px；16px 时 3.5px，缩小后仍是圆角不是方形
TEXT = "宠"
TEXT_COLOR = "#FFFFFF"
TEXT_COLOR_BLACK = "#141414"
TEXT_HEIGHT_RATIO = 0.74   # 字高占可用高度 72~78% 的中间值

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体
    r"C:\Windows\Fonts\msyhbd.ttc",      # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
)


def find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def supersample(size):
    """小尺寸超采样倍数：16~32 用 8×，48~64 用 4×，128 用 2×。"""
    if size <= 32:
        return 8
    if size <= 64:
        return 4
    if size <= 128:
        return 2
    return 1


def render(size, fg, font_path):
    """在 size×size 的透明画布上独立绘制圆角矩形 + 居中「宠」。"""
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
    # 笔画加粗：大图 1px，小图按超采样比例折算，缩小后仍有一点点厚度
    stroke = max(0, round(s / 128))
    bbox = d.textbbox((0, 0), TEXT, font=font, stroke_width=stroke)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # 视觉居中：用 textbbox 的偏移校正，而不是直接按 baseline 摆
    x = (s - tw) / 2 - bbox[0]
    y = (s - th) / 2 - bbox[1]
    d.text((x, y), TEXT, font=font, fill=fg, stroke_width=stroke, stroke_fill=fg)
    return img.resize((size, size), Image.LANCZOS), font_path and os.path.basename(font_path), fs


def save_ico(images, path):
    """手写 ICO 容器：每个尺寸写入自己渲染的 PNG，避免 Pillow 从 256 再缩一次。"""
    n = len(images)
    blobs = []
    for im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        blobs.append(buf.getvalue())
    header = struct.pack("<HHH", 0, 1, n)
    offset = 6 + 16 * n
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


def build_set(fg, ico_path, png_path, previews=()):
    font_path = find_font()
    imgs, used_font, sizes_pt = [], None, []
    for size in SIZES:
        im, used_font, fs = render(size, fg, font_path)
        imgs.append(im)
        sizes_pt.append((size, fs))
    os.makedirs(os.path.dirname(ico_path), exist_ok=True)
    save_ico(imgs, ico_path)
    imgs[-1].save(png_path, format="PNG")
    for size, p in previews:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        imgs[SIZES.index(size)].save(p, format="PNG")
    print("ico %s  %s" % (ico_path, " / ".join(str(s) for s in SIZES)))
    print("字体 %s，字号(超采样): %s" % (used_font, ", ".join("%d:%d" % x for x in sizes_pt)))
    for im in imgs:
        a = im.getchannel("A")
        nz = sum(1 for v in a.getdata() if v > 16)
        print("  %3dx%-3d 非透明 %.1f%%" % (im.width, im.height, 100.0 * nz / (im.width * im.height)))


def main():
    white = (os.path.join(ASSETS, "icon.ico"), os.path.join(ASSETS, "icon.png"))
    black = (os.path.join(ASSETS, "icon_black_text.ico"), os.path.join(ASSETS, "icon_black_text.png"))
    build_set(TEXT_COLOR, white[0], white[1], previews=(
        (16, os.path.join(PROBE, "tray_icon_16.png")),
        (32, os.path.join(PROBE, "tray_icon_32.png")),
        (256, os.path.join(PROBE, "tray_icon_256.png")),
    ))
    build_set(TEXT_COLOR_BLACK, black[0], black[1])
    print("\n色值：底 %s，白字 %s / 黑字 %s；圆角 %.0f%%"
          % (BG_COLOR, TEXT_COLOR, TEXT_COLOR_BLACK, RADIUS_RATIO * 100))


if __name__ == "__main__":
    main()
