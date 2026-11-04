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
from PyQt6.QtGui import QImage, QPainter, QAction, QActionGroup
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

T0 = time.monotonic()
ROOT = os.path.dirname(os.path.abspath(__file__))

TIERS = {"小": 0.10, "中": 0.15, "大": 0.30}   # 占可用屏幕宽度的比例（中=原来默认值的一半）
DEFAULT_TIER = "中"
PREFETCH = 8          # 向前预取帧数
KEEP_NEAR = 14        # 缓存里保留当前帧前后多少帧
FADE_MS = 250         # 两段之间的交叉淡化时长

META = None
CLIPS = {}
LOG_PATH = None


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
            path = os.path.join(ROOT, CLIPS[clip]["dir"], f"f{idx:04d}.png")
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

        self.clip = "idle"
        self.idx = 0
        self.idle_resume = 1
        self.tier_name = "中"
        self.prev_img = None
        self.fade_start = None
        self.drag_pos = None
        self.fps = CLIPS["idle"]["fps"]

        # 只同步读第 1 帧：窗口立刻可见，不等缓存预热
        self.loader.reset(self._size())
        self.current_img = self._load_now("idle", 0)
        self._prefetch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(max(16, round(1000 / self.fps)))

    # ---- 帧 / 尺寸 ----
    def _size(self):
        return self.width_px, round(self.width_px * self.aspect)

    def _load_now(self, clip, idx):
        w, h = self._size()
        img = QImage(os.path.join(ROOT, CLIPS[clip]["dir"], f"f{idx:04d}.png"))
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

    def _prefetch(self):
        n = CLIPS[self.clip]["count"]
        for k in range(1, PREFETCH + 1):
            self.loader.request(self.clip, (self.idx + k) % n)

    def tick(self):
        n = CLIPS[self.clip]["count"]
        if self.clip == "action" and self.idx >= n - 1:
            self._switch("idle")
        else:
            self.idx = (self.idx + 1) % n
            self.current_img = self.loader.get(self.clip, self.idx) or self.current_img
        self._prefetch()
        self.loader.drop_far(self.clip, self.idx)
        self.update()

    def _switch(self, clip):
        self.prev_img = self.current_img
        self.fade_start = time.monotonic()
        if clip == "action":
            self.idle_resume = (self.idx + 1) % CLIPS["idle"]["count"]
            self.idx = 0
        else:
            self.idx = self.idle_resume
        self.clip = clip
        self.loader.reset(self._size())
        self.current_img = self._load_now(clip, self.idx)
        self._prefetch()

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
        if e.button() == Qt.MouseButton.LeftButton and self.clip == "idle":
            self._switch("action")
            log("action_start_ms=%.0f" % ((time.monotonic() - T0) * 1000))
        e.accept()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        group = QActionGroup(self)
        for name in ("小", "中", "大"):
            act = QAction(f"尺寸：{name}", self)
            act.setCheckable(True)
            act.setChecked(name == self.tier_name)
            act.triggered.connect(lambda _, n=name: self.set_tier(n))
            group.addAction(act)
            menu.addAction(act)
        menu.addSeparator()
        quit_act = QAction("退出", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)
        menu.exec(e.globalPos())

    def set_tier(self, name):
        self.tier_name = name
        self._resize_to(max(140, int(QApplication.primaryScreen().availableGeometry().width()
                                     * TIERS[name])))
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            QApplication.quit()


def main():
    global META, CLIPS, LOG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=0, help="宠物宽度(像素)，默认中档=屏幕宽 15%%")
    ap.add_argument("--log", default="", help="把启动/动作时间点写到这个文件，用于测时")
    args = ap.parse_args()
    LOG_PATH = args.log or None

    META = json.load(open(os.path.join(ROOT, "meta.json"), encoding="utf-8"))
    CLIPS = META["clips"]

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
    log("first_paint_ms=%.0f width=%d pid=%d"
        % ((time.monotonic() - T0) * 1000, pet.width(), os.getpid()))
    QTimer.singleShot(6000, lambda: log(
        "mem_6s_mb=%.1f peak_mb=%.1f cached=%d"
        % (self_mem_mb()[0], self_mem_mb()[1], len(pet.loader.cache))))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
