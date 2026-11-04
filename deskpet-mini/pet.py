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

        # 只同步读第 1 帧：窗口立刻可见，不等缓存预热
        self.loader.reset(self._size())
        self.current_img = self._load_now(IDLE_ID, 0)
        self._prefetch()

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
    log("first_paint_ms=%.0f width=%d pid=%d"
        % ((time.monotonic() - T0) * 1000, pet.width(), os.getpid()))
    QTimer.singleShot(6000, lambda: log(
        "mem_6s_mb=%.1f peak_mb=%.1f cached=%d"
        % (self_mem_mb()[0], self_mem_mb()[1], len(pet.loader.cache))))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
