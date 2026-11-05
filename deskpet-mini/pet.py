"""最小桌宠：透明窗口循环播放待机段，双击播一次动作段，可拖动、可换档、可退出。

用法:
    python pet.py                 # 默认中档（屏幕宽 15%）
    python pet.py --width 400     # 指定宠物宽度(像素)
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import deque

from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QImage, QPainter, QAction, QActionGroup, QRegion
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

T0 = time.monotonic()
ROOT = os.path.dirname(os.path.abspath(__file__))

TIERS = {"小": 0.10, "中": 0.15, "大": 0.30}   # 占可用屏幕宽度的比例（中=原来默认值的一半）
DEFAULT_TIER = "中"
PREFETCH = 8          # 向前预取帧数
KEEP_NEAR = 14        # 缓存里保留当前帧前后多少帧
FADE_MS = 250         # 两段之间的交叉淡化时长

# ---- 点击穿透 ----------------------------------------------------------
# 用窗口级 setMask(QRegion)：把窗口裁剪成宠物轮廓（Qt 内部走 SetWindowRgn）。
# 透明区不再属于这个窗口，点击由系统直接派给下层窗口；本体照常接拖动/双击。
# 掩码来自当前帧 alpha 的 1/MASK_DIV 降采样，按行合并成矩形，结果按帧缓存。
# 注：Qt6 在 QWindowsWindow 里就把 WM_NCHITTEST 处理掉了，QWidget.nativeEvent
# 收不到这条消息（实测 1.5s 内 0 次），所以"子类化 nativeEvent"那条路走不通，
# 真要用得 SetWindowLongPtr 换窗口过程——setMask 已经够用，不引入原生回调风险。
MASK_DIV = 2            # 掩码降采样倍数（2 => 命中边界精度约 ±2px）
MASK_ALPHA_MIN = 8      # alpha >= 8 视为宠物本体（alpha<8 在桌面上肉眼不可见）
MASK_REFRESH_MS = 150   # 掩码最短更新间隔，避免每帧重建 QRegion

META = None
CLIPS = {}
EXTS = {}          # 每段实际使用的帧后缀："webp" 或 "png"
ACTIONS = []       # 动作 clip 的 id，按优先级排序
IDLE_ID = "idle"
LOG_PATH = None


def load_clips():
    """clips.json 管顺序/类型/名字；meta.json 管目录、帧数、fps、格式。缺 clips.json 时退回旧行为。"""
    global IDLE_ID
    meta_clips = META.get("clips", {})
    path = os.path.join(ROOT, "clips.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            arr = json.load(f).get("clips", [])
    else:
        arr = [{"id": k, "type": "idle" if k == "idle" else "action",
                "loop": k == "idle", "name": k} for k in meta_clips]
    out = {}
    for e in arr:
        cid = e.get("id")
        if not cid:
            continue
        m = meta_clips.get(cid, {})
        out[cid] = {"dir": m.get("dir", e.get("dir", "frames_" + cid)),
                    "fps": m.get("fps", 24), "count": m.get("count", 1),
                    "format": m.get("format"), "name": e.get("name", cid),
                    "type": e.get("type", "action"), "loop": bool(e.get("loop", False)),
                    "priority": e.get("priority", 99)}
    idd = [c for c in out if out[c]["type"] == "idle"]
    IDLE_ID = idd[0] if idd else next(iter(out))
    return out, sorted([c for c in out if out[c]["type"] != "idle"],
                       key=lambda c: out[c]["priority"])


def resolve_ext(clip):
    """优先用 webp（体积约为 png 的 1/7），目录里没有就回退 png。"""
    m = CLIPS[clip]
    d = os.path.join(ROOT, m["dir"])
    for ext in ([m["format"]] if m.get("format") else []) + ["webp", "png"]:
        if os.path.exists(os.path.join(d, f"f0000.{ext}")):
            return ext
    return m.get("format") or "png"


def frame_file(clip, idx):
    ext = EXTS.get(clip) or EXTS.setdefault(clip, resolve_ext(clip))
    return os.path.join(ROOT, CLIPS[clip]["dir"], f"f{idx:04d}.{ext}")


def log(msg):
    if LOG_PATH:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def self_mem_mb():
    """读自己的工作集（外部进程在沙箱里量不到，自测最准）。"""
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    h = ctypes.windll.kernel32.OpenProcess(0x0410, False,
                                           ctypes.windll.kernel32.GetCurrentProcessId())
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    ctypes.windll.kernel32.CloseHandle(h)
    return pmc.WorkingSetSize / 1e6, pmc.PeakWorkingSetSize / 1e6


class Loader(threading.Thread):
    """后台解码线程：按请求把 PNG 读成显示尺寸的 QImage，放进共享缓存。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.jobs = deque()
        self.pending = set()
        self.cache = {}          # (clip, idx) -> QImage
        self._size = (0, 0)
        self.cond = threading.Condition()

    def request(self, clip, idx):
        with self.cond:
            key = (clip, idx)
            if key in self.cache or key in self.pending:
                return
            self.pending.add(key)
            self.jobs.append(key)
            self.cond.notify()

    def get(self, clip, idx):
        with self.cond:
            return self.cache.get((clip, idx))

    def reset(self, size):
        with self.cond:
            self._size = size
            self.cache.clear()
            self.pending.clear()
            self.jobs.clear()

    def drop_far(self, clip, idx, keep=KEEP_NEAR):
        with self.cond:
            for k in [k for k in self.cache if k[0] == clip and abs(k[1] - idx) > keep]:
                del self.cache[k]

    def run(self):
        while True:
            with self.cond:
                while not self.jobs:
                    self.cond.wait()
                clip, idx = self.jobs.popleft()
                w, h = self._size
            path = frame_file(clip, idx)
            img = QImage(path)
            if w and img.width() != w:
                img = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            with self.cond:
                self.cache[(clip, idx)] = img
                self.pending.discard((clip, idx))


class Pet(QWidget):
    def __init__(self, width: int):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.aspect = META["canvas"]["h"] / META["canvas"]["w"]
        self.width_px = width
        self.resize(width, round(width * self.aspect))

        self.loader = Loader()
        self.loader.start()

        self.clip = IDLE_ID
        self.idx = 0
        self.action_cursor = 0
        self.idle_resume = 1
        self.tier_name = "中"
        self.prev_img = None
        self.fade_start = None
        self.drag_pos = None
        self.fps = CLIPS[IDLE_ID]["fps"]

        # ---- 点击穿透：降采样 alpha 掩码 ----
        self.mask_cache = {}          # (clip, idx, w, h) -> QRegion（按帧缓存，不重复构建）
        self.mask_applied_key = None  # 已经 setMask 过的帧 key
        self.mask_updates = 0         # 实际调用 setMask 的次数
        self.mask_builds = 0          # 实际重建 QRegion 的次数（缓存未命中）
        self.mask_last_ms = 0.0       # 最近一次重建耗时
        self.mask_total_ms = 0.0
        self.next_mask_at = 0.0

        # 只同步读第 1 帧：窗口立刻可见，不等缓存预热
        self.loader.reset(self._size())
        self.current_img = self._load_now(IDLE_ID, 0)
        self._prefetch()
        self._apply_mask(force=True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(max(16, round(1000 / self.fps)))

    # ---- 帧 / 尺寸 ----
    def _size(self):
        return self.width_px, round(self.width_px * self.aspect)

    def _load_now(self, clip, idx):
        w, h = self._size()
        img = QImage(frame_file(clip, idx))
        if img.isNull():
            raise SystemExit(f"缺少帧文件: {clip} #{idx}")
        return img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)

    def _resize_to(self, width):
        self.width_px = width
        self.resize(width, round(width * self.aspect))
        self.loader.reset(self._size())
        self.current_img = self._load_now(self.clip, self.idx)
        self._prefetch()
        self.mask_cache.clear()          # 尺寸变了，旧掩码不能再用
        self.mask_applied_key = None
        self._apply_mask(force=True)

    # ---- 点击穿透：降采样 alpha 掩码 ----
    def _mask_build(self, img):
        """把显示尺寸的帧降采样成 MASK_DIV 网格的二值掩码，再按行合并成 QRegion。

        降采样用 FastTransformation（最近邻），每格取左上角那个像素，
        所以边界量化误差 ~MASK_DIV px。实测可见像素损失 1.4~1.9%（都在轮廓半透明
        毛边和发梢），1:1 看与原图无差别。
        """
        w, h = img.width(), img.height()
        cw, ch = max(1, w // MASK_DIV), max(1, h // MASK_DIV)
        alpha = img.convertToFormat(QImage.Format.Format_Alpha8)
        small = alpha.scaled(cw, ch, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.FastTransformation)
        buf = small.constBits()
        buf.setsize(small.sizeInBytes())
        raw = bytes(buf)
        stride = small.bytesPerLine()

        region = QRegion()
        for y in range(ch):
            base = y * stride
            x = 0
            while x < cw:
                if raw[base + x] >= MASK_ALPHA_MIN:
                    start = x
                    while x < cw and raw[base + x] >= MASK_ALPHA_MIN:
                        x += 1
                    region = region.united(
                        QRegion(start * MASK_DIV, y * MASK_DIV, (x - start) * MASK_DIV, MASK_DIV))
                else:
                    x += 1
        return region

    def _mask_ensure(self, key):
        region = self.mask_cache.get(key)
        if region is None:
            t0 = time.perf_counter()
            region = self._mask_build(self.current_img)
            dt = (time.perf_counter() - t0) * 1000
            self.mask_builds += 1
            self.mask_last_ms = dt
            self.mask_total_ms += dt
            self.mask_cache[key] = region
        return region

    def _apply_mask(self, force=False):
        """按当前帧刷新窗口掩码。节流 + 按帧缓存：同一帧（同一档尺寸）只算一次，
        待机循环第二轮起不会再重建 QRegion，只剩一次 setMask。"""
        now = time.monotonic()
        if not force and now < self.next_mask_at:
            return
        self.next_mask_at = now + MASK_REFRESH_MS / 1000
        key = (self.clip, self.idx, self.width(), self.height())
        if key == self.mask_applied_key:
            return
        self.setMask(self._mask_ensure(key))
        self.mask_applied_key = key
        self.mask_updates += 1

    def _prefetch(self):
        n = CLIPS[self.clip]["count"]
        for k in range(1, PREFETCH + 1):
            self.loader.request(self.clip, (self.idx + k) % n)

    def tick(self):
        n = CLIPS[self.clip]["count"]
        if not CLIPS[self.clip]["loop"] and self.idx >= n - 1:
            self._switch(IDLE_ID)
        else:
            self.idx = (self.idx + 1) % n
            self.current_img = self.loader.get(self.clip, self.idx) or self.current_img
        self._prefetch()
        self.loader.drop_far(self.clip, self.idx)
        self._apply_mask()
        self.update()

    def _switch(self, clip):
        self.prev_img = self.current_img
        self.fade_start = time.monotonic()
        if clip == IDLE_ID:
            self.idx = self.idle_resume
        else:
            self.idle_resume = (self.idx + 1) % CLIPS[IDLE_ID]["count"]
            self.idx = 0
        self.clip = clip
        self.loader.reset(self._size())
        self.current_img = self._load_now(clip, self.idx)
        self._prefetch()
        self._apply_mask(force=True)

    def play_action(self, cid):
        """播指定动作；播完 tick() 会自动回待机。"""
        if cid in CLIPS:
            self._switch(cid)
            log("action_start_ms=%.0f clip=%s" % ((time.monotonic() - T0) * 1000, cid))

    def play_next_action(self):
        """双击：按 clips.json 的优先级依次播放，播到最后一个再从头。"""
        if not ACTIONS:
            return
        self.play_action(ACTIONS[self.action_cursor % len(ACTIONS)])
        self.action_cursor = (self.action_cursor + 1) % len(ACTIONS)

    # ---- 绘制 ----
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        t = 1.0
        if self.fade_start is not None:
            t = min(1.0, (time.monotonic() - self.fade_start) * 1000 / FADE_MS)
            if t >= 1.0:
                self.fade_start = None
        if self.fade_start is not None and self.prev_img is not None:
            p.setOpacity(1.0 - t)
            p.drawImage(QRect(0, 0, self.width(), self.height()), self.prev_img)
        p.setOpacity(t)
        p.drawImage(QRect(0, 0, self.width(), self.height()), self.current_img)

    # ---- 鼠标 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self.drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self.drag_pos = None
        e.accept()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.clip == IDLE_ID:
            self.play_next_action()
        e.accept()

    def _build_menu(self):
        menu = QMenu(self)
        group = QActionGroup(self)
        for name in ("小", "中", "大"):
            act = QAction(f"尺寸：{name}", self)
            act.setCheckable(True)
            act.setChecked(name == self.tier_name)
            act.triggered.connect(lambda _, n=name: self.set_tier(n))
            group.addAction(act)
            menu.addAction(act)
        if ACTIONS:
            menu.addSeparator()
            for cid in ACTIONS:
                act = QAction("动作：" + CLIPS[cid]["name"], self)
                act.triggered.connect(lambda _, c=cid: self.play_action(c))
                menu.addAction(act)
        menu.addSeparator()
        quit_act = QAction("退出", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)
        return menu

    def contextMenuEvent(self, e):
        self._build_menu().exec(e.globalPos())

    def set_tier(self, name):
        self.tier_name = name
        self._resize_to(max(140, int(QApplication.primaryScreen().availableGeometry().width()
                                     * TIERS[name])))
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            QApplication.quit()


def main():
    global META, CLIPS, ACTIONS, LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=0, help="宠物宽度(像素)，默认中档=屏幕宽 15%%")
    ap.add_argument("--log", default="", help="把启动/动作时间点写到这个文件，用于测时")
    ap.add_argument("--clip", default="", help="启动时直接播某个动作（调试用）")
    ap.add_argument("--freeze", action="store_true",
                    help="调试用：停在第一帧不播，供命中测试取稳定样本")
    args = ap.parse_args()
    LOG_PATH = args.log or None

    META = json.load(open(os.path.join(ROOT, "meta.json"), encoding="utf-8"))
    CLIPS, ACTIONS = load_clips()

    app = QApplication(sys.argv)
    screen = app.primaryScreen().availableGeometry()
    if args.width:
        width, tier = args.width, ""
    else:
        tier = DEFAULT_TIER
        width = max(140, int(screen.width() * TIERS[tier]))

    pet = Pet(width)
    pet.tier_name = tier
    pet.move(screen.right() - pet.width() - 24, screen.bottom() - pet.height() - 12)
    pet.show()
    pet.repaint()          # 同步画完第一帧，再记时间
    if args.freeze:
        pet.timer.stop()
    log("first_paint_ms=%.0f width=%d pid=%d"
        % ((time.monotonic() - T0) * 1000, pet.width(), os.getpid()))
    QTimer.singleShot(6000, lambda: log(
        "mem_6s_mb=%.1f peak_mb=%.1f cached=%d mask_builds=%d mask_updates=%d "
        "mask_last_ms=%.2f mask_total_ms=%.2f"
        % (self_mem_mb()[0], self_mem_mb()[1], len(pet.loader.cache),
           pet.mask_builds, pet.mask_updates, pet.mask_last_ms, pet.mask_total_ms)))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
