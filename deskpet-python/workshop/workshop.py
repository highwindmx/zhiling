"""素材工坊：把绿幕视频抠成透明帧序列，写进 packs/animations/<assetId>/ 并登记 meta.json。

三步流程（一屏三块）：① 导入 → ② 抠像 → ③ 导出。
抠像全部调用 workshop/keyer.py 的 key_frame()，生成调用 workshop/build_frames.py 的 build_clip()。

运行：
    uv run python workshop/workshop.py
    .venv\\Scripts\\python.exe workshop/workshop.py      （没有 uv 时的降级路径）
"""
import argparse, glob, json, os, re, subprocess, sys, time, traceback, logging
import numpy as np
from PIL import Image
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QPen, QIcon
from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QListWidget, QListWidgetItem,
                             QPushButton, QHBoxLayout, QVBoxLayout, QSlider, QDoubleSpinBox,
                             QComboBox, QSpinBox, QPlainTextEdit, QProgressBar, QSplitter,
                             QFileDialog, QSizePolicy, QCheckBox, QLineEdit, QInputDialog,
                             QMessageBox, QGroupBox, QToolButton, QFormLayout, QDialog,
                             QDialogButtonBox, QScrollArea, QFrame, QTextEdit, QStyle)

try:                                    # 可选：装了 pypinyin 就能把中文名转成拼音 id
    from pypinyin import lazy_pinyin
except Exception:                       # 没装就退回 clip_NN，不拦主流程
    lazy_pinyin = None

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)                  # deskpet-python/（build/、assets/ 都在这里）
REPO = os.path.dirname(ROOT)                  # 仓库根 zhiling/（material/ 等）
CACHE_DIR = os.path.join(ROOT, "build", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
# 槽函数里的异常会被 PyQt 直接 abort（0xC0000409），所以统一记一份日志，别只靠控制台
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(CACHE_DIR, "workshop.log"), encoding="utf-8")],
)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(ROOT, "build", "tools"))   # pack / ui_theme 仍在 build/tools（与设置窗共用）
from keyer import key_frame
import build_frames
import pack
from ui_theme import P as UI, QSS, dark_palette   # 与设置窗共用同一份配色

EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")

# 预览顶栏那一排按钮统一做成胶囊样式：描边颜色代表用途分组，选中反白。
# 蓝 = 取色（多帧取色 / 当前帧取色 / 加取色框），红 = 擦除（矩形擦除 / 清空标注），
# 灰 = 裁切（重跑裁边 / 裁切），这样一排按钮按用途分成三段，不再是清一色灰方块。
CHIP_SAMPLE_QSS = (
    "QToolButton { background-color:%s; border:1px solid %s; border-radius:11px;"
    " padding:3px 12px; color:%s; font-weight:600; }"
    "QToolButton:hover { background-color:%s; border-color:%s; }"
    "QToolButton:checked { background-color:%s; color:%s; border-color:%s; }"
    "QToolButton:disabled { color:%s; border-color:%s; background-color:%s; font-weight:400; }"
    % (UI["raised"], UI["accent"], UI["text"], UI["raised_hover"], UI["accent"],
       UI["accent"], UI["bg"], UI["accent"],
       UI["disabled"], UI["border"], UI["panel"]))
CHIP_ERASE_QSS = (
    "QToolButton { background-color:%s; border:1px solid %s; border-radius:11px;"
    " padding:3px 12px; color:%s; font-weight:600; }"
    "QToolButton:hover { background-color:%s; border-color:%s; }"
    "QToolButton:checked { background-color:%s; color:%s; border-color:%s; }"
    "QToolButton:disabled { color:%s; border-color:%s; background-color:%s; font-weight:400; }"
    % (UI["raised"], UI["error"], UI["text"], UI["raised_hover"], UI["error"],
       UI["error"], UI["bg"], UI["error"],
       UI["disabled"], UI["border"], UI["panel"]))
CHIP_PLAIN_QSS = (
    "QToolButton { background-color:%s; border:1px solid %s; border-radius:11px;"
    " padding:3px 12px; color:%s; }"
    "QToolButton:hover { background-color:%s; border-color:%s; }"
    "QToolButton:checked { background-color:%s; border-color:%s; color:%s; font-weight:600; }"
    "QToolButton:disabled { color:%s; border-color:%s; background-color:%s; }"
    % (UI["raised"], UI["border"], UI["text"], UI["raised_hover"], UI["accent"],
       UI["raised_hover"], UI["accent"], UI["text"],
       UI["disabled"], UI["border"], UI["panel"]))
# 源视频搜索目录：manifest 里一般只记文件名（如「举手提问.mp4」），按这个顺序找
SRC_DIRS = (os.path.join(REPO, "material", "DSMaid"),
            os.path.join(REPO, "material", "archive"),
            os.path.join(ROOT, "assets"))


def is_video_file(p):
    return bool(p) and os.path.isfile(p) and os.path.splitext(p)[1].lower() in EXTS


def rel_repo(p):
    """仓库内路径写相对路径，仓库外保留绝对路径。"""
    try:
        rel = os.path.relpath(p, REPO)
        return rel.replace("\\", "/") if not os.path.isabs(rel) and not rel.startswith("..") else p
    except (OSError, ValueError):
        return p


def find_source_by_name(name, extra_dirs=()):
    """在素材目录里按文件名找视频；先精确匹配，再无扩展名匹配。"""
    if not name:
        return ""
    dirs = list(extra_dirs) + list(SRC_DIRS)
    for d in dirs:
        p = os.path.join(d, name)
        if is_video_file(p):
            return p
    stem, ext = os.path.splitext(name)
    if not ext:
        for d in dirs:
            for e in EXTS:
                p = os.path.join(d, stem + e)
                if is_video_file(p):
                    return p
    # material 下可能有归档子目录
    for d in dirs:
        for p in glob.glob(os.path.join(d, "**", os.path.basename(name)), recursive=True):
            if is_video_file(p):
                return p
    return ""

# 参数分两层：主区只放「调了肉眼能看见」的三个，其余收进「更多参数」折叠。
# 依据见 keyer.key_frame()：bg = min(kh, ks, kv)，饱和度闸门 ks 是 min() 的瓶颈，
# 所以 sat_lo/sat_hi 一动整片背景都跟着变；hue_in/out 在色相已被正确取样后，
# 纯色绿幕的 dh 几乎都落在「完全背景」区，调了当然看不出差别。
MAIN_DEFS = [
    ("sat_lo", "饱和度下限", 0, 1, 0.30, "ks 的起点：低于此值的像素一定不当背景。压低它 → 背景抠得更干净。"),
    ("sat_hi", "饱和度上限", 0, 1, 0.55, "ks 的终点：高于此值才算「够绿」。这一对是真正的总闸门。"),
    ("blur", "羽化", 0, 8, 1.0, "alpha 边缘高斯模糊半径（像素），改的是边缘观感。"),
]
MORE_DEFS = [
    ("hue_in", "色相内", 0, 90, 18.0, "dh 小于此角度 → 完全背景。纯色绿幕几乎都用不到，但角色带绿色时是保险。"),
    ("hue_out", "色相外", 0, 120, 34.0, "dh 大于此角度 → 完全前景；两者之间是软过渡。"),
    ("floor", "alpha 软地板", 0, 0.95, 0.16, "把半透明的淡绿雾直接归零；调大会啃掉头发丝/薄纱边缘。"),
    ("despill", "去溢色", 0, 1, 0.75, "只改 RGB 不改 alpha：在棋盘格预览里看不出来，要切「实色底」才看得见绿边。"),
]
ADV_DEFS = MAIN_DEFS + MORE_DEFS + [
    ("hue_ref", "背景色相", 0, 360, 136.0, "绿幕色相中心；请由取色框自动填，手调最容易把整个背景判成前景。"),
]
HUE_DEFAULT = 136.0
FIXED = dict(val_lo=0.30, val_hi=0.45, erode=1, watermarks=[], keep_frac=0.05)
PRESET_DIR = os.path.join(BASE, "presets")


def pil_to_pixmap(im):
    im = im.convert("RGBA")
    q = QImage(im.tobytes("raw", "RGBA"), im.width, im.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(q.copy())


def base_id_from_name(name):
    """显示名 → ASCII 建议键：有 pypinyin 就转拼音，没有就返回空（退回 clip_NN）。"""
    if lazy_pinyin:
        s = "_".join(lazy_pinyin(name))
    else:
        s = name
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
    return s[:24].strip("_")


def suggest_cid(name, used):
    """按显示名建议一个当前没被占用的 ASCII clipId（不询问，冲突就加后缀）。"""
    base = base_id_from_name(name)
    if not base:
        n = 1
        while "clip_%02d" % n in used:
            n += 1
        return "clip_%02d" % n
    cid, n = base, 1
    while cid in used:
        n += 1
        cid = "%s_%d" % (base, n)
    return cid


def default_params():
    """工坊的默认抠像参数（= 拖进新视频什么都不改时用的那套）。"""
    p = {"hue_ref": HUE_DEFAULT}
    for key, _name, _lo, _hi, val, _tip in ADV_DEFS:
        p[key] = val
    p.update(FIXED)
    return p


def jsonable_params(p):
    """抠像参数 → 可写进 meta.json 的普通 JSON（watermarks 是 list[tuple]）。"""
    def cv(v):
        if isinstance(v, (list, tuple)):
            return [cv(x) for x in v]
        if isinstance(v, (int, float, str, bool)) or v is None:
            return v
        return str(v)
    return {k: cv(v) for k, v in p.items()}


def detect_crop(files, dark=70.0, margin=2):
    """扫若干帧，取"近黑/深色且均匀"边缘带的并集，返回 (crop, 详情)。

    并集而不是逐帧：黑边宽度逐帧会变 1~2px，逐帧裁会让脚底锚点每帧抖。
    """
    boxes = []
    for p in files:
        rgb = np.asarray(Image.open(p).convert("RGB")).astype(np.float32)
        l = _band(rgb, axis=1)
        r = _band(rgb, axis=1, reverse=True)
        t = _band(rgb, axis=0)
        b = _band(rgb, axis=0, reverse=True)
        boxes.append((l, t, r, b))
    l = max(x[0] for x in boxes) + margin
    t = max(x[1] for x in boxes) + margin
    r = max(x[2] for x in boxes) + margin
    b = max(x[3] for x in boxes) + margin
    h, w = rgb.shape[0], rgb.shape[1]
    detail = "并集 l=%d t=%d r=%d b=%d" % (l, t, r, b)
    if l + t + r + b < 8 or l >= w - 40 or r >= w - 40:
        return None, detail
    return (l, t, w - r, h - b), detail


def _band(rgb, axis, reverse=False, dark=70.0, spread_max=40.0):
    """从边缘往里数"近黑或深色且均匀"的带宽度（按行/列均值）。"""
    rows = rgb.mean(axis=1) if axis == 0 else rgb.mean(axis=0)   # (H,3) 或 (W,3)
    if reverse:
        rows = rows[::-1]
    n = 0
    for row in rows:
        m = float(row.mean())
        spread = float(row.max() - row.min())
        if m < dark and spread < spread_max:
            n += 1
        else:
            break
        if n > rows.shape[0] // 2:
            break
    return n


def hsv_planes(rgb):
    """0~1（或 0~255）RGB → (hue 0~360°, sat 0~1, 明度 mx) 同形状平面。

    keyer.key_frame() 用的是同一套公式；取样、打分、推导闸门都走这里，
    避免三处各写一遍色相换算导致口径不一致。
    """
    a = np.asarray(rgb, dtype=np.float32)
    if a.size and a.max() > 1.5:
        a = a / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    # 同时支持 (H,W,3) 整帧和 (N,3) 展平像素（当前帧取色用的是后者）
    mx, mn = a.max(-1), a.min(-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    d = mx - mn + 1e-6
    hue = np.where(mx == r, ((g - b) / d) % 6,
                   np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
    return hue, sat, mx


def sample_hue_from(rgb):
    """点四角取样：返回 (色相中位数, 饱和度中位数)。

    近黑/近白像素直接丢弃：四角经常是暗角或黑边，它们的色相是噪声，
    会把 hue_ref 带偏到完全抠不掉背景（示例：暗角 155° vs 真实绿幕 99°）。
    """
    h, w, _ = rgb.shape
    k = max(8, min(h, w) // 12)
    px = np.concatenate([c.reshape(-1, 3) for c in
                         (rgb[:k, :k], rgb[:k, -k:], rgb[-k:, :k], rgb[-k:, -k:])])
    hue, sat, mx = hsv_planes(px)
    ok = (mx > 0.10) & (mx < 0.98)
    if not ok.any():
        return 0.0, 0.0
    sat, hue = sat[ok], hue[ok]
    pick = sat >= np.percentile(sat, 60)
    return float(np.median(hue[pick])), float(np.median(sat[pick]))


def region_stats(rgb, rect):
    """一个用户取色框 → 下采样后的 (hue, sat) 一维数组（丢掉近黑/近白/灰噪点）。

    框坐标是「源帧像素」，与裁切框、擦除框同一坐标系；取样作用于未裁切的完整帧，
    所以之后改裁切也不会让已取的点失效。
    """
    a = np.asarray(rgb)
    h, w = a.shape[:2]
    l, t, r, b = (int(round(x)) for x in rect)
    l, t = max(0, min(l, w - 1)), max(0, min(t, h - 1))
    r, b = max(l + 1, min(r, w)), max(t + 1, min(b, h))
    a = a[t:b, l:r]
    if a.size == 0:
        return None
    hh, ww = a.shape[:2]
    step = max(1, int(np.ceil(np.sqrt(hh * ww / 12000.0))))
    hue, sat, mx = hsv_planes(a[::step, ::step])
    ok = (mx > 0.10) & (mx < 0.98) & (sat > 0.03)
    if not ok.any():
        return None
    return hue[ok], sat[ok]


def circular_hue_stats(hue):
    """圆形色相中位数 + 偏差分位。普通 median 在 0/360 跨界时会算错，要迭代收敛。"""
    if hue.size == 0:
        return None
    cur = float(np.median(hue))
    for _ in range(3):
        shifted = ((hue - cur + 180) % 360) - 180 + cur
        cur = float(np.median(shifted))
    href = cur % 360
    dev = np.abs(((hue - href + 180) % 360) - 180)
    # 四角框里常混进暗角/角色像素，色相偏差能到 130°；分位必须只用
    # 「确实接近背景色」的核心像素，否则 hue_out 会被撑到 60°、误伤角色绿色。
    core = dev[dev <= 45]
    if core.size < max(10, hue.size * 0.05):
        core = dev
    return href, float(np.percentile(core, 60)), float(np.percentile(core, 90)), int(hue.size)


def derive_range(regions):
    """多个取色框的 (hue, sat) → 一套数据驱动的闸门参数。

    不再让用户手调 7 个滑块：色相中心/宽度、饱和度上下限全部由样点分布推出来。
    sat_hi 取背景饱和度低分位减一点，保证 ≥90% 的背景像素 ks=1；
    hue_in/out 取色相偏差的 p60 / p90，兼顾绿幕光照渐变和轻微色偏。
    """
    hs = [r[0] for r in regions if r is not None]
    ss = [r[1] for r in regions if r is not None]
    if not hs:
        return None
    hue = np.concatenate(hs)
    sat = np.concatenate(ss)
    st = circular_hue_stats(hue)
    if not st:
        return None
    href, d60, d90, n = st
    s05, s10, s50 = (float(x) for x in np.percentile(sat, [5, 10, 50]))
    hue_in = float(np.clip(round(d60 * 1.2 + 4, 1), 6, 32))
    hue_out = float(np.clip(round(max(d90 * 1.4, hue_in + 10), 1), hue_in + 6, 60))
    sat_lo = max(0.02, round(max(0.0, s10 - 0.10), 2))
    sat_hi = max(sat_lo + 0.02, round(max(0.04, min(s05, s10) - 0.01), 2))
    return {"hue_ref": round(href, 1), "hue_in": hue_in, "hue_out": hue_out,
            "sat_lo": sat_lo, "sat_hi": sat_hi,
            "_info": {"n": n, "hue_dev60": d60, "hue_dev90": d90,
                      "sat05": s05, "sat10": s10, "sat50": s50}}


class Canvas(QWidget):
    """棋盘格 + 居中预览；同一块画布承担三种鼠标工具，切换靠 set_mode()。

    坐标全部用「源帧像素」：rect_image 是源帧尺寸，pix 只是按显示缩放的同一块图；
    所有框（裁切 / 取色框 / 擦除区）都换算回源帧坐标，预览与生成用同一套数。

    三种模式
    --------
    crop   : 拖裁切框四条边 / 四角（默认）；不再提供上下左右数值微调按钮。
    sample : 左键拖一个矩形 = 加一处背景取色框；多点汇总成「取色范围」自动回填闸门。
    erase  : 左键拖一个矩形 = 手动切除角落杂物（写进 params.watermarks）。
    三种模式下右键点已画的框 = 删掉那一处。
    """

    cropEdited = pyqtSignal(object)      # 拖动中：只同步状态行，不重算抠像
    cropCommitted = pyqtSignal(object)   # 松手：重算一次
    sampleAdded = pyqtSignal(object)     # (l, t, r, b) 源帧坐标
    eraseAdded = pyqtSignal(object)
    overlayPicked = pyqtSignal(str, int)  # ("sample"|"erase", 序号)：右键请求删除
    MIN_CROP = 16
    MODE_CROP, MODE_SAMPLE, MODE_ERASE = "crop", "sample", "erase"

    def __init__(self, placeholder="把绿幕视频拖进左边"):
        super().__init__()
        self.pix = None
        self.rect_image = (0, 0)      # 源帧尺寸（crop 的坐标系）
        self.crop = None              # (l, t, r, b)，None = 全帧
        self.editable = False
        self.placeholder = placeholder
        self.mode = self.MODE_CROP
        self.samples = []             # 取色框矩形 [(l,t,r,b), ...] 源帧坐标
        self.erases = []              # 擦除矩形
        self._drag = None             # ("l"/"t"/..."lt"…, 起始 crop, 起始鼠标源坐标)
        self._rubber = None           # 取样/擦除/新建裁切拖框 [x0,y0,x1,y1] 源帧坐标
        self._new_crop = False        # 这一次的 rubber 是「拖出新裁切框」而不是取色/擦除
        self._hover = None
        self.setMinimumSize(200, 140)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_mode(self, mode):
        self.mode = mode if mode in (self.MODE_CROP, self.MODE_SAMPLE, self.MODE_ERASE) else self.MODE_CROP
        self._rubber = None
        if self.mode == self.MODE_SAMPLE:
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif self.mode == self.MODE_ERASE:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def set_overlays(self, samples=None, erases=None):
        self.samples = list(samples or [])
        self.erases = list(erases or [])
        self.update()

    def set_image(self, pix, size=None, crop=None, editable=False):
        self.pix = pix
        self.rect_image = size or ((pix.width(), pix.height()) if pix else (0, 0))
        self.crop = crop
        self.editable = bool(editable)
        self.update()

    def _norm_rubber(self, r, min_size=None):
        """拖框规范化 + 夹到源帧内；点一下（几乎没拖）扩成一小块取样窗口。"""
        iw, ih = self.rect_image
        if not iw or not ih:
            return None
        x0, y0, x1, y1 = r
        l, r_ = sorted((x0, x1))
        t, b = sorted((y0, y1))
        k = max(6, min_size or 6, min(iw, ih) // 50)
        if r_ - l < k or b - t < k:
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            l, r_, t, b = cx - k, cx + k, cy - k, cy + k
        l = max(0, min(int(round(l)), iw - 1))
        t = max(0, min(int(round(t)), ih - 1))
        r_ = max(l + 1, min(int(round(r_)), iw))
        b = max(t + 1, min(int(round(b)), ih))
        return (l, t, r_, b)

    def _to_screen(self, rect):
        g = self._geom()
        if not g or not self.rect_image[0]:
            return None
        x0, y0, rw, rh, _ = g
        s = rw / self.rect_image[0]
        l, t, r, b = rect
        return (x0 + l * s, y0 + t * s, (r - l) * s, (b - t) * s)

    def _draw_overlays(self, p):
        """取色框（柔蓝）/ 擦除区（暗红）/ 正在拖的框（虚线）。"""
        for i, rect in enumerate(self.samples):
            g = self._to_screen(rect)
            if not g:
                continue
            x, y, w, h = g
            p.setPen(QPen(QColor(UI["accent"]), 1))
            p.setBrush(QColor(0, 0, 0, 0))
            p.drawRect(int(x), int(y), max(1, int(w)), max(1, int(h)))
            p.setPen(QColor(UI["accent"]))
            p.drawText(int(x) + 2, int(y) - 3, "%d" % (i + 1))
        for rect in self.erases:
            g = self._to_screen(rect)
            if not g:
                continue
            x, y, w, h = g
            p.fillRect(int(x), int(y), max(1, int(w)), max(1, int(h)), QColor(190, 90, 90, 70))
            p.setPen(QPen(QColor(210, 110, 110), 1))
            p.setBrush(QColor(0, 0, 0, 0))
            p.drawRect(int(x), int(y), max(1, int(w)), max(1, int(h)))
        if self._rubber:
            g = self._to_screen(self._rubber)
            if g:
                x, y, w, h = g
                col = (UI["accent"] if self.mode == self.MODE_CROP else
                       UI["ok"] if self.mode == self.MODE_SAMPLE else UI["error"])
                pen = QPen(QColor(col), 1)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(QColor(0, 0, 0, 0))
                p.drawRect(int(x), int(y), max(1, int(w)), max(1, int(h)))

    def _pick_overlay(self, x, y):
        """右键命中检测：后画的优先（叠在上面的先命中）。"""
        for kind, rects in (("erase", self.erases), ("sample", self.samples)):
            for i in reversed(range(len(rects))):
                l, t, r, b = rects[i]
                if l <= x <= r and t <= y <= b:
                    return kind, i
        return None

    def _geom(self):
        """返回显示矩形 (x0, y0, rw, rh, 缩放)。"""
        if not self.pix or self.pix.isNull():
            return None
        w, h = self.pix.width(), self.pix.height()
        s = min(self.width() / w, self.height() / h)
        rw, rh = max(1, int(w * s)), max(1, int(h * s))
        return (self.width() - rw) // 2, (self.height() - rh) // 2, rw, rh, s

    def _to_img(self, pos):
        g = self._geom()
        if not g:
            return None
        x0, y0, rw, rh, _ = g
        iw, ih = self.rect_image
        if iw <= 0 or ih <= 0:
            return None
        return ((pos.x() - x0) * iw / rw, (pos.y() - y0) * ih / rh)

    def _norm(self, crop):
        iw, ih = self.rect_image
        l, t, r, b = (int(round(v)) for v in crop)
        l = max(0, min(l, iw - self.MIN_CROP))
        t = max(0, min(t, ih - self.MIN_CROP))
        r = max(l + self.MIN_CROP, min(r, iw))
        b = max(t + self.MIN_CROP, min(b, ih))
        return (l, t, r, b)

    def _hit(self, x, y, crop):
        """返回拖拽部位：四角优先，其次四边；容差 8 个屏幕像素。"""
        l, t, r, b = crop
        tol = 8 * self.rect_image[0] / max(1, self._geom()[2])
        near_l, near_r = abs(x - l) <= tol, abs(x - r) <= tol
        near_t, near_b = abs(y - t) <= tol, abs(y - b) <= tol
        inside_x, inside_y = l - tol <= x <= r + tol, t - tol <= y <= b + tol
        if near_l and near_t:
            return "lt"
        if near_r and near_t:
            return "rt"
        if near_l and near_b:
            return "lb"
        if near_r and near_b:
            return "rb"
        if near_l and inside_y:
            return "l"
        if near_r and inside_y:
            return "r"
        if near_t and inside_x:
            return "t"
        if near_b and inside_x:
            return "b"
        return None

    def _apply_drag(self, x, y):
        which, start_crop, sx, sy = self._drag
        l, t, r, b = start_crop
        dx, dy = x - sx, y - sy
        iw, ih = self.rect_image
        if which in ("l", "lt", "lb"):
            l = max(0, min(l + dx, r - self.MIN_CROP))
        if which in ("r", "rt", "rb"):
            r = min(iw, max(r + dx, l + self.MIN_CROP))
        if which in ("t", "lt", "rt"):
            t = max(0, min(t + dy, b - self.MIN_CROP))
        if which in ("b", "lb", "rb"):
            b = min(ih, max(b + dy, t + self.MIN_CROP))
        self.crop = (int(round(l)), int(round(t)), int(round(r)), int(round(b)))
        self.cropEdited.emit(self.crop)
        self.update()

    def mousePressEvent(self, e):
        if not (self.editable and self.pix):
            return super().mousePressEvent(e)
        p = self._to_img(e.position())
        if p is None:
            return
        if e.button() == Qt.MouseButton.RightButton:
            hit = self._pick_overlay(p[0], p[1])
            if hit:
                self.overlayPicked.emit(*hit)
                e.accept()
                return
        if e.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(e)
        if self.mode == self.MODE_CROP:
            hit = self._hit(p[0], p[1], self.crop) if self.crop else None
            if hit:
                self._drag = (hit, tuple(self.crop), p[0], p[1])
                e.accept()
            else:
                # 没有裁切框 / 点在线外：直接拖出一个新框，不用先去点「自动裁边」
                self._rubber = [p[0], p[1], p[0], p[1]]
                self._new_crop = True
                self.update()
                e.accept()
        else:
            self._rubber = [p[0], p[1], p[0], p[1]]
            self._new_crop = False
            self.update()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag:
            p = self._to_img(e.position())
            if p:
                self._apply_drag(p[0], p[1])
            e.accept()
            return
        if self._rubber:
            p = self._to_img(e.position())
            if p:
                self._rubber[2], self._rubber[3] = p[0], p[1]
                self.update()
            e.accept()
            return
        if self.mode == self.MODE_CROP and self.editable and self.crop and self.pix:
            p = self._to_img(e.position())
            hit = self._hit(p[0], p[1], self.crop) if p else None
            if hit != self._hover:
                self._hover = hit
                self._set_cursor(hit)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = None
            self.cropCommitted.emit(tuple(self.crop))
            e.accept()
            return
        if self._rubber:
            new_crop = self._new_crop
            rect = self._norm_rubber(self._rubber, self.MIN_CROP if new_crop else None)
            self._rubber = None
            self._new_crop = False
            self.update()
            if rect:
                if new_crop:
                    self.cropCommitted.emit(rect)
                else:
                    (self.sampleAdded if self.mode == self.MODE_SAMPLE else self.eraseAdded).emit(rect)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def _set_cursor(self, hit):
        cur = {"l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
               "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
               "lt": Qt.CursorShape.SizeFDiagCursor, "rb": Qt.CursorShape.SizeFDiagCursor,
               "rt": Qt.CursorShape.SizeBDiagCursor, "lb": Qt.CursorShape.SizeBDiagCursor}.get(hit)
        self.setCursor(cur or Qt.CursorShape.ArrowCursor)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(UI["preview_bg"]))
        step = 12
        for y in range(0, self.height(), step):
            for x in range(0, self.width(), step):
                if (x // step + y // step) % 2 == 0:
                    p.fillRect(x, y, step, step, QColor(UI["preview_alt"]))
        if not self.pix or self.pix.isNull():
            p.setPen(QColor(UI["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            self._draw_overlays(p)
            return
        g = self._geom()
        x0, y0, rw, rh, _ = g
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawPixmap(x0, y0, rw, rh, self.pix)
        if self.crop and self.rect_image[0]:
            iw, ih = self.rect_image
            l, t, r, b = self.crop
            s = rw / iw
            xl, yt = x0 + l * s, y0 + t * s
            xr, yb = x0 + r * s, y0 + b * s
            # 取色/擦除模式下不暗化框外：要看清角落里到底有什么
            if self.mode == self.MODE_CROP:
                shade = QColor(0, 0, 0, 120)
                for (bx, by, bw, bh) in ((x0, y0, rw, max(0, yt - y0)),
                                         (x0, yb, rw, max(0, y0 + rh - yb)),
                                         (x0, yt, max(0, xl - x0), max(0, yb - yt)),
                                         (xr, yt, max(0, x0 + rw - xr), max(0, yb - yt))):
                    if bw > 0 and bh > 0:
                        p.fillRect(int(bx), int(by), int(bw), int(bh), shade)
            p.setPen(QPen(QColor(UI["accent"]), 1))
            p.drawRect(int(xl), int(yt), int(xr - xl), int(yb - yt))
            if self.editable and self.mode == self.MODE_CROP:
                hs = 4
                for (cx, cy) in ((xl, yt), (xr, yt), (xl, yb), (xr, yb)):
                    p.fillRect(int(cx - hs), int(cy - hs), hs * 2, hs * 2, QColor(UI["accent_strong"]))
                    p.drawRect(int(cx - hs), int(cy - hs), hs * 2, hs * 2)
        self._draw_overlays(p)



class VideoList(QListWidget):
    dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        self.dropped.emit([p for p in paths if p.lower().endswith(EXTS)])
        e.acceptProposedAction()


class SliderRow(QWidget):
    changed = pyqtSignal()

    def __init__(self, name, lo, hi, val, tip):
        super().__init__()
        self.lo, self.hi = lo, hi
        self.setToolTip(tip)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lab = QLabel(name)
        lab.setFixedWidth(62)
        self.sld = QSlider(Qt.Orientation.Horizontal)
        self.sld.setRange(0, 1000)
        self.sld.setMinimumWidth(46)
        self.spb = QDoubleSpinBox()
        self.spb.setRange(lo, hi)
        self.spb.setDecimals(2 if hi <= 1 else 1)
        self.spb.setSingleStep(round((hi - lo) / 100, 3))
        self.spb.setFixedWidth(68)
        lay.addWidget(lab)
        lay.addWidget(self.sld, 1)
        lay.addWidget(self.spb)
        self.sld.valueChanged.connect(self._from_slider)
        self.spb.valueChanged.connect(self._from_spin)
        self.set_value(val)

    def _from_slider(self, v):
        val = self.lo + (self.hi - self.lo) * v / 1000.0
        if abs(val - self.spb.value()) > 1e-6:
            self.spb.blockSignals(True)
            self.spb.setValue(val)
            self.spb.blockSignals(False)
        self.changed.emit()

    def _from_spin(self, val):
        v = int(round((val - self.lo) / (self.hi - self.lo) * 1000)) if self.hi > self.lo else 0
        if v != self.sld.value():
            self.sld.blockSignals(True)
            self.sld.setValue(v)
            self.sld.blockSignals(False)
        self.changed.emit()

    def set_value(self, val):
        v = int(round((val - self.lo) / (self.hi - self.lo) * 1000))
        self.sld.blockSignals(True)
        self.sld.setValue(v)
        self.sld.blockSignals(False)
        self.spb.blockSignals(True)
        self.spb.setValue(val)
        self.spb.blockSignals(False)

    def value(self):
        return self.spb.value()


class NewPackDialog(QDialog):
    """新建主题：主题 id / 名称 / 新动画的默认导出画布（pack.V2_DEFAULT_CANVAS）。

    v2 里画布归每段动画自己（meta.json），主题只是绑定索引；这里的画布只作为
    将来在本主题下新生成动画的导出模板，不决定窗口比例、不要求全系列统一。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建主题")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_id = QLineEdit("neko")
        self.ed_id.setPlaceholderText("ASCII，如 neko / cat")
        self.ed_role = QLineEdit()
        self.ed_role.setPlaceholderText("菜单/文档里显示的角色名，可中文")
        row = QHBoxLayout()
        self.sp_w = QSpinBox()
        self.sp_w.setRange(192, 1024)
        self.sp_w.setSingleStep(32)
        self.sp_w.setValue(pack.V2_DEFAULT_CANVAS[0])
        self.sp_h = QSpinBox()
        self.sp_h.setRange(192, 1024)
        self.sp_h.setSingleStep(32)
        self.sp_h.setValue(pack.V2_DEFAULT_CANVAS[1])
        row.addWidget(self.sp_w)
        row.addWidget(QLabel("×"))
        row.addWidget(self.sp_h)
        row.addStretch(1)
        form.addRow("主题 id", self.ed_id)
        form.addRow("名称", self.ed_role)
        form.addRow("新动画导出画布", row)
        lay.addLayout(form)
        tip = QLabel("主题只是「状态 → 动画」的绑定索引，本身没有画布。这里的尺寸只用于"
                     "本主题下新生成动画的导出模板；每段动画的画布写在自己的 meta.json 里，"
                     "不同段允许不同尺寸，切到比例不同的段时窗口会按段自适应（跳一下）。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:%s" % UI["warn"])
        lay.addWidget(tip)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                             QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("创建")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self):
        pid = re.sub(r"[^0-9A-Za-z_\-]+", "_", self.ed_id.text().strip()).strip("_").lower()
        return pid or "pack", self.ed_role.text().strip() or pid or "pack", \
            (self.sp_w.value(), self.sp_h.value())


class GenThread(QThread):
    msg = pyqtSignal(str)
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, spec):
        super().__init__()
        self.spec = spec
        self.cancelled = False

    def run(self):
        try:
            self.done.emit(build_frames.build_clip(self.spec, log=self.msg.emit))
        except RuntimeError as e:
            # 输入校验 / ffmpeg 失败：给用户一句话，不糊一屏堆栈
            self.failed.emit(str(e))
            logging.exception("build_clip 失败（RuntimeError）")
        except Exception:
            self.failed.emit("生成失败（未预期错误）：%s\n%s" % (sys.exc_info()[1], traceback.format_exc()))
            logging.exception("build_clip 失败（未预期错误）")


class Workshop(QWidget):
    def __init__(self, origin_state="", theme_id=""):
        super().__init__()
        self.setWindowTitle("素材工坊")
        self.origin_state = origin_state or ""    # 从设置带过来的目标状态（生成完默认绑定）
        self.start_theme = theme_id or ""
        self.videos = []          # [{path, cid, name, frames, fps}]
        self._last_row = -1
        self.raw_rgb = None
        self.raw_size = None      # 原片尺寸（遮罩用）
        self.src_size = None      # 源帧像素尺寸（crop 的坐标系；已有段没有源分辨率）
        self.crop = None          # 当前行的裁切 (l,t,r,b)；自动与手动共用这一个值
        self._crop_guard = False
        self._last_valid_crop = None
        self._fit_cache = {}      # (cid,crop,params) → 三帧合并前景框，保证预览缩放与生成完全一致
        # 用户取色框 / 手动擦除框：都是「源帧坐标」，挂在每一行视频上（切行互不串）。
        # 这里缓存当前行，切行时从 v["samples"] / v["erase_rects"] 恢复。
        self.sample_regions = []  # [{"rect":(l,t,r,b), "hue":array, "sat":array, "frame":i}]
        self.erase_rects = []     # [(l,t,r,b), ...] → params.watermarks
        self.tool = Canvas.MODE_CROP
        self.solid_bg = False     # 「抠图后」预览底：棋盘格 / 实色（看 despill 绿边）
        self.gen = None
        self.queue = []           # 批量排队 [(video, cid, name)]
        self.last_out_dir = ""    # 最近一次生成结果目录：「打开当前动画目录」优先定位到这里
        # 首次自动取样的标记放在每个 video 字典上（v["auto_key_done"]），不是窗口级：
        # 同一会话导入多段视频时，每段都要各自判一次。
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self.render)
        # 播放预览：时间轴挪到预览图下方之后，才有「就地看一遍」的地方。
        # 用固定 tick 推进帧号，每帧都走 render()（不另建解码缓存，代价可接受）。
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._play_tick)
        self._build_ui()
        self._fit_to_screen()
        self.refresh_packs()
        # 启动第一行就把「你跑的到底是哪份代码」打在日志里，排「改了没生效」时先看这行
        self.log("工坊文件：%s" % os.path.abspath(__file__))
        self.log("修改时间：%s（若不是你刚改的时间，说明启动的是旧副本/旧进程）"
                 % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(__file__))))

    def _fit_to_screen(self):
        """初始尺寸 = 可用屏幕的 88%（上限 1320×880）并居中，永不撑出桌面/任务栏。"""
        scr = self.screen() or QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        if avail is None:
            self.resize(1320, 880)
            return
        self.setMinimumSize(min(720, avail.width()), min(520, avail.height()))
        w = min(1320, max(self.minimumWidth(), int(avail.width() * 0.88)))
        h = min(880, max(self.minimumHeight(), int(avail.height() * 0.88)))
        self.resize(w, h)
        self.move(max(avail.x(), avail.x() + (avail.width() - w) // 2),
                  max(avail.y(), avail.y() + (avail.height() - h) // 2))
        self.log("窗口：可用屏幕 %d×%d（含任务栏区域 %d×%d）→ 初始 %d×%d"
                 % (avail.width(), avail.height(), scr.geometry().width(),
                    scr.geometry().height(), w, h))

    # ---------- 界面 ----------
    def _build_ui(self):
        # 共用配色表：旧版只设了 QWidget{color} 没设 background，浅色字直接落在
        # 系统默认 #efefef 白底上（实测），这里两个窗口统一成同一套低对比深色。
        self.setStyleSheet(QSS)
        self.setPalette(dark_palette())       # QSS 不改 QPalette，Canvas 自绘要用
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        head = QLabel("① 导入绿幕视频   →   ② 取色 / 裁切 / 擦除（拖框即自动抠净）   →   ③ 改 assetId · 选主题 · 命名 · 导出")
        head.setStyleSheet("color:%s; font-size:13px; font-weight:600; padding:6px 10px; "
                           "background:%s; border-radius:5px;" % (UI["accent"], UI["panel"]))
        outer.addWidget(head)
        # 2×2 分区：左列（上导入 / 下导出），右列（上三预览 / 下参数+步骤）。
        # 两列、两行都是 QSplitter，用户能自己拖分栏比例；左列不再有一整栏空白列表。
        main = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(main, 1)
        left = QSplitter(Qt.Orientation.Vertical)
        right = QSplitter(Qt.Orientation.Vertical)
        for s in (main, left, right):
            s.setChildrenCollapsible(False)
        main.addWidget(left)
        main.addWidget(right)
        main.setStretchFactor(0, 31)
        main.setStretchFactor(1, 69)

        # ① 左上：导入视频（列表只显示文件名 + 选中帧预览 + 视频信息）
        g1 = QGroupBox("① 导入视频")
        lv = QVBoxLayout(g1)
        lv.setSpacing(5)
        self.list = VideoList()
        self.list.dropped.connect(self.add_files)
        self.list.currentRowChanged.connect(self.on_select)
        self.list.itemSelectionChanged.connect(self.on_select)
        lv.addWidget(self.list, 1)
        rowb = QHBoxLayout()
        b_add = QPushButton("添加视频…")
        b_add.clicked.connect(self.pick_files)
        b_del = QPushButton("移除")
        b_del.clicked.connect(self.remove_cur)
        rowb.addWidget(b_add)
        rowb.addWidget(b_del)
        rowb.addStretch(1)
        lv.addLayout(rowb)
        self.thumb = QLabel("选中视频后显示首帧")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setMinimumHeight(84)
        self.thumb.setMaximumHeight(132)
        self.thumb.setStyleSheet("color:%s;background:%s;border:1px solid %s;border-radius:4px;"
                                 % (UI["faint"], UI["preview_bg"], UI["border"]))
        lv.addWidget(self.thumb)
        self.lb_info = QLabel("尚未导入视频")
        self.lb_info.setWordWrap(True)
        self.lb_info.setStyleSheet("color:%s" % UI["muted"])
        lv.addWidget(self.lb_info)
        left.addWidget(g1)

        # ② 右上：抠图前 / 抠图后 / alpha 三视图并列，同一帧一次算完三张图
        gprev = QGroupBox("② 抠像预览 · 三视图并列")
        pv = QVBoxLayout(gprev)
        pv.setSpacing(5)
        # 顶栏放「动手前要先按一下」的东西，按用途分成三段：
        # 裁切（自动裁黑边 / 重跑裁边 / 裁切）→ 取色（多帧取色 / 当前帧取色 / 加取色框）
        # → 擦除（矩形擦除 / 清空标注）。参数调节区只留滑块，两块不再各放一半同类按钮。
        toolrow = QHBoxLayout()
        toolrow.setSpacing(6)
        self.cb_crop = QCheckBox("自动裁黑边")
        self.cb_crop.setToolTip("扫全片近黑/深色均匀边，取并集裁一次；手动拖框后会自动关掉")
        self.cb_crop.setChecked(True)
        self.cb_crop.stateChanged.connect(self.on_crop_toggled)
        toolrow.addWidget(self.cb_crop)
        self.b_auto_crop = QToolButton()
        self.b_auto_crop.setText("重跑裁边")
        self.b_auto_crop.setToolTip("重新跑一次自动裁边；之后再手动拖框微调")
        self.b_auto_crop.setStyleSheet(CHIP_PLAIN_QSS)
        self.b_auto_crop.clicked.connect(self.rerun_auto_crop)
        toolrow.addWidget(self.b_auto_crop)

        self.tools = {}
        b_crop = QToolButton()
        b_crop.setText("裁切")
        b_crop.setToolTip("在「抠图后」预览里拖裁切框的四条边 / 四角；不再需要数值微调")
        b_crop.setCheckable(True)
        b_crop.setStyleSheet(CHIP_PLAIN_QSS)
        b_crop.clicked.connect(lambda _on: self.set_tool(Canvas.MODE_CROP))
        toolrow.addWidget(b_crop)
        self.tools[Canvas.MODE_CROP] = b_crop

        # 「多帧取色」= 原来的「一键抠净」：抽 9 帧四角取样 → 候选闸门实跑打分 → 只回填更优解。
        # 放在「当前帧取色」左边，因为它更稳、更该先按。
        self.b_auto = QToolButton()
        self.b_auto.setText("多帧取色")
        self.b_auto.setToolTip("抽十来帧四角取样、剔除离群后取中位数；候选闸门实跑打分，"
                               "只回填更优解（比当前参数不优就不动）")
        self.b_auto.setStyleSheet(CHIP_SAMPLE_QSS)
        self.b_auto.clicked.connect(lambda: self.auto_key(silent=False))
        toolrow.addWidget(self.b_auto)

        self.b_hue = QToolButton()
        self.b_hue.setText("当前帧取色")
        self.b_hue.setToolTip("对当前预览帧的四角取背景色；只在必要时压低饱和度闸门，不覆盖手调参数。"
                              "拖时间轴挑一帧四角干净的绿幕再按，效果最准")
        self.b_hue.setStyleSheet(CHIP_SAMPLE_QSS)
        self.b_hue.clicked.connect(self.sample_hue)
        toolrow.addWidget(self.b_hue)

        b_sample = QToolButton()
        b_sample.setText("加取色框")
        b_sample.setToolTip("在「抠图后」预览里拖一个框取背景色 → 自动对准色相并重算闸门；"
                            "可叠加多个；右键点框删除。纯点击无效，要拖出矩形")
        b_sample.setCheckable(True)
        b_sample.setStyleSheet(CHIP_SAMPLE_QSS)
        b_sample.clicked.connect(lambda _on: self.set_tool(Canvas.MODE_SAMPLE))
        toolrow.addWidget(b_sample)
        self.tools[Canvas.MODE_SAMPLE] = b_sample

        b_erase = QToolButton()
        b_erase.setText("矩形擦除")
        b_erase.setToolTip("拖框把角落里的杂物直接切掉；右键点框可删除")
        b_erase.setCheckable(True)
        b_erase.setStyleSheet(CHIP_ERASE_QSS)
        b_erase.clicked.connect(lambda _on: self.set_tool(Canvas.MODE_ERASE))
        toolrow.addWidget(b_erase)
        self.tools[Canvas.MODE_ERASE] = b_erase

        self.b_clear_marks = QToolButton()
        self.b_clear_marks.setText("清空标注")
        self.b_clear_marks.setToolTip("清空全部取色框 + 矩形擦除框")
        self.b_clear_marks.setStyleSheet(CHIP_ERASE_QSS)
        self.b_clear_marks.clicked.connect(lambda: (self.clear_overlays("sample"),
                                                    self.clear_overlays("erase")))
        toolrow.addWidget(self.b_clear_marks)
        self.tools[Canvas.MODE_CROP].setChecked(True)
        toolrow.addStretch(1)
        pv.addLayout(toolrow)

        # 工具栏下面一行：标注/裁切的当前状态。原来散在「参数调节」里的两行
        # 说明合并成这一条，一行显示完（不换行，避免把预览区越挤越高）。
        self.lb_marks = QLabel("裁切 全帧 ｜ 取色框 0 ｜ 擦除框 0 ｜ 拖框即重算 · 右键删框")
        self.lb_marks.setStyleSheet("color:%s; font-size:11px;" % UI["muted"])
        self.lb_marks.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pv.addWidget(self.lb_marks)

        views = QHBoxLayout()
        views.setSpacing(6)
        self.canvas_raw = Canvas("导入视频后显示原片")
        self.canvas = Canvas("导入视频后显示抠像结果（选「加取色框 / 矩形擦除」可在此拖框）")
        self.canvas_alpha = Canvas("导入视频后显示 alpha 通道")
        for c in (self.canvas_raw, self.canvas, self.canvas_alpha):
            c.setMinimumHeight(150)
        self.canvas.cropEdited.connect(self.on_crop_dragged)
        self.canvas.cropCommitted.connect(self.on_crop_committed)
        self.canvas.sampleAdded.connect(self.on_sample_added)
        self.canvas.eraseAdded.connect(self.on_erase_added)
        self.canvas.overlayPicked.connect(self.on_overlay_picked)
        for c, title in ((self.canvas_raw, "抠图前（源帧）"),
                         (self.canvas, "抠图后 · 可交互（右键删框）"),
                         (self.canvas_alpha, "alpha 通道（白=不透明）")):
            col = QVBoxLayout()
            col.setSpacing(3)
            cap = QLabel(title)
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap.setStyleSheet("color:%s; font-size:11px;" % UI["muted"])
            col.addWidget(cap)
            col.addWidget(c, 1)
            views.addLayout(col, 1)
        pv.addLayout(views, 1)
        # 预览图下方：帧时间轴（播放 / 暂停 / 滑块 / 帧号），占用原来顶栏那一行
        trow = QHBoxLayout()
        trow.setSpacing(6)
        # 用 Qt 自带的标准图标：不依赖字体里有没有 ▶ / ⏸ 这些字形（雅黑就没有，会画成方框）
        self.b_play = QToolButton()
        self.b_play.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPlay))
        self.b_play.setToolTip("连续播放预览（空格键也可）")
        self.b_play.setFixedSize(28, 22)
        self.b_play.setCheckable(True)
        self.b_play.clicked.connect(self.toggle_play)
        trow.addWidget(self.b_play)
        self.b_pause = QToolButton()
        self.b_pause.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPause))
        self.b_pause.setToolTip("暂停播放（空格键也可）")
        self.b_pause.setFixedSize(28, 22)
        self.b_pause.clicked.connect(self.pause_play)
        trow.addWidget(self.b_pause)
        self.frame_sld = QSlider(Qt.Orientation.Horizontal)
        self.frame_sld.setRange(0, 0)
        self.frame_sld.setMinimumWidth(80)
        self.frame_sld.valueChanged.connect(self.on_frame)
        trow.addWidget(self.frame_sld, 1)
        self.lb_frame = QLabel("0 / 0")
        self.lb_frame.setFixedWidth(66)
        trow.addWidget(self.lb_frame)
        self.lb_perf = QLabel("")
        self.lb_perf.setStyleSheet("color:%s" % UI["faint"])
        trow.addWidget(self.lb_perf)
        pv.addLayout(trow)

        # 「实色底」是看预览用的开关，跟「预览来源」一样属于这一屏的显示设置，放同一行。
        self.cb_solid = QCheckBox("实色底")
        self.cb_solid.setToolTip("「抠图后」预览改铺纯色底而不是透明棋盘格："
                                  "只有纯色底才能看出「去溢色」有没有削掉绿边、边缘有没有残留绿"
                                  "（只影响预览，不影响导出）")
        self.cb_solid.stateChanged.connect(self.on_solid_toggled)
        src_row = QHBoxLayout()
        src_row.setSpacing(10)
        src_row.addWidget(self.cb_solid)
        self.lb_src = QLabel("预览来源：—")
        self.lb_src.setStyleSheet("color:%s" % UI["muted"])
        src_row.addWidget(self.lb_src, 1)
        pv.addLayout(src_row)
        right.addWidget(gprev)

        # 右下：靠左「参数调节」，靠右「步骤 / 日志」
        lower = QSplitter(Qt.Orientation.Horizontal)
        lower.setChildrenCollapsible(False)

        gparam = QGroupBox("参数调节")
        qv = QVBoxLayout(gparam)
        qv.setSpacing(5)
        # 裁切 / 取色 / 擦除 全部搬到预览顶栏了，这里是从「饱和度下限」直接开始的纯滑块区

        # 主参数：只留调了肉眼看得见的三个（依据见 MAIN_DEFS 注释）
        self.rows = {}
        for key, name, lo, hi, val, tip in MAIN_DEFS:
            r = SliderRow(name, lo, hi, val, tip)
            r.changed.connect(self.schedule_render)
            qv.addWidget(r)
            self.rows[key] = r

        # 其余参数收进折叠：不是删掉，是不让 7 个滑块一起制造「调了没用」的错觉
        self.adv_btn = QToolButton()
        self.adv_btn.setText("更多参数（色相 / 地板 / 去溢色 / 预设）")
        self.adv_btn.setCheckable(True)
        self.adv_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.adv_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.adv_btn.clicked.connect(self.toggle_adv)
        qv.addWidget(self.adv_btn)
        self.adv = QWidget()
        av = QVBoxLayout(self.adv)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(3)
        for key, name, lo, hi, val, tip in MORE_DEFS + ADV_DEFS[-1:]:
            r = SliderRow(name, lo, hi, val, tip)
            r.changed.connect(self.schedule_render)
            av.addWidget(r)
            self.rows[key] = r
        rowk = QHBoxLayout()
        self.cb_keep = QCheckBox("只保留最大连通块（去水印/光晕）")
        self.cb_keep.setChecked(True)
        self.cb_keep.stateChanged.connect(self.schedule_render)
        rowk.addWidget(self.cb_keep)
        rowk.addStretch(1)
        b_reset = QPushButton("恢复默认")
        b_reset.clicked.connect(self.reset_adv)
        rowk.addWidget(b_reset)
        av.addLayout(rowk)
        pre_row = QHBoxLayout()
        pre_row.addWidget(QLabel("预设"))
        self.cb_preset = QComboBox()
        self.cb_preset.currentIndexChanged.connect(self.load_preset)
        pre_row.addWidget(self.cb_preset, 1)
        b_save = QPushButton("存为…")
        b_save.clicked.connect(self.save_preset)
        pre_row.addWidget(b_save)
        av.addLayout(pre_row)
        self.adv_scroll = QScrollArea()
        self.adv_scroll.setWidgetResizable(True)
        self.adv_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.adv_scroll.setMinimumHeight(150)
        self.adv_scroll.setMaximumHeight(300)
        self.adv_scroll.setWidget(self.adv)
        self.adv_scroll.setVisible(False)
        qv.addWidget(self.adv_scroll, 1)
        qv.addStretch(1)
        lower.addWidget(gparam)

        # 右下只留「跑成什么样」的反馈：一行状态 + 一行结果 + 日志。
        # 步骤说明、成品画布、导出后对比图都去掉了 —— 成品画布与生成同一函数，
        # 真要看结果就去导出目录，别在这儿再占一块视觉面积。
        gstep = QGroupBox("日志")
        sv = QVBoxLayout(gstep)
        sv.setSpacing(5)
        self.lb_state = QLabel("参数 → 结果：等待载入视频")
        # 不要用 Consolas：等宽西文字体里没有 ✓ / ⚠ / 中文字形，会渲染成方框
        self.lb_state.setStyleSheet("color:%s" % UI["accent"])
        self.lb_state.setWordWrap(True)
        sv.addWidget(self.lb_state)
        self.lb_result = QLabel("")
        self.lb_result.setWordWrap(True)
        self.lb_result.setStyleSheet("color:%s;font-weight:600" % UI["ok"])
        sv.addWidget(self.lb_result)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(80)
        sv.addWidget(self.log_box, 1)
        lower.addWidget(gstep)
        lower.setStretchFactor(0, 55)
        lower.setStretchFactor(1, 45)
        lower.setSizes([420, 340])
        right.addWidget(lower)
        right.setStretchFactor(0, 58)
        right.setStretchFactor(1, 42)

        # ③ 左下：导出信息栏（主题 / 动画名 / 开始导出 / 打开目录）
        g3 = QGroupBox("③ 导出")
        rv = QVBoxLayout(g3)
        rv.setSpacing(5)
        form3 = QFormLayout()
        form3.setSpacing(5)
        prow = QHBoxLayout()
        self.cb_pack = QComboBox()
        self.cb_pack.setMinimumWidth(120)
        prow.addWidget(self.cb_pack, 1)
        b_newpack = QPushButton("新建…")
        b_newpack.setToolTip("新建一张主题（状态→动画的绑定索引），空映射起步")
        b_newpack.clicked.connect(self.new_pack)
        prow.addWidget(b_newpack)
        form3.addRow("主题", prow)
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("动画名称（如「得意状」）")
        self.ed_name.editingFinished.connect(self.save_row_meta)
        form3.addRow("动画名称", self.ed_name)
        self.ed_id = QLineEdit()
        self.ed_id.setPlaceholderText("ASCII，如 dsm_yawn；留空按名称自动生成")
        self.ed_id.textChanged.connect(self.on_id_text)
        self.ed_id.editingFinished.connect(self.apply_manual_id)
        self.ed_id.setToolTip("动画资产唯一键，也是帧目录名 animations/<assetId>/；"
                              "只能 ASCII。改动会做路径可用性检验。")
        form3.addRow("assetId", self.ed_id)
        self.lb_id_status = QLabel("")
        self.lb_id_status.setWordWrap(True)
        form3.addRow("", self.lb_id_status)
        rv.addLayout(form3)
        act = QHBoxLayout()
        act.setSpacing(5)
        self.b_gen = QPushButton("开始导出")
        self.b_gen.setStyleSheet("border:1px solid %s; font-weight:600;" % UI["accent_strong"])
        self.b_gen.clicked.connect(self.generate)
        self.b_cancel = QPushButton("取消")
        self.b_cancel.setEnabled(False)
        self.b_cancel.clicked.connect(self.cancel)
        self.b_open = QPushButton("打开导出目录")
        self.b_open.setToolTip("定位到刚生成的 animations/<assetId>/ 帧目录，而不是它的上一级")
        self.b_open.clicked.connect(self.open_out)
        for b in (self.b_gen, self.b_cancel, self.b_open):
            act.addWidget(b)
        rv.addLayout(act)
        # 进度条跟着「开始导出」走：它在哪被点，反馈就在哪，不用去右边日志区找
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        rv.addWidget(self.prog)
        self.lb_msg = QLabel("")
        self.lb_msg.setWordWrap(True)
        self.lb_msg.setStyleSheet("color:%s" % UI["muted"])
        rv.addWidget(self.lb_msg)
        hint = QLabel("导出写到 packs/animations/<assetId>/（含 meta.json）。"
                      "若从设置的空状态进来，会自动绑定到那个状态。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:%s" % UI["faint"])
        rv.addWidget(hint)
        rv.addStretch(1)
        left.addWidget(g3)

        # 分栏比例：左窄右宽；左列上下各半，右列上预览下参数
        g1.setMinimumWidth(220)
        g3.setMinimumWidth(220)
        gprev.setMinimumWidth(540)
        gparam.setMinimumWidth(280)
        gstep.setMinimumWidth(240)
        left.setStretchFactor(0, 52)
        left.setStretchFactor(1, 48)
        left.setSizes([300, 280])
        main.setSizes([340, 900])
        right.setSizes([400, 300])

    def toggle_adv(self, on):
        """折叠区只管显示，不再擅自改窗口高度（2×2 布局里参数区本来就有位置）。"""
        self.adv_scroll.setVisible(bool(on))
        self.adv_btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        if on:
            self.adv_scroll.verticalScrollBar().setValue(0)

    # ---------- pack ----------
    def refresh_packs(self):
        """v2：下拉显示主题名，data 存主题 id；生成写到全局 animations/ 目录。"""
        themes = pack.list_themes()
        self.cb_pack.clear()
        for t in themes:
            self.cb_pack.addItem(t["name"], t["id"])
        want = self.start_theme or pack.active_theme()
        i = self.cb_pack.findData(want)
        if i >= 0:
            self.cb_pack.setCurrentIndex(i)
        elif self.cb_pack.count():
            self.cb_pack.setCurrentIndex(0)
        if self.origin_state and hasattr(self, "log_box"):
            self.log("来自设置：目标主题 %s / 状态「%s」（生成完自动绑定）"
                     % (want, pack.STATE_CN_V2.get(self.origin_state, self.origin_state)))

    def cur_pack(self):
        """v2：返回当前主题 id（不是显示名）。动画本身不归属任何主题。"""
        return self.cb_pack.currentData() or pack.active_theme()

    def pack_conf(self):
        """导出参数：画布取当前主题 idle 段的 meta.canvas（v2 里画布归动画，不归主题）。

        没有 idle 段时回退全局默认。质量沿用历史值 80，不进 meta.json。
        """
        d = {"w": pack.V2_DEFAULT_CANVAS[0], "h": pack.V2_DEFAULT_CANVAS[1],
             "fps": 24, "fmt": "webp", "quality": 80}
        try:
            th = pack.load_theme(self.cur_pack())
            tmpl = th.get("exportCanvas")
            pool = (th["states"].get("idle") or {}).get("clipIds") or []
            m = pack.load_asset(pool[0]) if pool else None
            w, h = pack.aspect_pair(tmpl or (m or {}).get("canvas"))
            d.update(w=w, h=h, fps=int((m or {}).get("fps") or 24),
                     fmt=(m or {}).get("format") or "webp")
        except Exception:
            pass
        return d

    def pack_clip_ids(self):
        """v2：assetId 全局唯一，冲突检查对全库做（不再按主题隔离）。"""
        return set(pack.list_assets())

    def new_pack(self):
        """v2：新建的是「主题」（一张纯绑定索引），不再建 packs/<id>/frames 目录。"""
        dlg = NewPackDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pid, role, canvas = dlg.values()
        if os.path.exists(pack.theme_path(pid)):
            QMessageBox.warning(self, "已存在", "packs\\themes\\%s.json 已经存在，直接选它就行" % pid)
            return
        th = pack.empty_theme(pid, name=role)
        th["exportCanvas"] = {"w": int(canvas[0]), "h": int(canvas[1])}
        pack.write_theme(pid, th)
        self.refresh_packs()
        i = self.cb_pack.findData(pid)
        if i >= 0:
            self.cb_pack.setCurrentIndex(i)
        self.log("新建主题 %s（角色：%s）· 空映射：13 行状态均为 0 段" % (pid, role))

    # ---------- 文件 ----------
    def pick_files(self):
        ps, _ = QFileDialog.getOpenFileNames(self, "选择绿幕视频", ROOT,
                                             "视频 (*.mp4 *.mov *.webm *.mkv *.avi)")
        if ps:
            self.add_files(ps)

    def add_files(self, paths):
        used = {v["cid"] for v in self.videos}
        for p in paths:
            if any(v["path"] == p for v in self.videos):
                continue
            name = os.path.splitext(os.path.basename(p))[0]
            cid = suggest_cid(name, used)       # 中文名 → 拼音/ASCII；转不出来就 clip_NN
            used.add(cid)
            self.videos.append({"path": p, "cid": cid, "name": name, "frames": [], "fps": None,
                                "manual": False})
            self._refresh_item(len(self.videos) - 1)
        self.log("添加 %d 个视频，共 %d 个（支持一次多选排队）" % (len(paths), len(self.videos)))
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)

    def focus_clip(self, cid):
        """--clip：把 packs/<当前包>/frames/<clipId> 的成品帧插成列表首行并选中。

        工坊列表本来是「待导入的源视频」；已有成品段没有源视频文件，所以这里用
        「已有段」行直接读成品帧，方便从设置面板跳过来核对/改参数。
        """
        if not cid:
            return
        clip = pack.load_asset(cid)
        if not clip:
            self.log("定位失败：animations/ 里没有 assetId=%s" % cid)
            return
        d = pack.asset_dir(cid)
        frames = sorted(glob.glob(os.path.join(d, "f*.webp")) + glob.glob(os.path.join(d, "f*.png")))
        if not frames:
            self.log("定位失败：%s 没有成品帧目录（%s）" % (cid, d))
            return
        fps = self.pack_conf()["fps"]
        # 同一段只保留一条「已有段」行
        for i in reversed(range(len(self.videos))):
            if self.videos[i].get("existing") and self.videos[i]["cid"] == cid:
                self.list.takeItem(i)
                del self.videos[i]
        self.videos.insert(0, {"path": d, "cid": cid,
                               "name": clip.get("displayName") or cid,
                               "frames": frames, "fps": fps, "manual": True, "existing": True,
                               "crop": clip.get("crop"),
                               # 复抠已有段时把上次的擦除框带回来（取色框含像素数组，不入库）
                               "erase_rects": [tuple(x) for x in (clip.get("eraseRects") or [])],
                               "samples": []})
        self.list.insertItem(0, QListWidgetItem(""))
        self._refresh_item(0)
        self.list.setCurrentRow(0)
        self.log("已定位到成品段 %s（%d 帧，%s）" % (cid, len(frames), d))

    def _refresh_item(self, row):
        v = self.videos[row]
        it = self.list.item(row) or QListWidgetItem("", self.list)
        if v.get("existing"):
            info = "已有段 %s    [%s]   %d 帧（成品）" % (v["name"], v["cid"], len(v["frames"]))
        else:
            info = "%s" % os.path.basename(v["path"])
            if v["frames"]:
                info += "   [%s]   %d 帧" % (v["cid"], len(v["frames"]))
        it.setText(info)

    def remove_cur(self):
        rows = sorted({i.row() for i in self.list.selectedIndexes()}, reverse=True) or \
               ([self.list.currentRow()] if self.list.currentRow() >= 0 else [])
        for r in rows:
            if 0 <= r < len(self.videos):
                self.list.takeItem(r)
                del self.videos[r]
        if self.videos:
            self.list.setCurrentRow(min(rows[-1] if rows else 0, len(self.videos) - 1))
        else:
            self.pause_play()                 # 没有行可放，别让定时器对着空列表空转
            self.frame_sld.blockSignals(True)
            self.frame_sld.setRange(0, 0)
            self.frame_sld.setValue(0)
            self.frame_sld.blockSignals(False)
            self.lb_frame.setText("0 / 0")
            self.lb_perf.setText("")
            self.raw_rgb = None
            self.src_size = None
            self.crop = None
            for c in (self.canvas_raw, self.canvas, self.canvas_alpha):
                c.set_image(None)
            self.thumb.setText("选中视频后显示首帧")
            self.thumb.setPixmap(QPixmap())
            self.lb_info.setText("尚未导入视频")
            self.sample_regions, self.erase_rects = [], []
            self._sync_overlays()
            self._set_crop_controls_enabled(False)
            self._sync_crop_label()
            self.lb_result.setText("")

    def save_row_meta(self):
        row = self.list.currentRow()
        if not (0 <= row < len(self.videos)):
            return
        v = self.videos[row]
        name = self.ed_name.text().strip()
        if name and name != v["name"]:
            v["name"] = name
            if not v.get("manual"):
                self._assign_cid(v)
        self.ed_name.setText(v["name"])
        self.ed_id.setText(v["cid"])
        self._refresh_item(row)

    def _assign_cid(self, v):
        """显示名改了 → 重新建议 ASCII 键；撞上目标 pack 里已有的段就问你复用还是新建。"""
        used = {x["cid"] for x in self.videos if x is not v}
        cid = suggest_cid(v["name"], used)
        if cid in self.pack_clip_ids() and cid != v["cid"]:
            box = QMessageBox(self)
            box.setWindowTitle("clipId 已存在")
            box.setIcon(QMessageBox.Icon.Question)
            box.setText("packs/%s 里已经有「%s」这一段了。\n\n"
                        "复用 = 覆盖它（旧帧自动备份到 .backup/，可回滚）\n"
                        "新建 = 加后缀另存一份" % (self.cur_pack(), cid))
            b_reuse = box.addButton("复用（覆盖）", QMessageBox.ButtonRole.AcceptRole)
            b_new = box.addButton("新建（加后缀）", QMessageBox.ButtonRole.ActionRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is b_new:
                cid = suggest_cid(v["name"], used | self.pack_clip_ids())
                self.log("clipId 重复：改用 %s（另存一份）" % cid)
            elif box.clickedButton() is b_reuse:
                self.log("clipId 重复：复用 %s（生成时覆盖，旧帧进 .backup/）" % cid)
            else:
                return
        v["cid"] = cid
        return cid

    def _id_status(self, cid):
        """assetId 实时校验：返回 (severity, msg)。severity: ok / warn / err。
        err = 拒绝提交；warn = 允许但提示（导出将覆盖已有资产）。"""
        if not cid:
            return "ok", "留空 = 按动画名自动生成 assetId"
        if not re.match(r"^[0-9A-Za-z_\-]+$", cid):
            return "err", "✗ 只能字母 / 数字 / 下划线 / 短横线（ASCII）"
        try:
            assets = pack.list_assets()
        except Exception:
            assets = {}
        if cid in assets:
            return "warn", "⚠ 已存在 animations/%s/，导出将覆盖（旧帧自动进 .backup/）" % cid
        row = self.list.currentRow()
        if any(i != row and self.videos[i]["cid"] == cid for i in range(len(self.videos))):
            return "err", "✗ 已被本列表另一段占用"
        return "ok", "✓ 可用：animations/%s/" % cid

    def on_id_text(self):
        """编辑 assetId 时实时反馈校验结果（不提交，提交在 editingFinished）。"""
        cid = self.ed_id.text().strip()
        sev, msg = self._id_status(cid)
        self.lb_id_status.setStyleSheet(
            "color:%s" % {"ok": UI["ok"], "warn": UI["warn"], "err": UI["error"]}[sev])
        self.lb_id_status.setText(msg)

    def apply_manual_id(self):
        row = self.list.currentRow()
        if not (0 <= row < len(self.videos)):
            return
        v = self.videos[row]
        cid = self.ed_id.text().strip()
        sev, msg = self._id_status(cid)
        self.lb_id_status.setStyleSheet(
            "color:%s" % {"ok": UI["ok"], "warn": UI["warn"], "err": UI["error"]}[sev])
        self.lb_id_status.setText(msg)
        if sev == "err":
            self.log("assetId 校验未通过（%s）：保持原值 %s" % (msg, v["cid"]))
            self.ed_id.setText(v["cid"])          # 还原显示
            return
        if not cid:
            v["manual"] = False
            v["cid"] = suggest_cid(v["name"], {x["cid"] for x in self.videos if x is not v})
        else:
            v["cid"] = cid
            v["manual"] = True
            if sev == "warn":
                self.log("assetId %s 已存在，导出将覆盖（旧帧进 .backup/）" % cid)
        self.ed_id.setText(v["cid"])
        self._refresh_item(row)

    def on_select(self, *a):
        row = self.list.currentRow()
        if not (0 <= row < len(self.videos)):
            return
        v = self.videos[row]
        self.ed_id.setText(v["cid"])
        self.ed_name.setText(v["name"])
        fps = self.pack_conf()["fps"]
        try:
            if v["fps"] != fps or not v["frames"]:
                raw = os.path.join(BASE, "..", "cache", "raw", v["cid"])
                if os.path.isdir(raw) and v["fps"] != fps:
                    import glob
                    for f in glob.glob(os.path.join(raw, "*.png")):
                        os.remove(f)
                t0 = time.monotonic()
                v["frames"] = build_frames.extract_frames(v["path"], raw, fps)
                v["fps"] = fps
                src_fps = build_frames.probe_src_fps(v["path"])
                self.log("抽帧：%s → %d 帧（目标 %d fps%s，%.1fs）"
                         % (os.path.basename(v["path"]), len(v["frames"]), fps,
                            ("，源片 %.2gfps → 已重采样" % src_fps) if src_fps and abs(src_fps - fps) > 0.5 else "",
                            time.monotonic() - t0))
            im = Image.open(v["frames"][0])
            self.raw_size = im.size
            # 已有段的帧是成品画布尺寸，不是源分辨率：裁切框只在源视频行可用
            self.src_size = None if v.get("existing") else im.size
            self.lb_info.setText("%s\n%d 帧 @ %d fps · 源片 %s"
                                 % (v["name"], len(v["frames"]), fps, "%d×%d" % im.size))
            self._refresh_item(row)
            self.frame_sld.blockSignals(True)
            self.frame_sld.setRange(0, max(0, len(v["frames"]) - 1))
            if row != self._last_row:
                self.frame_sld.setValue(len(v["frames"]) // 2)
                self._last_row = row
            self.frame_sld.blockSignals(False)
            # 左上「导入视频栏」的可预览：选中行的首帧
            try:
                thumb = Image.open(v["frames"][0]).convert("RGBA")
                thumb.thumbnail((250, 120), Image.BILINEAR)
                self.thumb.setPixmap(pil_to_pixmap(thumb))
                self.thumb.setText("")
            except Exception:
                self.thumb.setText("（首帧不可用）")
                self.thumb.setPixmap(QPixmap())
            self._restore_row_crop(v)
            self._set_crop_controls_enabled(self.src_size is not None)
            self.lb_src.setText(
                ("预览：成品帧（%d 帧，只读）" % len(v["frames"]))
                if v.get("existing") else
                ("预览：源视频抽帧（%d 帧）" % len(v["frames"])))
        except Exception as e:
            msg = "打不开这个视频：\n%s\n\n常见原因：文件被占用/损坏、不是视频、或 ffmpeg 抽不出帧。" % e
            self.log(msg)
            QMessageBox.warning(self, "视频打不开", msg)
            return
        if self.cb_crop.isChecked() and not v.get("existing"):
            self.auto_crop()
        # 每行首次载入自动取样：默认 hue_ref=136° 对本批 99° 绿幕完全无效。
        # 标记必须在「每行」上，旧版放在窗口上 → 同一会话导入第 2 段起再也不自动抠，
        # 于是后来导入的视频整片绿背景。只在参数还是出厂默认时跑，不覆盖手调值。
        # 「已有段」是成品帧，自动取样会去翻源视频甚至弹文件框；重抠时手动点取色即可
        if (not v.get("existing") and not v.get("auto_key_done")
                and self.rows["hue_ref"].value() == HUE_DEFAULT):
            v["auto_key_done"] = True
            self.auto_key(silent=True)
        self.render()

    # ---------- 手动裁切（与自动裁边共用同一个 self.crop） ----------
    def _frame_src_size(self):
        return self.src_size or (0, 0)

    def _fit_bbox_cached(self, crop, params):
        """与 build_frames.build_clip 完全相同：首/中/末三帧各自抠像后合并前景框。

        预览改成「只画抠像结果」之后已不再调用；留着是因为改回成品画布预览时
        必须复用它，否则预览的 scale/offset 会和成品不一致。
        """
        v = self.cur()
        if not v:
            return None
        fr = v["frames"]
        key = (v["cid"], tuple(crop) if crop else None, repr(sorted(params.items())))
        if key in self._fit_cache:
            return self._fit_cache[key]
        l, t = (crop[0], crop[1]) if crop else (0, 0)
        p = params
        # 擦除框是源帧坐标；与 render / build_clip 走同一套换算，杜绝预览与成品错位
        p = dict(params, watermarks=self._wm_for_crop(params, crop))
        boxes = []
        for fp in (fr[0], fr[len(fr) // 2], fr[-1]):
            im = Image.open(fp).convert("RGB")
            if crop:
                im = im.crop(tuple(crop))
            bb = build_frames.alpha_bbox(key_frame(np.asarray(im), **p))
            if bb:
                boxes.append(bb)
        box = None if not boxes else (min(x[0] for x in boxes), min(x[1] for x in boxes),
                                     max(x[2] for x in boxes), max(x[3] for x in boxes))
        if len(self._fit_cache) >= 16:
            self._fit_cache.clear()
        self._fit_cache[key] = box
        return box

    def _clamp_crop(self, crop):
        """夹到源帧范围内，并保证最小 16×16；None 表示全帧不裁。"""
        W, H = self._frame_src_size()
        if not W or not H:
            return None
        if not crop:
            return None
        l, t, r, b = (int(round(x)) for x in crop)
        l = max(0, min(l, W - Canvas.MIN_CROP))
        t = max(0, min(t, H - Canvas.MIN_CROP))
        r = max(l + Canvas.MIN_CROP, min(r, W))
        b = max(t + Canvas.MIN_CROP, min(b, H))
        return (l, t, r, b)

    def _restore_row_crop(self, v):
        """切行时恢复该行的裁切 + 取色框 + 擦除框（都挂在行上，互不串）。"""
        self.crop = None if v.get("existing") else self._clamp_crop(v.get("crop"))
        self._last_valid_crop = self.crop
        self.sample_regions = list(v.get("samples") or [])
        self.erase_rects = [tuple(x) for x in (v.get("erase_rects") or [])]
        self._sync_crop_label()
        self._sync_overlays()

    def _sync_crop_label(self):
        """裁切只靠拖框；数值改成一行只读展示，不再给四个上下左右微调框。"""
        self._sync_marks()

    def _sync_marks(self):
        """工具栏下方那一行：裁切 + 取色框 + 擦除框的状态，挤在一行里显示完。"""
        W, H = self._frame_src_size()
        if self.crop and W:
            crop_txt = "裁切 [%d,%d,%d,%d]" % tuple(self.crop)
        else:
            crop_txt = "裁切 全帧%s" % ("（%d×%d）" % (W, H) if W else "")
        self.lb_marks.setText(
            "%s ｜ 取色框 %d ｜ 擦除框 %d ｜ 拖框即重算 · 右键删框"
            % (crop_txt, len(self.sample_regions), len(self.erase_rects)))

    def _set_crop_controls_enabled(self, on):
        self.cb_crop.setEnabled(bool(on))
        self.b_auto_crop.setEnabled(bool(on))
        self.tools[Canvas.MODE_CROP].setEnabled(bool(on))

    # ---------- 三个鼠标工具：裁切 / 取色框 / 矩形擦除 ----------
    def _sync_overlays(self):
        """把当前行的取色框/擦除框推给 Canvas，并把擦除框接进 params.watermarks。"""
        if not hasattr(self, "canvas"):
            return
        self.canvas.set_overlays([x["rect"] for x in self.sample_regions], self.erase_rects)
        self._sync_marks()
        self.schedule_render()

    def set_tool(self, key):
        """三个工具互斥；切到取色/擦除时不再暗化框外，方便看清角落杂物。"""
        self.tool = key
        for k, b in self.tools.items():
            b.blockSignals(True)
            b.setChecked(k == key)
            b.blockSignals(False)
        self.canvas.set_mode(key)
        self.lb_msg.setText({Canvas.MODE_CROP: "裁切工具：拖框边/角",
                             Canvas.MODE_SAMPLE: "取色工具：在绿幕上拖框加取色框",
                             Canvas.MODE_ERASE: "擦除工具：拖框切除角落杂物"}[key])

    def on_solid_toggled(self, *_a):
        self.solid_bg = bool(self.cb_solid.isChecked())
        self.schedule_render()

    def _current_full_rgb(self):
        """当前预览帧的「完整源帧」RGB（未裁切、0~1），供取色框取样。"""
        v = self.cur()
        if not v:
            return None
        i = min(self.frame_sld.value(), len(v["frames"]) - 1)
        return np.asarray(Image.open(v["frames"][i]).convert("RGB")).astype(np.float32) / 255.0

    def on_sample_added(self, rect):
        rgb = self._current_full_rgb()
        if rgb is None:
            return
        st = region_stats(rgb, rect)
        if st is None:
            self.lb_msg.setText("这个框里没有可用背景像素（近黑/近白/灰），换个位置再取")
            return
        v = self.cur()
        self.sample_regions.append({"rect": tuple(int(x) for x in rect),
                                    "hue": st[0], "sat": st[1],
                                    "frame": self.frame_sld.value()})
        v["samples"] = self.sample_regions
        self.log("取色框 %d：框 %s · 第 %d 帧 · 色相中位 %.1f° · 饱和度中位 %.2f · 有效像素 %d"
                 % (len(self.sample_regions), list(rect), self.frame_sld.value() + 1,
                    float(np.median(st[0])), float(np.median(st[1])), st[0].size))
        self._sync_overlays()
        # 加框即自动对准色相并选最优闸门（不弹高级面板）；下一行就是用户问的「下一步」
        self.apply_sample_range(expand_adv=False)

    def on_erase_added(self, rect):
        v = self.cur()
        if not v:
            return
        self.erase_rects.append(tuple(int(x) for x in rect))
        v["erase_rects"] = list(self.erase_rects)
        self.log("擦除框 %d：源帧坐标 %s（预览与生成都会减去裁切左上偏移）"
                 % (len(self.erase_rects), list(rect)))
        self._sync_overlays()

    def on_overlay_picked(self, kind, idx):
        v = self.cur()
        if not v:
            return
        arr = self.sample_regions if kind == "sample" else self.erase_rects
        if not (0 <= idx < len(arr)):
            return
        arr.pop(idx)
        v["samples" if kind == "sample" else "erase_rects"] = (
            self.sample_regions if kind == "sample" else list(self.erase_rects))
        self._sync_overlays()

    def clear_overlays(self, kind):
        v = self.cur()
        if kind == "sample":
            self.sample_regions = []
            if v:
                v["samples"] = []
        else:
            self.erase_rects = []
            if v:
                v["erase_rects"] = []
        self._sync_overlays()

    def apply_sample_range(self, expand_adv=True):
        """多点取色 → 数据驱动的闸门；候选实跑打分，只回填更优解。

        实测教训：直接把 sat_hi 压到背景低分位，在低饱和绿幕（dsh_idle sat≈0.14）
        上会把角色灰白衣物一起啃掉（主体损失 36%）。所以这里和「多帧取色」一样
        用「背景去除 − 2×主体损失」打分，绝不无条件覆盖手调参数。
        色相中心是取色框最可靠的产出，无条件回填（出厂 136° 对 99° 绿幕完全无效）。

        expand_adv=False（来自「加取色框」自动应用）时不强制展开「更多参数」面板，
        避免每次加框都把高级面板弹开。
        """
        d = derive_range([(x["hue"], x["sat"]) for x in self.sample_regions])
        if not d:
            self.lb_msg.setText("还没有有效取色框；先用「加取色框」工具在绿幕上拖几个框")
            return
        rgb = self._current_full_rgb()
        self.rows["hue_ref"].set_value(float(d["hue_ref"]))
        cur = self.params()
        sats = [x["sat"] for x in self.sample_regions if x["sat"].size]
        s_med = float(np.median(np.concatenate(sats))) if sats else d["_info"]["sat50"]
        cands = [
            ("保留现有闸门", {}),
            ("保守闸门（中位饱和）", {"sat_lo": max(0.02, round(s_med - 0.10, 2)),
                                     "sat_hi": max(0.04, round(s_med - 0.04, 2))}),
            ("完整取色范围", {k: d[k] for k in ("hue_in", "hue_out", "sat_lo", "sat_hi")}),
        ]

        def qual(over):
            p = dict(cur, **over)
            if rgb is None:
                return 0.0
            try:
                return self._key_quality(rgb, p)[0]
            except Exception:
                return -1e9

        base = qual({})
        name, chosen, score = max(((nm, ov, qual(ov)) for nm, ov in cands), key=lambda x: x[2])
        if score < base + 0.02:
            name, chosen, score = "保留现有闸门", {}, base
        for k, v in chosen.items():
            self.rows[k].set_value(float(v))
        i = d["_info"]
        self.log("取色范围（%d 处 / %d 像素）：hue %.1f°（偏差 p60 %.1f° / p90 %.1f°）· "
                 "sat p05 %.2f p10 %.2f p50 %.2f → 采用「%s」· 评分 %.3f→%.3f；"
                 "候选范围 hue_in %.1f hue_out %.1f sat %.2f~%.2f"
                 % (len(self.sample_regions), i["n"], d["hue_ref"], i["hue_dev60"], i["hue_dev90"],
                    i["sat05"], i["sat10"], i["sat50"], name, base, score,
                    d["hue_in"], d["hue_out"], d["sat_lo"], d["sat_hi"]))
        if expand_adv:
            self.adv_btn.setChecked(True)
            self.toggle_adv(True)
        self.lb_msg.setText("已对准色相 %.1f°；闸门选「%s」（评分 %.3f）"
                            % (d["hue_ref"], name, score))
        self.schedule_render()

    def on_crop_dragged(self, crop):
        """拖动中：只更新状态行，不重算抠像（避免每像素一次 key_frame）。"""
        if not self.cur():
            return
        self.crop = tuple(crop)
        self.lb_state.setText("crop=[%d,%d,%d,%d] · 松开鼠标后重算抠像" % tuple(crop))

    def on_crop_committed(self, crop):
        self._commit_crop(crop, "拖动")

    def _commit_crop(self, crop, source):
        v = self.cur()
        if not v:
            return
        # 手动拖框后关掉「自动裁黑边」：否则下次切回这一行 auto_crop() 会把手动值冲掉
        if source == "拖动" and self.cb_crop.isChecked():
            self.cb_crop.blockSignals(True)
            self.cb_crop.setChecked(False)
            self.cb_crop.blockSignals(False)
        crop = self._clamp_crop(crop)
        self.crop = crop
        self._last_valid_crop = crop
        v["crop"] = list(crop) if crop else None
        self._sync_crop_label()
        if crop:
            self.lb_msg.setText("裁切已更新：crop=[%d,%d,%d,%d]（%s）" % (tuple(crop) + (source,)))
        else:
            self.lb_msg.setText("裁切已关闭：使用全帧（%s）" % source)
        self.schedule_render()

    def rerun_auto_crop(self):
        v = self.cur()
        if not v or not self.src_size:
            self.lb_msg.setText("已有段没有源分辨率，无法自动裁边；请从源视频重新导入")
            return
        if not self.cb_crop.isChecked():
            self.cb_crop.blockSignals(True)
            self.cb_crop.setChecked(True)
            self.cb_crop.blockSignals(False)
        self.auto_crop()

    # ---------- 自动裁黑边 ----------
    def auto_crop(self):
        v = self.cur()
        if not v or not self.src_size:
            return
        probe = os.path.join(BASE, "..", "cache", "probe", v["cid"])
        try:
            files = build_frames.extract_frames(v["path"], probe, 1)
        except Exception as e:
            self.log("裁边失败（抽 1fps 探针帧出错）：%s" % e)
            return
        crop, detail = detect_crop(files)
        self.crop = self._clamp_crop(crop) if crop else None
        v["crop"] = list(self.crop) if self.crop else None
        self._last_valid_crop = self.crop
        self._sync_crop_label()
        if self.crop:
            self.log("自动裁边：%s → crop=%s（已填入四个数值框，可继续手动微调）" % (detail, list(self.crop)))
        else:
            self.log("未发现明显暗边（%s），不裁" % detail)
        self.render()

    def on_crop_toggled(self, on):
        if on:
            self.auto_crop()
        else:
            self.crop = None
            self._last_valid_crop = None
            v = self.cur()
            if v:
                v["crop"] = None
            self._sync_crop_label()
            self.render()


    # ---------- 预览 ----------
    def cur(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.videos) and self.videos[row]["frames"]:
            return self.videos[row]
        return None

    def on_frame(self, i):
        v = self.cur()
        if v:
            i = min(i, len(v["frames"]) - 1)
            self.lb_frame.setText("%d / %d" % (i + 1, len(v["frames"])))
            self.schedule_render()

    # ---------- 播放 / 暂停 ----------
    def toggle_play(self, *_a):
        if self.play_timer.isActive():
            self.pause_play()
        else:
            self.start_play()

    def start_play(self):
        v = self.cur()
        if not v or len(v["frames"]) < 2:
            self.lb_perf.setText("至少需要 2 帧")
            return
        fps = max(1.0, float(v.get("fps") or 12.0))
        self.play_timer.start(max(16, int(1000.0 / fps)))
        self.b_play.setChecked(True)

    def pause_play(self, *_a):
        if self.play_timer.isActive():
            self.play_timer.stop()
        self.b_play.setChecked(False)
        self.lb_perf.setText("已暂停")

    def _play_tick(self):
        v = self.cur()
        if not v or len(v["frames"]) < 2:
            self.pause_play()
            return
        n = len(v["frames"])
        i = self.frame_sld.value() + 1
        if i >= n:                      # 到头回卷，预览是循环的，和桌面行为一致
            i = 0
        self.frame_sld.blockSignals(True)
        self.frame_sld.setValue(i)
        self.frame_sld.blockSignals(False)
        # 每 tick 直接渲染，不排队 100ms 去抖 —— 去抖会把播放节流成 10fps 以下
        self.render()

    def keyPressEvent(self, ev):
        # 空格 = 播放/暂停；左侧工具区有按钮，帧滑块自己处理左右方向键
        if ev.key() == Qt.Key.Key_Space and not self._edit_has_focus():
            self.toggle_play()
            return
        super().keyPressEvent(ev)

    def _edit_has_focus(self):
        """输入类控件拿到焦点时，空格归它自己（否则在输入框里打字会触发播放）。"""
        w = QApplication.focusWidget()
        return isinstance(w, (QLineEdit, QPlainTextEdit, QTextEdit,
                              QSpinBox, QDoubleSpinBox, QComboBox))

    def schedule_render(self, *a):
        self.render_timer.start(100)

    def params(self):
        p = default_params()
        p.update({k: self.rows[k].value() for k in self.rows})
        p["keep_frac"] = FIXED["keep_frac"] if self.cb_keep.isChecked() else 1.0
        # 擦除框一律存「源帧坐标」；喂给 key_frame 之前由 _wm_for_crop 统一减裁切偏移。
        p["watermarks"] = [list(r) for r in self.erase_rects]
        return p

    def _wm_for_crop(self, params, crop):
        """源帧坐标的擦除框 → 裁切后坐标（key_frame 的输入是已裁切的帧）。

        render / 主体框预览 / build_frames.build_clip 三处必须都调这个，
        否则「预览切掉了、生成没切掉」这类错位就会回来。
        """
        l, t = (int(crop[0]), int(crop[1])) if crop else (0, 0)
        out = []
        for (x0, y0, x1, y1) in params.get("watermarks") or []:
            a, b_, c_, d = max(0, x0 - l), max(0, y0 - t), max(0, x1 - l), max(0, y1 - t)
            if c_ > a and d > b_:
                out.append([a, b_, c_, d])
        return out

    def reset_adv(self):
        for key, _n, _lo, _hi, val, _t in ADV_DEFS:
            self.rows[key].set_value(val)
        self.cb_keep.setChecked(True)
        self.log("参数已恢复默认")
        self.schedule_render()

    def _fit_view(self, edit, lim=460):
        s = min(1.0, lim / max(edit.size))
        if s < 1.0:
            edit = edit.resize((max(1, round(edit.width * s)), max(1, round(edit.height * s))),
                               Image.BILINEAR)
        return pil_to_pixmap(edit)

    def render(self):
        """一次 key_frame，同时产出「抠图前 / 抠图后 / alpha」三张并列视图。

        三张图共用同一块裁切后的抠像结果，所以参数一改三张一起变；
        中间的画布承载裁切/取色/擦除三种鼠标工具，另两张只做对照。
        """
        v = self.cur()
        if not v:
            return
        i = min(self.frame_sld.value(), len(v["frames"]) - 1)
        t0 = time.monotonic()
        full = Image.open(v["frames"][i]).convert("RGB")
        W, H = full.size
        crop = self._clamp_crop(self.crop) if (self.src_size and self.crop) else None
        l, t, r, b = crop if crop else (0, 0, W, H)
        raw_c = full.crop((l, t, r, b))          # 先裁边再抠像：与 build_frames 生成时一致
        p = self.params()
        p_crop = dict(p, watermarks=self._wm_for_crop(p, crop))
        rgba_c = key_frame(np.asarray(raw_c), **p_crop)
        arr = np.asarray(rgba_c)
        alpha = arr[:, :, 3]
        bg = float((alpha < 8).mean())

        # 抠图前：完整源帧；抠图后：裁切区换成抠像结果（可铺实色底看绿边）；alpha：灰度通道
        edit_raw = full.convert("RGBA").copy()
        rgba_shown = rgba_c
        if self.solid_bg:
            rgba_shown = Image.alpha_composite(
                Image.new("RGBA", raw_c.size, (46, 52, 64, 255)), rgba_c)
        edit_key = full.convert("RGBA").copy()
        edit_key.paste(rgba_shown, (l, t))
        edit_alpha = full.convert("RGBA").copy()
        edit_alpha.paste(Image.fromarray(alpha, "L").convert("RGBA"), (l, t))
        edit_ok = bool(self.src_size)
        self.canvas_raw.set_image(self._fit_view(edit_raw), (W, H), None, False)
        self.canvas_alpha.set_image(self._fit_view(edit_alpha), (W, H), None, False)
        self.canvas.set_image(self._fit_view(edit_key), (W, H), crop, edit_ok)

        dt = (time.monotonic() - t0) * 1000
        self.lb_perf.setText("预览 %.0f ms" % dt)
        self.lb_frame.setText("%d / %d" % (i + 1, len(v["frames"])))
        # 状态行：只给用户能看懂的结论，不堆技术参数（hue/sat/floor 留给「更多参数」）
        if bg >= 0.95:
            status = "✓ 背景已抠净"
        elif bg >= 0.25:
            status = "⚠ 背景未抠净"
        else:
            status = "⚠ 背景几乎没抠掉"
        erase_note = (" · 已擦除 %d 处" % len(self.erase_rects)) if self.erase_rects else ""
        self.lb_state.setText("%s（透明 %.0f%%）· 第 %d/%d 帧%s"
                              % (status, bg * 100, i + 1, len(v["frames"]), erase_note))


    def _sample_paths(self, v):
        """返回 (帧路径列表, 来源说明, 是否源帧坐标系)。

        重抠「已有段」时 v["frames"] 指向成品帧（despill 后绿幕 99°→73°），
        必须优先用 build/cache/raw/<cid>/ 的源抽帧。旧版只取「中间一帧」，
        而 10 段素材实测有 2 段四角取样直接返回 (0,0)、另有 2 段色相离群，
        所以这里把完整路径列表交给调用方做多帧稳健取样。
        crop 是源帧坐标系，只有 from_raw=True 才套用（成品帧尺寸不同会错位）。
        """
        import glob
        raw = os.path.join(CACHE_DIR, "raw", v["cid"])
        fs = sorted(glob.glob(os.path.join(raw, "r*.png")))
        used, from_raw = "源视频抽帧", True
        if not fs and not v.get("existing"):
            fs, used, from_raw = list(v["frames"]), "预览帧（新导入=源帧）", True
        if not fs:
            src = self.resolve_source_video(v, v["cid"], self.cur_pack())
            if src:
                try:
                    fs = build_frames.extract_frames(src, raw, self.pack_conf()["fps"])
                    used, from_raw = "源视频抽帧", True
                except Exception as e:              # noqa: BLE001 - 取样失败不该炸 UI
                    self.log("源帧抽取失败：%r" % (e,))
        if not fs:
            fs = list(v["frames"])
            used, from_raw = "成品帧（源帧不可用，色相可能偏差）", False
        return fs, used, from_raw

    def _rgb_of(self, path, from_raw=True):
        """读一帧 + 按 crop 裁切 → 0~1 浮点 RGB。"""
        im = Image.open(path).convert("RGB")
        if from_raw and self.crop:
            l, t, r, b = (int(round(x)) for x in self.crop)
            if 0 <= l < r <= im.width and 0 <= t < b <= im.height:
                im = im.crop((l, t, r, b))
        return np.asarray(im).astype(np.float32) / 255.0

    def _sample_frame_rgb(self, v, index=None):
        """兼容旧调用：取「当前预览帧」对应的取样帧，不再是写死的中间帧。"""
        paths, used, from_raw = self._sample_paths(v)
        if not paths:
            raise ValueError("没有可取样帧")
        i = self.frame_sld.value() if index is None else index
        p = paths[min(max(0, i), len(paths) - 1)]
        return self._rgb_of(p, from_raw), p, used

    def _key_quality(self, rgb, params):
        """返回 (评分, 背景环去除率, 主体损失率)；评分 = 背景去除 − 2×主体损失。

        旧一键抠净拿 (alpha<8).mean() 当目标：角色占满画面时永远够不到 25%，
        于是把 hue_out 放宽到 70°、floor 降到 0.04 —— 透明率没涨，边缘先糊了。
        这里同时盯住「背景有没有去掉」和「角色有没有被啃」，绝不自动改 hue_out/floor。
        """
        p = dict(params, watermarks=[])
        a = np.asarray(key_frame((np.clip(rgb, 0, 1) * 255).astype(np.uint8), **p))[..., 3]
        h, w = a.shape
        k = max(8, min(h, w) // 12)
        ring = np.ones((h, w), bool)
        ring[k:-k, k:-k] = False
        bg = float((a[ring] < 8).mean())
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        mx, mn = rgb.max(2), rgb.min(2)
        sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
        d = mx - mn + 1e-6
        hue = np.where(mx == r, ((g - b) / d) % 6,
                       np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
        dh = np.abs(((hue - params["hue_ref"] + 180) % 360) - 180)
        subject = ((dh > 60) & (sat > 0.06)) | ((sat < 0.22) & (mx > 0.40))
        loss = float(1.0 - (a[subject] > 200).mean()) if subject.any() else 0.0
        return bg - 2.0 * loss, bg, loss

    def _key_transparency(self, rgb, params):
        """用当前参数实跑一次 key_frame，返回「完全透明像素」占比。"""
        p = dict(params, watermarks=[])
        a = np.asarray(key_frame((np.clip(rgb, 0, 1) * 255).astype(np.uint8), **p))[..., 3]
        return float((a < 8).mean())

    def auto_key(self, silent=False):
        """多帧取色（旧名「一键抠净」）：多帧四角取样取稳健中位数 → 候选闸门实跑打分 → 只回填更优解。

        与旧版区别：取样不再写死中间帧、不再无条件覆盖用户手调的闸门、
        不再靠放宽 hue_out / 压低 floor 去追「全图透明率」这个会误伤主体的指标。
        """
        v = self.cur()
        if not v:
            if not silent:
                self.log("先选一个视频")
            return False
        try:
            paths, used, from_raw = self._sample_paths(v)
            if not paths:
                raise ValueError("没有可取样帧")
            n = len(paths)
            idxs = sorted({0, n - 1, n // 2} | {int(round((n - 1) * k / 8)) for k in range(9)})
            samples = []
            for i in idxs:
                h, s = sample_hue_from(self._rgb_of(paths[i], from_raw))
                if s >= 0.10:                  # 四角被角色/黑边占住的帧直接丢掉
                    samples.append((h, s))
        except Exception as e:                  # noqa: BLE001
            self.log("取样失败：%r" % (e,))
            return False
        if not samples:
            self.log("⚠ 多帧四角都没有可用背景像素；拖帧滑块挑一帧，再用「当前帧取色」")
            return False
        hs = np.array([x[0] for x in samples])
        ss = np.array([x[1] for x in samples])
        h0 = float(np.median(hs))
        good = np.abs(((hs - h0 + 180) % 360) - 180) <= 25   # 圆形色相离群剔除
        href = float(np.median(hs[good])) if good.any() else h0
        sat_med = float(np.median(ss[good])) if good.any() else float(np.median(ss))
        eval_rgb = self._rgb_of(paths[n // 2], from_raw)
        cur = self.params()
        cands = [
            ("当前闸门", dict(cur, hue_ref=href)),
            ("出厂闸门", dict(default_params(), hue_ref=href)),
            ("实测闸门", dict(cur, hue_ref=href,
                             sat_lo=max(0.02, round(sat_med - 0.10, 2)),
                             sat_hi=max(0.04, round(sat_med - 0.04, 2)))),
        ]
        base = self._key_quality(eval_rgb, cur)[0]
        name, best, score = max(
            ((nm, p, self._key_quality(eval_rgb, p)[0]) for nm, p in cands),
            key=lambda x: x[2])
        if score < base + 0.02:
            self.log("多帧取色：当前参数评分 %.3f 已不劣于自动候选 %.3f，未改动"
                     "（只想改色相请用「当前帧取色」）" % (base, score))
            return True
        self.rows["hue_ref"].set_value(href)
        self.rows["sat_lo"].set_value(best["sat_lo"])
        self.rows["sat_hi"].set_value(best["sat_hi"])
        _sc, bg, loss = self._key_quality(eval_rgb, self.params())
        if not silent:
            self.adv_btn.setChecked(True)
            self.toggle_adv(True)
        self.log("多帧取色（%d/%d 帧·%s）：hue %.1f° sat %.3f → %s %.2f~%.2f；"
                 "评分 %.3f→%.3f（背景去除 %.0f%% · 主体损失 %.1f%%）"
                 % (len(samples), len(idxs), used, href, sat_med, name,
                    best["sat_lo"], best["sat_hi"], base, score, bg * 100, loss * 100))
        if bg < 0.25:
            self.log("⚠ 背景去除率偏低：这段可能不是纯色绿幕，切 alpha 视图手动微调")
        self.schedule_render()
        return bg >= 0.25

    def sample_hue(self):
        """当前帧取色（旧名「四点取色」）：对「当前预览帧」四角取样——旧版那个更好用的按钮，原样恢复。

        用户可以用帧滑块挑一帧四角干净绿幕的；只在背景饱和度低于下限时才压闸门，
        已经手调过的 sat 参数不会被无条件覆盖。
        """
        v = self.cur()
        if not v:
            self.log("先选一个视频")
            return
        try:
            rgb, path, used = self._sample_frame_rgb(v)
        except Exception as e:                  # noqa: BLE001
            self.log("取样失败：%r" % (e,))
            return
        href, sat_med = sample_hue_from(rgb)
        if sat_med <= 0.01:
            self.log("⚠ 这一帧四角没有可用背景像素，拖帧滑块换一帧再点")
            return
        self.rows["hue_ref"].set_value(href)
        lo_row, hi_row = self.rows["sat_lo"], self.rows["sat_hi"]
        if sat_med < lo_row.value():
            old = lo_row.value()
            lo = max(0.02, round(sat_med - 0.10, 2))
            hi = max(lo + 0.02, round(sat_med - 0.05, 2))
            lo_row.set_value(lo)
            hi_row.set_value(hi)
            self.log("背景饱和度 %.2f 低于下限 %.2f：闸门已改为 %.2f~%.2f"
                     % (sat_med, old, lo, hi))
        self.adv_btn.setChecked(True)
        self.toggle_adv(True)
        self.log("当前帧取色（%s · %s）：色相 %.1f° 饱和度 %.2f；闸门 %.2f~%.2f"
                 % (used, os.path.basename(path), href, sat_med,
                    lo_row.value(), hi_row.value()))
        if sat_med < 0.2:
            self.log("⚠ 四角不够绿：可能不是纯绿幕，切 alpha 视图看边框是否干净")
        self.schedule_render()

    # ---------- 预设 ----------
    def refresh_presets(self):
        os.makedirs(PRESET_DIR, exist_ok=True)
        names = sorted(os.path.splitext(f)[0] for f in os.listdir(PRESET_DIR)
                       if f.endswith(".json"))
        self.cb_preset.blockSignals(True)
        self.cb_preset.clear()
        self.cb_preset.addItems(names)
        self.cb_preset.setCurrentIndex(-1)
        self.cb_preset.setPlaceholderText("（未选）")
        self.cb_preset.blockSignals(False)

    def save_preset(self):
        name, ok = QInputDialog.getText(self, "存为预设", "预设名（存成 presets\\<名>.json）")
        if not ok or not name.strip():
            return
        name = re.sub(r'[\\/:*?"<>|]', "_", name.strip())
        data = {"params": {k: self.rows[k].value() for k in self.rows},
                "keep_largest": bool(self.cb_keep.isChecked())}
        os.makedirs(PRESET_DIR, exist_ok=True)
        with open(os.path.join(PRESET_DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.refresh_presets()
        self.log("已保存预设 presets/%s.json" % name)

    def load_preset(self, *a):
        name = self.cb_preset.currentText()
        path = os.path.join(PRESET_DIR, name + ".json")
        if not name or not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        for k, v in d.get("params", {}).items():
            if k in self.rows:
                self.rows[k].set_value(float(v))
        self.cb_keep.setChecked(bool(d.get("keep_largest", True)))
        self.log("载入预设：%s" % name)
        self.schedule_render()

    # ---------- 生成 ----------
    def _clip_entry(self, pack_id, cid):
        """v2：一段动画的元数据就在它自己的 meta.json 里。"""
        return pack.load_asset(cid) or {}

    def resolve_source_video(self, v, cid, pack_id):
        """重新生成必须喂源视频。已有段行的 path 是 frames 目录，不能直接给 ffmpeg。

        顺序：manifest 的 sourcePath/video/source → 素材目录按文件名 → 显示名/clipId →
        文件选择框。返回 "" 表示用户取消或找不到。
        """
        if is_video_file(v.get("path")) and not v.get("existing"):
            return v["path"]
        entry = self._clip_entry(pack_id, cid)
        for key in ("sourcePath", "video", "source"):
            cand = entry.get(key) or v.get(key)
            if not cand:
                continue
            cand = str(cand)
            if is_video_file(cand):
                return cand
            extra = (os.path.dirname(cand),) if os.path.isabs(cand) else ()
            p = find_source_by_name(os.path.basename(cand), extra)
            if p:
                return p
        for nm in (entry.get("name"), entry.get("displayName"), v.get("name"), cid):
            p = find_source_by_name(nm or "")
            if p:
                return p
        start = os.path.join(REPO, "material", "DSMaid")
        p, _ = QFileDialog.getOpenFileName(self, "选择该段的源视频：%s" % (v.get("name") or cid),
                                           start, "视频 (*.mp4 *.mov *.webm *.mkv *.avi)")
        return p if is_video_file(p) else ""

    def remember_source(self, pack_id, cid, src):
        """选定源路径写回该段 meta.json：source 是文件名，sourcePath 记仓库相对路径。"""
        try:
            meta = pack.load_asset(cid) or {"assetId": cid, "name": cid}
            meta.update(source=os.path.basename(src), sourcePath=rel_repo(src))
            pack.write_asset(cid, meta)
            self.log("源视频已写回 meta.json：%s → %s" % (cid, rel_repo(src)))
        except Exception as e:
            self.log("写回 meta.json 失败（不影响本次生成）：%r" % (e,))

    def collect_queue(self):
        """当前选中的视频（没选就当前行），一次排进队列。"""
        rows = sorted({i.row() for i in self.list.selectedIndexes()})
        if not rows:
            rows = [self.list.currentRow()]
        valid = []
        for r in rows:
            if 0 <= r < len(self.videos) and self.videos[r]["frames"]:
                valid.append(self.videos[r])
        return valid

    def _preflight_key(self, items):
        """生成前用真实源帧实测透明率，避免静默产出整片绿背景。"""
        p = self.params()
        rates = []
        for v in items[:3]:
            try:
                rgb, _, _ = self._sample_frame_rgb(v)
                rates.append(self._key_transparency(rgb, p))
            except Exception as e:              # noqa: BLE001
                self.log("预检取样失败（跳过）：%r" % (e,))
        if not rates:
            return True
        avg = sum(rates) / len(rates)
        self.log("生成前预检：抠像透明率 %.1f%%（%d 帧抽样）" % (avg * 100, len(rates)))
        if avg < 0.15:
            r = QMessageBox.warning(
                self, "背景可能没抠净",
                "当前参数下背景透明率只有 %.1f%%，生成后会留下整片绿背景。\n\n"
                "建议先点「当前帧取色」或「多帧取色」再生成。仍要继续吗？" % (avg * 100),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            return r == QMessageBox.StandardButton.Yes
        return True

    def generate(self):
        if self.gen and self.gen.isRunning():
            return
        items = self.collect_queue()
        if not items:
            self.log("先导入并选中至少一个视频")
            return
        if not self._preflight_key(items):
            self.log("已取消生成：先修抠像参数")
            return
        self.save_row_meta()
        pack_id = self.cur_pack()
        if not pack_id:
            self.log("先选择一个目标 pack（或点「新建…」）")
            return
        self.queue = []
        for v in items:
            cid = v["cid"]
            self.queue.append((v, cid, v["name"]))
        c = self.pack_conf()
        self.log("开始生成：pack=%s，%d 段，画布 %d×%d，%d fps，%s q%d（pack 级固定值）"
                 % (pack_id, len(self.queue), c["w"], c["h"], c["fps"], c["fmt"], c["quality"]))
        self.prog.setValue(0)
        self.b_gen.setEnabled(False)
        self.b_cancel.setEnabled(True)
        self._run_next()

    def _run_next(self):
        if not self.queue:
            self.b_gen.setEnabled(True)
            self.b_cancel.setEnabled(False)
            self.log("全部完成")
            return
        v, cid, name = self.queue[0]
        pack_id = self.cur_pack()
        c = self.pack_conf()
        src = self.resolve_source_video(v, cid, pack_id)
        if not src:
            self.log("已跳过 %s：没有可用的源视频（可在 manifest 的 source/sourcePath 补路径，或下次弹框时指定）" % cid)
            self.queue.pop(0)
            self._run_next()
            return
        try:
            build_frames.validate_source(src)
        except Exception as e:
            self.log("已跳过 %s：%s" % (cid, e))
            QMessageBox.warning(self, "无法重新生成 %s" % cid, str(e))
            self.queue.pop(0)
            self._run_next()
            return
        v["resolved_source"] = src
        self.remember_source(pack_id, cid, src)
        # 重抠同一段：旧帧备份到 .backup/，不产生 _v2 目录
        bak = pack.backup_asset_frames(cid)
        if bak:
            self.log("同 clipId 重抠：旧帧已备份到 %s（可回滚）" % os.path.relpath(bak, REPO))
        self.log("重新生成 %s ← 源视频 %s" % (cid, src))
        spec = {"id": cid, "video": src, "params": self.params(),
                "fps": c["fps"], "width": c["w"], "height": c["h"],
                "fmt": c["fmt"], "quality": c["quality"],
                # 每行自己的裁切（自动结果或手动微调后的值），不能拿当前选中行的覆盖别的行
                "crop": list(v.get("crop")) if v.get("crop") else None,
                "out_dir": pack.asset_dir(cid)}
        os.makedirs(spec["out_dir"], exist_ok=True)
        self.gen = GenThread(spec)
        self.gen.msg.connect(self.on_gen_msg)
        self.gen.done.connect(self.on_gen_done)
        self.gen.failed.connect(self.on_gen_fail)
        self.gen.start()

    def on_gen_msg(self, s):
        self.log(s)
        m = re.search(r"(\d+)/(\d+)$", s.strip())
        if m:
            self.prog.setValue(int(int(m.group(1)) * 100 / max(1, int(m.group(2)))))

    def on_gen_done(self, r):
        """生成完：写 animations/<assetId>/meta.json，再回写主题索引（默认勾来路状态）。

        V3 裁决：从设置的空状态进工坊，生成完默认勾对应状态；直接打开工坊（无来路）
        则只入库、不动绑定，由用户在设置里从动作库自行挑选。
        """
        v, cid, name = self.queue.pop(0)
        theme_id = self.cur_pack()
        self.last_out_dir = r.get("out_dir") or ""   # 「打开当前动画目录」直接落到这里
        h = r["height"]
        src = v.get("resolved_source") or v["path"]
        meta = {
            "assetId": cid, "name": name or cid,
            "canvas": {"w": int(r["width"]), "h": int(h)},
            "fps": r["fps"], "count": r["count"], "format": r["format"],
            "loop": False, "type": "action", "priority": 20,
            "scale": r["scale"], "offset": r["offset"],
            "anchor": {"foot": [0.5, round(r["foot"][1] / h, 4)]},
            "tags": [], "crop": r["crop"], "source": os.path.basename(src),
            # 手动矩形擦除以「源帧坐标」入库，下次「在工坊中打开这段」可继续编辑
            "eraseRects": [list(x) for x in self.erase_rects],
            "origin": {"theme": theme_id, "generated": True},
        }
        if is_video_file(src):
            meta["sourcePath"] = rel_repo(src)
        old = pack.load_asset(cid) or {}
        for k in ("license", "legacyDir", "displayName"):
            if k in old and k not in meta:
                meta[k] = old[k]
        # 记录本次真正用到的抠像参数。旧实现继承上一次的字符串字典，回溯时全是误导。
        meta["keyer"] = jsonable_params(r.get("params") or self.params())
        pack.write_asset(cid, meta)
        if self.origin_state and self.origin_state in pack.STATE_KEYS:
            pack.theme_add_clip(theme_id, self.origin_state, cid)
            self.log("已绑定到主题 %s 的状态「%s」" %
                     (theme_id, pack.STATE_CN_V2.get(self.origin_state, self.origin_state)))
        else:
            self.log("已入库（未指定目标状态，未自动绑定）；可在设置里从动作库挑选")
        q = self.qc(r["out_dir"], self.rows["hue_ref"].value())
        self.prog.setValue(100)
        warn = ""
        if q.get("opaque", 0) > 0.70:
            warn = "  ⚠ 不透明占比 %.0f%%：背景可能没抠净，点「多帧取色」后重生成" % (q["opaque"] * 100)
            self.log(warn.strip())
        self.lb_result.setText(
            "已生成 %d 帧 · %.2f MB · 脚底锚点 y=%d · 残留绿边 %.2f%% · 不透明占比 %.0f%% · "
            "scale %.3f offset %s%s"
            % (r["count"], r["bytes"] / 1e6, r["foot"][1], q["resid"],
               q.get("opaque", 0) * 100, r["scale"], r["offset"], warn))
        self.log("%s 完成：%d 帧，%.2f MB，%d×%d；animations/%s/meta.json 已写入"
                 % (cid, r["count"], r["bytes"] / 1e6, r["width"], h, cid))
        self._run_next()

    def qc(self, out_dir, hue):
        import glob
        fs = sorted(glob.glob(os.path.join(out_dir, "f*.webp"))) or \
             sorted(glob.glob(os.path.join(out_dir, "f*.png")))
        if not fs:
            return {"resid": 0.0, "opaque": 0.0}
        step = max(1, len(fs) // 8)
        vals = []
        opaque = []
        for p in fs[::step]:
            im = Image.open(p).convert("RGBA")
            a = np.asarray(im)[..., 3]
            opaque.append(float((a > 200).mean()))
            hsv = np.asarray(im.convert("HSV"))
            hh = hsv[..., 0].astype(np.int32) * 360 // 255
            ss = hsv[..., 1].astype(np.float32) / 255
            fg = a > 40
            if fg.any():
                dh = np.abs(((hh - hue + 180) % 360) - 180)
                vals.append(float(((dh < 30) & (ss > 0.3) & fg).sum()) / fg.sum() * 100)
        return {"resid": float(np.median(vals)) if vals else 0.0,
                "opaque": float(np.median(opaque)) if opaque else 0.0}

    def on_gen_fail(self, s):
        self.log("生成失败：\n" + s)
        self.queue = []
        self.b_gen.setEnabled(True)
        self.b_cancel.setEnabled(False)

    def cancel(self):
        self.queue = []
        self.log("已取消：当前这一段会写完再停（中间帧不能中断），后续排队已清空")
        self.b_cancel.setEnabled(False)

    def _out_target_dir(self):
        """定位到「这段动画自己的目录」，而不是 animations/ 上一级。

        优先级：最近一次生成结果 > 当前选中行的 assetId 目录 > 全局动画库。
        """
        if self.last_out_dir and os.path.isdir(self.last_out_dir):
            return self.last_out_dir, "最近生成"
        v = self.cur()
        if v:
            d = pack.asset_dir(v["cid"])
            if os.path.isdir(d):
                return d, "当前选中段"
        return pack.anim_dir(), "动画库（上一级）"

    def open_out(self):
        """打开当前动画目录：不存在则先创建；打不开就复制路径，不抛异常。

        历史 bug：os.startfile() 在部分环境（受限/ACL/中文路径）抛 PermissionError
        [WinError 5]，异常从 Qt 槽里冒出去会让 PyQt 直接 abort（0xC0000409），
        整窗闪退。这里把解析、创建、打开全部包住，并用剪贴板留退路。
        """
        d = ""
        try:
            d, why = self._out_target_dir()
            if not d:
                raise ValueError("输出目录为空（当前 pack 无效）")
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
                self.log("输出目录不存在，已创建：%s" % d)
            try:
                os.startfile(d)
            except OSError:
                logging.exception("os.startfile 打开输出目录失败: %s", d)
                self.open_out_fallback(d, "系统拒绝打开该目录")
                return
            self.log("已打开%s：%s" % (why, d))
            self.lb_msg.setText("已打开 %s · %s" % (why, os.path.basename(d)))
        except Exception:
            logging.exception("open_out 处理输出目录失败: %s", d)
            self.open_out_fallback(d, "路径处理失败，详见 build/cache/workshop.log")

    def open_out_fallback(self, path, why):
        """自动打开失败时的退路：复制路径到剪贴板 + 状态栏提示，绝不再让槽抛异常。"""
        QApplication.clipboard().setText(path or "")
        msg = "无法自动打开（%s）。路径已复制到剪贴板，请手动粘贴到资源管理器打开。" % why
        if path:
            msg += "\n" + path
        self.log(msg)
        self.lb_msg.setText("已复制输出目录路径，请手动打开")

    # ---------- 日志 ----------
    def log(self, s):
        self.log_box.appendPlainText(s)
        self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())


def main():
    ap = argparse.ArgumentParser(description="智灵素材工坊")
    ap.add_argument("--clip", default="", help="启动后定位到已有成品段 assetId（从设置面板跳转）")
    ap.add_argument("--state", default="", help="从设置带过来的目标状态：生成完自动绑定到该状态")
    ap.add_argument("--pack", default="", help="目标主题 id（沿用旧参数名，值是 packs/themes/<id>.json）")
    ap.add_argument("--new-theme", action="store_true", help="启动后直接打开「新建主题」对话框（设置页入口）")
    args, _unknown = ap.parse_known_args()
    if not args.pack and not pack.has_usable_material():
        _ok, _msg = pack.install_sample_pack()   # 与桌宠同一套首启兜底
        print("[workshop] first_run: %s" % _msg)
    app = QApplication(sys.argv)
    _ff = pack.ffmpeg_resolution()
    if not _ff["path"]:
        # 缺 ffmpeg 时明确提示 + 下载指引（pythonw 下也有窗口，不静默失败）
        logging.warning("ffmpeg 未找到：抽帧/生成不可用")
        QMessageBox.warning(None, "抽帧不可用", pack.FFMPEG_MISSING_HINT)
    w = Workshop(origin_state=args.state, theme_id=args.pack)
    w.show()
    w.refresh_presets()
    if args.clip:
        w.focus_clip(args.clip)
    if args.new_theme:
        w.new_pack()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        with open(os.path.join(BASE, "..", "cache", "tmp", "workshop.log"), "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        raise
