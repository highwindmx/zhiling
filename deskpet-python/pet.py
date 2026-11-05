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

from state_map import StateMap

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
MANIFEST = None
CLIPS = {}
EXTS = {}          # 每段实际使用的帧后缀："webp" 或 "png"
ACTIONS = []       # 动作 clip 的 id，按优先级排序
IDLE_ID = "idle"
LOG_PATH = None
VERBOSE = False
# packs 默认随 Python 版一起放（deskpet-python/packs/），这样整个 Python 目录可整体搬走；
# 老仓库（packs 在仓库根）仍能跑：按下面的优先级取第一个存在的目录。
PACKS_ROOT = next(
    (p for p in (os.path.join(ROOT, "packs"), os.path.join(os.path.dirname(ROOT), "packs"))
     if os.path.isdir(p)),
    os.path.join(ROOT, "packs"))


def active_pack():
    """当前 pack：packs/active.json 指定；没有就挑第一个带 clips.json 的 pack；再没有返回 None。"""
    p = os.path.join(PACKS_ROOT, "active.json")
    try:
        with open(p, encoding="utf-8") as f:
            pid = (json.load(f) or {}).get("pack")
        if pid and os.path.isdir(os.path.join(PACKS_ROOT, pid)):
            return pid
    except Exception:
        pass
    if os.path.isdir(PACKS_ROOT):
        for d in sorted(os.listdir(PACKS_ROOT)):
            if os.path.exists(os.path.join(PACKS_ROOT, d, "clips.json")):
                return d
    return None


PACK_ID = active_pack()


def _set_pack(pid):
    """--pack / 菜单切 pack 时重算目录与状态映射路径（窗口尺寸随之按新 canvas 重算）。"""
    global PACK_ID, PACK_DIR, STATE_MAP_PATH
    PACK_ID = pid
    PACK_DIR = os.path.join(PACKS_ROOT, pid) if pid else ""
    STATE_MAP_PATH = os.path.join(PACK_DIR, "state-map.json") if PACK_DIR else ""


_set_pack(PACK_ID)


def pack_canvas():
    """画布是 pack 级属性：manifest.canvas 优先，其次 clips.json，最后旧的 meta.json。

    返回 (w, h) 与来源说明；任何地方都不硬编码 576/530（DEFAULT_CANVAS 只在建新 pack 时用）。
    """
    if MANIFEST and MANIFEST.get("canvas"):
        c = MANIFEST["canvas"]
        return (int(c["w"]), int(c["h"]), "manifest")
    if META and META.get("clips_raw") and META["clips_raw"].get("canvas"):
        c = META["clips_raw"]["canvas"]
        return (int(c["w"]), int(c["h"]), "clips.json")
    c = (META or {}).get("canvas") or {}
    if c.get("w") and c.get("h"):
        return (int(c["w"]), int(c["h"]), "meta.json(legacy)")
    raise SystemExit("没有可用的画布：packs/%s/manifest.json 缺 canvas" % (PACK_ID or "?"))


def validate_canvas_frames(canvas):
    """加载时校验：所有 clip 的首帧尺寸都必须等于 pack canvas。

    不一致时列出每一段的实际尺寸并退出，**不静默缩放到某一段的尺寸**。
    """
    from PyQt6.QtGui import QImageReader
    bad = []
    for cid, c in CLIPS.items():
        path = frame_file(cid, 0)
        if not os.path.exists(path):
            bad.append((cid, "缺首帧"))
            continue
        size = QImageReader(path).size()
        if not size.isValid():
            bad.append((cid, "首帧读不出来"))
            continue
        if (size.width(), size.height()) != canvas:
            bad.append((cid, "%dx%d" % (size.width(), size.height())))
    if bad:
        lines = ["packs/%s 的帧尺寸与 canvas=%dx%d 不一致：" % (PACK_ID, canvas[0], canvas[1])]
        lines += ["  %-14s %s" % (cid, got) for cid, got in bad]
        lines.append("同一 pack 内所有 clip 必须统一画布；跨 pack 的帧不能混用。"
                     "改 manifest.canvas 或重新导出这些段。")
        raise SystemExit("\n".join(lines))


def load_clips():
    """clips.json 管顺序/类型/中文名/defaultIdle；manifest/meta.json 管目录、帧数、fps、格式。

    优先读当前 pack 的 clips.json（pack 自洽），没有才回退 deskpet-python/clips.json（运行时索引）。
    """
    global IDLE_ID
    meta_clips = META.get("clips", {})
    path = os.path.join(PACK_DIR, "clips.json") if PACK_DIR else ""
    if not path or not os.path.exists(path):
        path = os.path.join(ROOT, "clips.json")
    default_idle = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        META["clips_raw"] = raw
        arr = raw.get("clips", [])
        default_idle = raw.get("defaultIdle")
    else:
        arr = [{"id": k, "type": "idle" if k == "idle" else "action",
                "loop": k == "idle", "displayName": k} for k in meta_clips]
    out = {}
    for e in arr:
        cid = e.get("id")
        if not cid:
            continue
        m = meta_clips.get(cid, {})
        out[cid] = {"dir": e.get("dir") or m.get("dir", "frames_" + cid),
                    "fps": e.get("fps") or m.get("fps", 24),
                    "count": e.get("count") or m.get("count", 1),
                    "format": m.get("format"),
                    "displayName": e.get("displayName") or e.get("name") or cid,
                    "type": e.get("type", "action"), "loop": bool(e.get("loop", False)),
                    "priority": e.get("priority", 99)}
    if default_idle and default_idle in out:
        IDLE_ID = default_idle
    else:
        idd = [c for c in out if out[c]["type"] == "idle"]
        IDLE_ID = idd[0] if idd else next(iter(out))
    return out, sorted([c for c in out if c != IDLE_ID],
                       key=lambda c: out[c]["priority"])


def frames_dir(m):
    """帧目录解析：packs/<pack>/<dir>（新格式）→ deskpet-python/<dir>（旧格式，兼容）。

    新结构是 packs/<pack>/frames/<clipId>；没迁移过的老仓库还在 deskpet-python/frames_*，
    两个位置都认，避免为了目录结构把已有帧搬来搬去（搬完 pack 里就只认第一处）。
    """
    d = m["dir"]
    for root in ((PACK_DIR,) if PACK_DIR else ()) + (ROOT,):
        p = os.path.join(root, d)
        if os.path.isdir(p):
            return p
    return os.path.join(ROOT, d)


def resolve_ext(clip):
    """优先用 webp（体积约为 png 的 1/7），目录里没有就回退 png。"""
    m = CLIPS[clip]
    d = frames_dir(m)
    for ext in ([m["format"]] if m.get("format") else []) + ["webp", "png"]:
        if os.path.exists(os.path.join(d, f"f0000.{ext}")):
            return ext
    return m.get("format") or "png"


def frame_file(clip, idx):
    ext = EXTS.get(clip) or EXTS.setdefault(clip, resolve_ext(clip))
    return os.path.join(frames_dir(CLIPS[clip]), f"f{idx:04d}.{ext}")


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

        cw, ch, cv_src = pack_canvas()
        self.canvas = (cw, ch)
        self.canvas_src = cv_src
        self.aspect = ch / cw
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
        self.press = None             # (global 坐标, 时刻) 用于区分单击/拖动
        self._dbl_t = 0.0             # 上次双击的时刻，用于吞掉双击的第二次松开
        self.on_clip_end = None       # 当前段播完的回调（状态映射用）
        self.cur_loop = True
        self.sm = None                # 状态映射（StateMap），main() 里创建
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
        if self.idx >= n - 1 and not self.cur_loop:
            cb, self.on_clip_end = self.on_clip_end, None
            if cb:
                cb()
            else:
                self._switch(IDLE_ID)
        else:
            self.idx = (self.idx + 1) % n
            self.current_img = self.loader.get(self.clip, self.idx) or self.current_img
        self._prefetch()
        self.loader.drop_far(self.clip, self.idx)
        self._apply_mask()
        self.update()

    def _switch(self, clip, loop=None, on_end=None):
        """切到某一段。loop=None 表示用 clips.json 里的 loop；状态映射可以覆盖。"""
        self.prev_img = self.current_img
        self.fade_start = time.monotonic()
        if clip == IDLE_ID:
            self.idx = self.idle_resume
        else:
            self.idle_resume = (self.idx + 1) % CLIPS[IDLE_ID]["count"]
            self.idx = 0
        self.clip = clip
        self.cur_loop = CLIPS[clip]["loop"] if loop is None else bool(loop)
        self.on_clip_end = on_end
        if self.sm is not None:
            self.sm.playing = clip
        self.loader.reset(self._size())
        self.current_img = self._load_now(clip, self.idx)
        self._prefetch()
        self._apply_mask(force=True)

    # ---- 状态映射对接 ----
    def state_play(self, clip_id, loop, on_end):
        """StateMap 的播放回调（在线程外、主线程里调用）。"""
        if clip_id not in CLIPS:
            log("state_play 缺少 clip: %s" % clip_id)
            return
        self._switch(clip_id, loop=loop, on_end=on_end)
        log("state_play clip=%s loop=%s" % (clip_id, loop))

    def state_play_idle(self):
        self._switch(IDLE_ID, loop=True)

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
            self.press = (e.globalPosition().toPoint(), time.monotonic())
            e.accept()

    def mouseMoveEvent(self, e):
        if self.drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        press, self.press = self.press, None
        self.drag_pos = None
        if press and self.sm is not None:
            p, t0 = press
            moved = (e.globalPosition().toPoint() - p).manhattanLength()
            # 双击的第二次 release 也会走到这里：400ms 内的松开不算单击，
            # 否则会把双击的炸毛顶成"得意"（并且状态被改写成 click）
            if time.monotonic() - self._dbl_t < 0.4:
                e.accept()
                return
            if moved < 4 and time.monotonic() - t0 < 0.4:
                self.sm.clear_reminder("单击")
                self.sm.set_state("click", "单击")
        e.accept()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.sm is not None:
            self._dbl_t = time.monotonic()
            self.sm.clear_reminder("双击")
            if "double_click" in self.sm.rules:
                self.sm.set_state("double_click", "双击")
            elif self.clip == IDLE_ID:
                self.play_next_action()
        e.accept()

    def open_workshop(self):
        """素材工坊是独立工具，不是运行时依赖：找不到就明确告知，不静默失败。"""
        import subprocess
        from PyQt6.QtWidgets import QMessageBox
        bat = os.path.join(ROOT, "workshop.bat")
        if not os.path.exists(bat):
            QMessageBox.warning(self, "素材工坊", "找不到 %s" % bat)
            return
        try:
            subprocess.Popen(["cmd", "/c", bat], cwd=ROOT)
            log("workshop_launch ok")
        except OSError as exc:
            QMessageBox.warning(self, "素材工坊", "启动失败：%s" % exc)

    def about_assets(self):
        """菜单「关于素材」：列出 pack 的 license 与画布。只在用户主动点开时出现，不自动弹窗。"""
        from PyQt6.QtWidgets import QMessageBox
        lic = (MANIFEST or {}).get("license") or {}
        kind = lic.get("kind", "unknown")
        lines = [
            "素材包：packs/%s" % (PACK_ID or "?"),
            "角色：%s" % ((MANIFEST or {}).get("role") or "（未填写）"),
            "画布：%dx%d（来源 %s）" % (self.canvas[0], self.canvas[1], self.canvas_src),
            "许可：%s" % kind,
            "来源：%s" % (lic.get("source") or "（未填写）"),
        ]
        if lic.get("note"):
            lines.append("备注：%s" % lic["note"])
        if kind == "unknown":
            lines.append("")
            lines.append("「unknown」不阻塞本地运行；导出/发布这个 pack 前请补 license.kind 与 source。")
        QMessageBox.information(self, "关于素材", "\n".join(lines))

    def _build_menu(self):
        menu = QMenu(self)
        group = QActionGroup(self)
        # 交互类与状态映射共用一份 priority，这里按同一顺序排，避免菜单和状态表的顺序对不上
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
                act = QAction("动作：" + CLIPS[cid]["displayName"], self)
                act.triggered.connect(lambda _, c=cid: self.play_action(c))
                menu.addAction(act)
        menu.addSeparator()
        workshop_act = QAction("素材工坊…", self)
        workshop_act.triggered.connect(self.open_workshop)
        menu.addAction(workshop_act)
        about_act = QAction("关于素材…", self)
        about_act.triggered.connect(self.about_assets)
        menu.addAction(about_act)
        quit_act = QAction("退出", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)
        return menu

    def contextMenuEvent(self, e):
        if self.sm is not None:
            self.sm.clear_reminder("右键")
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
    global META, MANIFEST, CLIPS, ACTIONS, LOG_PATH, VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=0, help="宠物宽度(像素)，默认中档=屏幕宽 15%%")
    ap.add_argument("--pack", default="", help="指定素材包（默认 packs/active.json）")
    ap.add_argument("--verbose", action="store_true", help="启动时打印 pack 画布与素材 license")
    ap.add_argument("--log", default="", help="把启动/动作时间点写到这个文件，用于测时")
    ap.add_argument("--clip", default="", help="启动时直接播某个动作（调试用）")
    ap.add_argument("--state", default="", help="启动时强制进入某个状态（调试用：click/done/…）")
    ap.add_argument("--state-map", default="", help="状态映射配置路径（默认当前 pack 的 state-map.json）")
    ap.add_argument("--freeze", action="store_true",
                    help="调试用：停在第一帧不播，供命中测试取稳定样本")
    args = ap.parse_args()
    LOG_PATH = args.log or None
    VERBOSE = args.verbose

    if args.pack:
        _set_pack(args.pack)
        if not os.path.isdir(os.path.join(PACKS_ROOT, args.pack)):
            raise SystemExit("找不到素材包：packs/%s" % args.pack)

    META = json.load(open(os.path.join(ROOT, "meta.json"), encoding="utf-8"))
    if PACK_DIR and os.path.exists(os.path.join(PACK_DIR, "manifest.json")):
        MANIFEST = json.load(open(os.path.join(PACK_DIR, "manifest.json"), encoding="utf-8"))
    CLIPS, ACTIONS = load_clips()
    canvas = pack_canvas()[:2]
    validate_canvas_frames(canvas)
    if PACK_ID:
        log("pack=%s（packs/%s），%d 段，待机=%s，canvas=%dx%d" %
            (PACK_ID, PACK_ID, len(CLIPS), IDLE_ID, canvas[0], canvas[1]))
    else:
        log("packs/ 里没有可用素材包，回退旧 deskpet-python/frames_* 目录；"
            "建议运行 python build/tools/migrate_packs.py 迁移到 packs/<packId>/")
    if VERBOSE:
        lic = (MANIFEST or {}).get("license") or {}
        print("[verbose] canvas=%dx%d（%s） license=%s 来源=%s%s"
              % (canvas[0], canvas[1], pack_canvas()[2],
                 lic.get("kind", "unknown"), lic.get("source") or "（未填写）",
                 (" 备注=" + lic["note"]) if lic.get("note") else ""), file=sys.stderr)

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

    # ---- 状态映射：同一份 packs/<packId>/state-map.json，热加载 ----
    light = {cid: {"fps": c["fps"], "count": c["count"], "name": c["displayName"]}
             for cid, c in CLIPS.items()}
    sm_path = args.state_map or STATE_MAP_PATH
    pet.sm = StateMap(sm_path, light, pet.state_play, pet.state_play_idle, log)
    pet.sm.playing = IDLE_ID
    pet.sm.set_state("idle", "启动")

    def state_tick():
        pet.sm.load()          # mtime 没变就什么都不做（800ms 级去抖按内容比对）
        pet.sm.tick()

    state_timer = QTimer(pet)
    state_timer.timeout.connect(state_tick)
    state_timer.start(1000)

    pet.show()
    pet.repaint()          # 同步画完第一帧，再记时间
    if args.state:
        pet.sm.set_state(args.state, "cli")
    if args.clip:
        pet.play_action(args.clip)
    if args.freeze:
        pet.timer.stop()
    log("first_paint_ms=%.0f width=%d height=%d pos=%d,%d canvas=%dx%d pid=%d"
        % ((time.monotonic() - T0) * 1000, pet.width(), pet.height(),
           pet.x(), pet.y(), pet.canvas[0], pet.canvas[1], os.getpid()))
    QTimer.singleShot(6000, lambda: log(
        "mem_6s_mb=%.1f peak_mb=%.1f cached=%d mask_builds=%d mask_updates=%d "
        "mask_last_ms=%.2f mask_total_ms=%.2f"
        % (self_mem_mb()[0], self_mem_mb()[1], len(pet.loader.cache),
           pet.mask_builds, pet.mask_updates, pet.mask_last_ms, pet.mask_total_ms)))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
