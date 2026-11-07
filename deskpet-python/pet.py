"""最小桌宠：透明窗口循环播放待机段，双击播一次动作段，可拖动、可换档、可退出。

用法:
    python pet.py                 # 默认中档（屏幕宽 15%）
    python pet.py --width 400     # 指定宠物宽度(像素)
"""
import argparse
import datetime
import glob
import json
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from collections import deque

from PyQt6.QtCore import (Qt, QTimer, QRect, QProcess, QSize, QObject, pyqtSignal,
                         QThread)
from PyQt6.QtGui import (QImage, QPainter, QAction, QActionGroup, QRegion, QIcon,
                         QPixmap, QColor, QFont, QImageReader, QCursor)
from PyQt6.QtWidgets import (QApplication, QMenu, QWidget, QSystemTrayIcon, QDialog,
                             QComboBox, QCheckBox, QSpinBox, QLabel, QVBoxLayout,
                             QHBoxLayout, QPushButton, QScrollArea, QFrame, QGridLayout,
                             QListWidget, QListWidgetItem, QStyledItemDelegate,
                             QStyleOptionViewItem, QStyle, QSizePolicy,
                             QLineEdit, QMessageBox, QAbstractItemView, QInputDialog,
                             QGroupBox, QFileDialog, QTabBar, QStackedWidget)

from state_map import StateMap
from shared_constants import DEFAULT_DSH_WS_PORT, DEFAULT_STATE_PORT

T0 = time.monotonic()
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "build", "tools"))
import pack as packlib          # v2 数据层：主题 / 动画资产 / 画布整数运算都从这里走
from ui_theme import P as UI, QSS, dark_palette, LINK_COLORS   # 两界面共用配色


def load_app_icon() -> QIcon:
    """窗口/托盘图标：assets/icon.ico（多尺寸）优先，回退 icon.png；都没有就空图标。"""
    for name in ("icon.ico", "icon.png"):
        p = os.path.join(ROOT, "assets", name)
        if os.path.exists(p):
            return QIcon(p)
    return QIcon()

TIERS = {"小": 0.10, "中": 0.15, "大": 0.30}   # 占可用屏幕宽度的比例（中=原来默认值的一半）
DEFAULT_TIER = "中"
# 配置统一由 packlib 解析：%LOCALAPPDATA%\ZhiLing\config.json（与 Tauri 共用），
# DESKPET_CONFIG 可覆盖；旧 deskpet-python\config.json 首次访问时自动合并迁移。


def load_config():
    """读共享配置（packlib 同一解析函数 + 旧文件一次性迁移）。"""
    return packlib.user_config()


def save_config(patch):
    """读-改-写：只写入 patch 里的键，保留 Tauri 版等其它工具写的未知键。"""
    try:
        return packlib.update_user_config(patch)
    except OSError as e:
        log("config 写入失败: %s" % e)
        return {}


def _pid_alive(pid):
    """Windows 探活：OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + CloseHandle。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, int(pid))
        if h:
            k32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


# ---- 番茄钟：纯计时逻辑，不依赖 Qt，便于离屏/单测 ---------------------------
class Pomodoro:
    """专注 / 休息两阶段计时器。

    剩余时间永远用「单调时钟 deadline - now」现算，不靠定时器每次自减：
    窗口隐藏、系统卡顿、定时器合并都不会让计时漂移。

    状态：phase=None（未开始）→ focus（专注）→ break（休息）→ focus…
    tick() 只在 running 且越过 deadline 时推进阶段，返回 "finished:<phase>"。
    """

    FOCUS = "focus"
    BREAK = "break"

    def __init__(self, work_min=25, break_min=5, now=time.monotonic):
        self._now = now
        self.enabled = True
        self.work_min = max(1, int(work_min))
        self.break_min = max(1, int(break_min))
        self.phase = None           # None / "focus" / "break"
        self.running = False
        self.started = False        # 当前阶段是否已经开始过（区分「已暂停 / 未开始」）
        self._deadline = 0.0
        self._remaining = 0.0       # 暂停或未开始时的剩余秒数

    # ---- 配置 ---------------------------------------------------------------
    def configure(self, enabled=None, work_min=None, break_min=None):
        if enabled is not None:
            self.enabled = bool(enabled)
        if work_min is not None:
            self.work_min = max(1, int(work_min))
        if break_min is not None:
            self.break_min = max(1, int(break_min))
        if not self.enabled:
            self.stop()
        elif self.phase and not self.running and not self.started:
            # 「待开始」阶段改了时长：剩余时间跟着变（已开始的阶段不重算，避免跳变）
            self._remaining = self.duration(self.phase)

    def duration(self, phase=None):
        mins = self.work_min if (phase or self.phase) == self.FOCUS else self.break_min
        return max(1, int(mins) * 60)

    def remaining(self):
        if self.running:
            return max(0.0, self._deadline - self._now())
        return max(0.0, self._remaining)

    def remaining_text(self):
        """MM:SS；向上取整，刚开始时显示完整时长，不出现 24:59 的观感跳变。"""
        sec = int(math.ceil(self.remaining()))
        return "%02d:%02d" % (sec // 60, sec % 60)

    def phase_cn(self):
        return {self.FOCUS: "专注", self.BREAK: "休息"}.get(self.phase, "")

    def status_text(self):
        if not self.enabled:
            return "已关闭"
        if not self.phase:
            return "未开始（专注 %d 分钟）" % self.work_min
        suffix = "" if self.running else ("（已暂停）" if self.started else "（未开始）")
        return "%s %s%s" % (self.phase_cn(), self.remaining_text(), suffix)

    def start_label(self):
        """托盘「开始」项的文案：未开始/继续/开始休息。"""
        if not self.phase:
            return "开始（专注 %d 分钟）" % self.work_min
        if self.running:
            return "进行中…"
        if self.started:
            return "继续（%s）" % self.remaining_text()
        return "开始（%s %s）" % (self.phase_cn(), self.remaining_text())

    # ---- 操作 ---------------------------------------------------------------
    def start(self):
        """开始/继续当前阶段；返回是否真的启动了（已在跑 / 已关闭返回 False）。"""
        if not self.enabled or self.running:
            return False
        if not self.phase:
            self.phase = self.FOCUS
            self._remaining = self.duration(self.phase)
            self.started = False
        if self._remaining <= 0:
            self._remaining = self.duration(self.phase)
        self._deadline = self._now() + self._remaining
        self.running = True
        self.started = True
        return True

    def pause(self):
        if not self.running:
            return False
        self._remaining = max(0.0, self._deadline - self._now())
        self.running = False
        return True

    def stop(self):
        """停止并清空阶段；返回被停止的阶段（None 表示本来就没在计时）。"""
        was = self.phase
        self.phase = None
        self.running = False
        self.started = False
        self._deadline = 0.0
        self._remaining = 0.0
        return was

    def tick(self):
        """到点推进到下一阶段（设为待开始，不自动跑）；返回 "finished:<phase>"。"""
        if not (self.running and self.phase):
            return ""
        if self._now() < self._deadline:
            return ""
        finished = self.phase
        self.running = False
        self.started = False
        self.phase = self.BREAK if finished == self.FOCUS else self.FOCUS
        self._remaining = self.duration(self.phase)
        return "finished:%s" % finished


# 右键/托盘「动作」按系列分组；组外剩余的 clip 自动落到「其他」
ACTION_GROUPS = (
    ("工作", ("dsm_work", "dsm_think", "dsm_phone", "dsm_handover")),
    ("情绪", ("dsm_happy", "dsm_proud", "dsm_unhappy", "dsm_shock", "dsm_cheer")),
    ("特殊", ("dsm_yawn", "dsm_helpless", "dsm_tired", "dsm_rice", "dsm_handup")),
)

# ---- v2：13 行状态（long_idle 拆两档，番茄钟新增 focus / focus_break）----
STATE_KEYS = packlib.STATE_KEYS
STATE_CN = dict(packlib.STATE_CN_V2)
DEFAULT_MAP = {st: ("", packlib.DEFAULT_PRIORITY[st]) for st in STATE_KEYS}
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
LOG_ENABLED = False
VERBOSE = False
LOG_MAX_BYTES = 2 * 1024 * 1024         # 单文件上限 2 MB；归档固定为 pet.log.1（保留 1 份）
_ROTATE_WARNED = False
# ---- 多主题：当前主题由 packs/themes/*.json 的 active 字段决定（唯一真相）-----
# 素材目录只由 packlib 统一解析：DESKPET_PACKS → config.json:packsDir →
# %LOCALAPPDATA%\ZhiLing\packs → deskpet-python/packs → 仓库根/packs。
# 所有主题索引 / 动画资产 / 画布都通过 packlib 的 v2 API 解析，本文件不再自己拼路径。
PACKS_ROOT = packlib.packs_root()
PACK_CLI_LOCKED = False     # --pack 显式指定后置 True：热加载不再跟随主题 active 标记


def active_pack():
    """v2：当前主题 id（themes/*.json 里 active=true 的那份）。"""
    return packlib.active_theme()


PACK_ID = active_pack()
PACK_INFO = {}
PACK_DIR = ""
STATE_MAP_PATH = ""


def _set_pack(pid):
    """统一解析：主题索引路径、idle 段、初始画布全部来自 v2 数据层。"""
    global PACK_ID, PACK_DIR, STATE_MAP_PATH, PACK_INFO
    PACK_ID = pid or packlib.active_theme()
    PACK_DIR = packlib.packs_root()
    STATE_MAP_PATH = packlib.theme_path(PACK_ID)
    theme = packlib.load_theme(PACK_ID)
    idle_pool = (theme["states"].get("idle") or {}).get("clipIds") or []
    first = idle_pool[0] if idle_pool else ""
    meta = packlib.load_asset(first) if first else None
    canvas = packlib.aspect_pair((meta or {}).get("canvas"))
    PACK_INFO = {"exists": True, "canvas": canvas, "idle": first, "theme": theme}


_set_pack(PACK_ID)


def ensure_clip(ref, default_pack=None):
    """v2：ref 就是全局 assetId；不存在时从 animations/<id>/meta.json 登记进 CLIPS。"""
    key = str(ref or "")
    if not key:
        return None
    if key in CLIPS:
        return key
    meta = packlib.load_asset(key)
    if not meta:
        return None
    c = dict(meta)
    c["dir"] = meta.get("dir") or packlib.asset_dir(key)
    c["fps"] = c.get("fps") or 24
    c["count"] = c.get("count") or 1
    c["displayName"] = c.get("name") or key
    c["loop"] = bool(c.get("loop", False))
    c["type"] = c.get("type", "action")
    c["priority"] = c.get("priority", 99)
    c["canvas"] = packlib.aspect_pair(c.get("canvas"))
    CLIPS[key] = c
    return key


def clip_ref_info(ref, default_pack=None):
    """兼容入口：返回段的帧目录与自己 meta 的画布（整数对）。"""
    key = ensure_clip(ref)
    if not key:
        return None
    c = CLIPS[key]
    return {"pack": PACK_ID, "clip_id": key, "clip": c, "dir": c["dir"],
            "canvas": c["canvas"]}


def state_map_refs(pack_id=None):
    """主题索引里引用的所有 assetId（clipIds + fallbackClipIds）。"""
    theme = packlib.load_theme(pack_id or PACK_ID)
    out = []
    for r in (theme.get("states") or {}).values():
        if not isinstance(r, dict):
            continue
        for k in ("clipIds", "fallbackClipIds"):
            for c in (r.get(k) or []):
                if isinstance(c, str) and c and c not in out:
                    out.append(c)
    return out


def light_clips(pack_id=None):
    """StateMap 的 clips 映射：先登记主题引用的段，再给全部段 fps/count/name。"""
    for ref in state_map_refs(pack_id):
        ensure_clip(ref)
    return {k: {"fps": c["fps"], "count": c["count"], "name": c["displayName"]}
            for k, c in CLIPS.items()}


def pack_canvas(asset_id=None):
    """显示画布：默认取 idle 段的源画布；指定段时返回该段画布（全程整数，无浮点）。"""
    key = asset_id or (PACK_INFO.get("idle") or IDLE_ID)
    c = CLIPS.get(key) or packlib.load_asset(key) or {}
    w, h = packlib.aspect_pair(c.get("canvas"))
    return (w, h, "asset.meta.canvas")


def validate_canvas_frames():
    """逐段校验：每段首帧尺寸 == 自己 meta.canvas。

    v1 要求「同一 pack 所有段统一画布 / 跨 pack 帧不能混用」，v2 已作废：
    动画是全局资产，每段自带画布；只要求一段内部帧尺寸一致（首帧为代表）。
    不同段允许不同画布，切段时由 Pet._switch 用整数比例预判决定是否 resize。
    """
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
        w, h = c.get("canvas") or packlib.V2_DEFAULT_CANVAS
        if (size.width(), size.height()) != (w, h):
            bad.append((cid, "%dx%d ≠ %dx%d" % (size.width(), size.height(), w, h)))
    if bad:
        lines = ["段的帧尺寸与自己的 meta.canvas 不一致："]
        lines += ["  %-16s %s" % (cid, got) for cid, got in bad]
        lines.append("同一段内所有帧必须同画布；不同段允许不同画布。")
        raise SystemExit("\n".join(lines))


def load_clips(pack_id=None):
    """v2：扫 animations/*/meta.json → CLIPS（assetId 唯一身份）+ ACTIONS。"""
    global IDLE_ID
    assets = packlib.list_assets()
    out = {}
    for aid, m in assets.items():
        out[aid] = {
            "dir": m.get("dir") or packlib.asset_dir(aid),
            "fps": m.get("fps") or 24, "count": m.get("count") or 1,
            "format": m.get("format") or "webp",
            "displayName": m.get("name") or aid,
            "type": m.get("type") or "action",
            "loop": bool(m.get("loop", False)),
            "priority": m.get("priority", 99),
            "canvas": packlib.aspect_pair(m.get("canvas")),
        }
    theme = packlib.load_theme(pack_id or PACK_ID)
    pool = (theme["states"].get("idle") or {}).get("clipIds") or []
    IDLE_ID = pool[0] if pool and pool[0] in out else next(iter(out), "idle")
    out.setdefault(IDLE_ID, {"dir": packlib.asset_dir(IDLE_ID), "fps": 24, "count": 1,
                             "format": "webp", "displayName": IDLE_ID, "type": "idle",
                             "loop": True, "priority": 0,
                             "canvas": packlib.V2_DEFAULT_CANVAS})
    actions = sorted([c for c in out if c != IDLE_ID], key=lambda c: out[c]["priority"])
    return out, actions


def frames_dir(m):
    """v2：dir 一律是 animations/<assetId> 绝对路径，meta.json 是唯一路径来源。"""
    return m["dir"]


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


# ---- 设置面板缩略图（内存缓存，避免同一段反复解码）-------------------------
THUMB_PX = 48
_THUMB_CACHE = {}          # clipId -> QPixmap(48×48，已叠棋盘格)
_CHECKER = None


def _checkerboard():
    """48×48 棋盘格底，透出透明区域；只建一次。"""
    global _CHECKER
    if _CHECKER is None:
        pm = QPixmap(THUMB_PX, THUMB_PX)
        pm.fill(QColor(UI["raised"]))
        p = QPainter(pm)
        step = 8
        for y in range(0, THUMB_PX, step):
            for x in range(0, THUMB_PX, step):
                if ((x // step) + (y // step)) % 2 == 0:
                    p.fillRect(x, y, step, step, QColor(UI["surface"]))
        p.end()
        _CHECKER = pm
    return _CHECKER


def clip_thumb(cid):
    """取该段首帧（优先 f0000，退 f0002/f0001）缩到 48×48；缺帧显示「无帧」。"""
    if cid in _THUMB_CACHE:
        return _THUMB_CACHE[cid]
    pm = QPixmap(_checkerboard())
    img = QImage()
    if cid in CLIPS:
        for idx in (0, 2, 1):
            try:
                path = frame_file(cid, idx)
            except (Exception, SystemExit):    # 缺帧时 frame_file 会 SystemExit，缩略图只需显示「无帧」
                continue
            if os.path.exists(path) and img.load(path):
                break
    p = QPainter(pm)
    if img.isNull():
        p.setPen(QColor(UI["muted"]))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "无帧")
    else:
        sc = img.scaled(THUMB_PX, THUMB_PX, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
        p.drawImage((THUMB_PX - sc.width()) // 2, (THUMB_PX - sc.height()) // 2, sc)
    p.end()
    _THUMB_CACHE[cid] = pm
    return pm


def tray_log(msg):
    """托盘诊断并入主日志：默认关闭，排障时用 DESKPET_LOG 打开（pythonw 下也能看）。"""
    log("[tray] " + msg)


def integrity_level():
    """当前进程令牌完整性 SID：Low(4096) 会被 explorer(Medium) 拒绝托盘注册（ERROR_ACCESS_DENIED）。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        adv.OpenProcessToken.restype = wt.BOOL
        adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD,
                                            ctypes.POINTER(wt.DWORD)]
        adv.GetTokenInformation.restype = wt.BOOL
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        adv.ConvertSidToStringSidW.restype = wt.BOOL
        h = k32.OpenProcess(0x1000, False, k32.GetCurrentProcessId())
        if not h:
            return "?"
        ht = wt.HANDLE()
        if not adv.OpenProcessToken(h, 0x8, ctypes.byref(ht)):
            k32.CloseHandle(h)
            return "?"
        n = wt.DWORD(0)
        adv.GetTokenInformation(ht, 25, None, 0, ctypes.byref(n))
        buf = ctypes.create_string_buffer(n.value)
        adv.GetTokenInformation(ht, 25, buf, n.value, ctypes.byref(n))
        s = ctypes.c_wchar_p()
        p = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        out = s.value if adv.ConvertSidToStringSidW(ctypes.c_void_p(p), ctypes.byref(s)) else "?"
        k32.CloseHandle(ht)
        k32.CloseHandle(h)
        return out
    except Exception:                          # noqa: BLE001
        return "?"


def _always_log_path():
    """致命错误/迁移说明的落点：日志关闭时也写默认位置的 pet.log。"""
    if LOG_PATH:
        return LOG_PATH
    return os.path.join(packlib.app_root(), "pet.log")


def _rotate_log(path):
    """超过 LOG_MAX_BYTES 就把 path 轮转成 path.1（只保留 1 份）；失败只告警一次。"""
    global _ROTATE_WARNED
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= LOG_MAX_BYTES:
            return
        bak = path + ".1"
        if os.path.exists(bak):
            os.remove(bak)
        os.replace(path, bak)
    except OSError as e:
        # 轮转失败不能影响主流程（PyQt 槽函数里抛异常会 abort），只提醒一次
        if not _ROTATE_WARNED:
            _ROTATE_WARNED = True
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write("%s [log] rotate failed: %s\n"
                            % (time.strftime("%H:%M:%S"), e))
            except OSError:
                pass
            if sys.stderr is not None:
                print("log rotate failed: %s" % e, file=sys.stderr)


def _write_log(path, msg):
    line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        _rotate_log(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        # 日志写失败不能反过来把槽函数炸掉（PyQt 里异常会 abort）。
        # pythonw 下 sys.stderr is None，直接 print(file=None) 会抛 AttributeError，必须先判空。
        if sys.stderr is not None:
            print("log write failed: %s" % e, file=sys.stderr)


def log(msg):
    """常规事件日志：默认关闭（DESKPET_LOG=1 或 --log 打开）。"""
    if not LOG_ENABLED:
        return
    _write_log(_always_log_path(), msg)


def log_fatal(msg):
    """致命错误：默认也写一条，保证 pythonw 下崩溃有据可查。"""
    _write_log(_always_log_path(), msg)


def _resolve_log(arg_path, verbose):
    """解析日志开关：--log 路径 > DESKPET_LOG > 关闭。

    DESKPET_LOG 取值：1/on/true/yes 打开并写默认位置；0/off/false/no 关闭；
    其它非空值当作日志文件路径（保留 Tauri 版既有语义）。
    """
    global LOG_PATH, LOG_ENABLED
    default = os.path.join(packlib.app_root(), "pet.log")
    env = os.environ.get("DESKPET_LOG", "").strip()
    if arg_path:
        LOG_PATH, LOG_ENABLED = os.path.abspath(os.path.expanduser(arg_path)), True
        return
    low = env.lower()
    if env and low not in ("0", "off", "false", "no"):
        if low in ("1", "on", "true", "yes"):
            LOG_PATH = default
        else:
            LOG_PATH = os.path.abspath(os.path.expanduser(env))
        LOG_ENABLED = True
        return
    LOG_PATH = default
    LOG_ENABLED = bool(verbose)          # --verbose 也顺带打开日志（pythonw 下 stdout 不可见）


# ---- 首次启动：示例素材兜底 + 无素材时的可见提示（pythonw 下也有窗口）----
REPO_README_URL = ("https://github.com/highwindmx/zhiling/blob/main/README.md"
                   "#获取与安装素材包")
FIRST_RUN_MARKER = os.path.join(ROOT, "build", "cache", "first_run.prompted")


def open_install_readme():
    """打开 README「获取与安装素材包」：先试本地文件，失败再开仓库页面。返回方式字符串。"""
    local = os.path.join(packlib.REPO, "README.md")
    if os.path.isfile(local):
        try:
            os.startfile(local)                 # Windows：用默认 md 查看器打开
            return "local:%s" % local
        except OSError as e:
            log("first_run: 打开本地 README 失败: %s" % e)
    try:
        webbrowser.open(REPO_README_URL)
        return "url:%s" % REPO_README_URL
    except Exception as e:                      # noqa: BLE001
        log("first_run: 打开仓库页面失败: %r" % (e,))
        return "none"


def first_run_prompted():
    return os.path.exists(FIRST_RUN_MARKER)


def mark_first_run_prompted():
    try:
        os.makedirs(os.path.dirname(FIRST_RUN_MARKER), exist_ok=True)
        with open(FIRST_RUN_MARKER, "a", encoding="utf-8"):
            pass
    except OSError as e:
        log("first_run: 写提示标记失败: %s" % e)


def first_run_asset_guard():
    """按现有解析顺序仍没有可用素材时的可见兜底。True=继续启动，False=退出。

    用 QMessageBox / QFileDialog，不依赖 stdout，pythonw + run.vbs 下同样可见。
    主提示在同一次安装只弹一次（标记 build/cache/first_run.prompted）。
    """
    if first_run_prompted():
        log("first_run: 仍无可用素材；首次引导已提示过（%s），本次不再弹窗" % FIRST_RUN_MARKER)
        return False
    mark_first_run_prompted()
    log("first_run: 无可用素材，弹出首次引导（packs_root=%s）" % packlib.packs_root())
    box = QMessageBox()
    box.setWindowTitle("缺少桌宠素材")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText("没有找到可用的桌宠素材。")
    box.setInformativeText(
        "完整素材包请按 README「获取与安装素材包」解压到：\n%s\n"
        "解压后该目录应能直接看到 themes 与 animations。" % packlib.default_packs_dir())
    b_pick = box.addButton("选择素材目录…", QMessageBox.ButtonRole.AcceptRole)
    b_readme = box.addButton("查看安装说明", QMessageBox.ButtonRole.ActionRole)
    box.addButton("退出", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(b_pick)
    # 无主窗口时也置顶，避免提示框被别的窗口盖住（pythonw 启动尤其需要）
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    box.exec()
    clicked = box.clickedButton()
    if clicked == b_pick:
        while True:
            d = QFileDialog.getExistingDirectory(
                None, "选择素材目录（应能直接看到 themes 与 animations）",
                packlib.default_packs_dir())
            if not d:
                log("first_run: 用户取消选择素材目录，退出")
                return False
            if packlib.has_usable_material(d):
                try:
                    packlib.update_user_config({"packsDir": d})
                except OSError as e:
                    log("first_run: 写入 packsDir 失败: %s" % e)
                    QMessageBox.critical(None, "保存失败", "无法写入配置：%s" % e)
                    return False
                log("first_run: 已选素材目录并写入 packsDir=%s，继续启动" % d)
                return True
            log("first_run: 所选目录不可用（缺 themes/idle 帧）：%s" % d)
            QMessageBox.warning(
                None, "目录不可用",
                "该目录没有可用素材：需要 themes/*.json，且 idle 绑定的动画有帧。\n"
                "请选择解压后的 packs 目录本身。")
    elif clicked == b_readme:
        how = open_install_readme()
        log("first_run: 已打开安装说明（%s），退出" % how)
        return False
    log("first_run: 用户选择退出")
    return False


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
            # 切主题时 apply_pack 会 clear()/重建 CLIPS，而本线程可能正拿着上一主题的
            # (clip, idx) 作业。此时 frame_file 会 KeyError —— 丢弃这帧即可，不能让
            # 后台线程抛未捕获异常（不致命，但会污染日志且该帧永远留在 pending 里）。
            try:
                path = frame_file(clip, idx)
                img = QImage(path)
                # 与 _load_now 完全对齐：按 (w,h) 一起判断。
                # v1 只判宽度，「宽同高不同」时不缩放而 _load_now 会拉高，两条路径分叉。
                if w and (img.width() != w or img.height() != h):
                    img = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            except Exception:                   # noqa: BLE001 - 切主题竞态/坏帧都跳过
                with self.cond:
                    self.pending.discard((clip, idx))
                continue
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
        self.setWindowIcon(load_app_icon())

        cw, ch, cv_src = pack_canvas(IDLE_ID)
        self.canvas = (cw, ch)
        self.canvas_wh = (cw, ch)      # 整数对：B 方案窗口比例唯一来源，绝不存浮点 aspect
        self.canvas_src = cv_src
        self.width_px = width
        self.resize(width, packlib.display_height(width, {"w": cw, "h": ch}))

        self.loader = Loader()
        self.loader.start()

        self.clip = IDLE_ID
        self.idx = 0
        self.action_cursor = 0
        self.idle_resume = 1
        self.tier_name = "中"
        self.hold_reminders = True    # 提醒态常驻（config.json: remindHold，设置里可关）
        self.pomodoro = Pomodoro()    # main() 按 config.json 重新配置并挂 1s 心跳
        self._pomo_state = None       # 番茄钟当前实际打到的状态键（focus / busy / focus_break / idle）
        self.tray_pomo = None         # 托盘「番茄钟 ▸」的 QMenu 与 QAction 引用（setup_tray 填）
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
        """窗口显示尺寸：宽度来自尺寸档位，高度由当前段画布按整数比例算出（零浮点）。"""
        w, h = self.canvas_wh
        return self.width_px, packlib.display_height(self.width_px, {"w": w, "h": h})

    def _load_now(self, clip, idx):
        w, h = self._size()
        img = QImage(frame_file(clip, idx))
        if img.isNull():
            raise SystemExit(f"缺少帧文件: {clip} #{idx}")
        return img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)

    def _apply_canvas(self, new_wh):
        """B 方案：只有比例真的变了才调用（调用方已用整数交叉相乘判定）。

        resize 后保持窗口右下角不动 —— v1 的 pet.move() 只在启动跑一次，
        高度一变而 top 不变会让桌宠上下窜，那比尺寸变化本身更扎眼。
        同比例时连 resize() 都不调用，所以同画布段之间零跳变。
        """
        right, bottom = self.x() + self.width(), self.y() + self.height()
        self.canvas_wh = tuple(new_wh)
        self.canvas = tuple(new_wh)
        self.resize(self.width_px, self._size()[1])
        self.move(right - self.width(), bottom - self.height())

    def _resize_to(self, width):
        self.width_px = width
        self.resize(width, self._size()[1])
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
        # 轮廓变了：指针可能已经不在新形状里（只在手型状态下才检查，正常悬停零开销）
        if self.cursor().shape() == Qt.CursorShape.PointingHandCursor:
            self._sync_cursor()

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
        """切到某一段；assetId 是全局唯一身份，不再有 packId:clipId 跨系列引用。

        B+预判：先取目标段 meta.canvas，用 packlib.same_aspect() 的**整数交叉相乘**
        比较比例（w1*h2 == w2*h1，绝不用浮点/eps）。同比例 → 只换帧，不 resize、
        不 move；不同比例 → 才 _apply_canvas()，桌宠跳一下（用户已认可这个取舍）。
        """
        key = ensure_clip(clip, PACK_ID)
        if not key:
            log("切换失败：找不到动画 %r" % (clip,))
            return False
        target_wh = CLIPS[key].get("canvas") or packlib.V2_DEFAULT_CANVAS
        cur = {"w": self.canvas_wh[0], "h": self.canvas_wh[1]}
        if not packlib.same_aspect(cur, {"w": target_wh[0], "h": target_wh[1]}):
            self._apply_canvas(target_wh)
            log("canvas_change %dx%d → %dx%d（比例不同，窗口按段自适应）"
                % (cur["w"], cur["h"], target_wh[0], target_wh[1]))
        self.prev_img = self.current_img
        self.fade_start = time.monotonic()
        if key == IDLE_ID:
            self.idx = self.idle_resume
        else:
            self.idle_resume = (self.idx + 1) % CLIPS[IDLE_ID]["count"]
            self.idx = 0
        self.clip = key
        self.cur_loop = CLIPS[key]["loop"] if loop is None else bool(loop)
        self.fps = CLIPS[key]["fps"] or self.fps
        self.timer.setInterval(max(16, round(1000 / self.fps)))
        self.on_clip_end = on_end
        if self.sm is not None:
            self.sm.playing = key
        self.loader.reset(self._size())
        self.current_img = self._load_now(key, self.idx)
        self._prefetch()
        self._apply_mask(force=True)
        return True

    # ---- 状态映射对接 ----
    def state_play(self, clip_id, loop, on_end):
        """StateMap 的播放回调（在线程外、主线程里调用）。"""
        if ensure_clip(clip_id, PACK_ID) is None:
            log("state_play 缺少 clip: %s（主题 %s）" % (clip_id, PACK_ID))
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

    # ---- 悬停光标：只在可见形状内变手型 ----
    def _set_cursor_shape(self, shape):
        """光标形状没变就不调用 setCursor：setCursor 会触发一次光标刷新，
        每帧都调等于白白增加开销（鼠标悬停本身也会带来大量 move 事件）。"""
        if self.cursor().shape() != shape:
            self.setCursor(shape)

    def _sync_cursor(self, global_pos=None):
        """指针在「窗口矩形 ∩ 当前掩码」内才用手型。

        窗口是 transparent + frameless + setMask(QRegion)：Windows 下 setMask 走
        SetWindowRgn，mask 之外的鼠标消息直接落到下面的窗口，本窗口收不到 enter，
        本来就是穿透的。这里再按 mask() 判一次，是给两类情况兜底：
          ① 平台/尺寸变化瞬间，"整块矩形"也会触发 enter；
          ② 动画换帧后轮廓变了，指针可能已经从新形状里"漏出去"。
        """
        gp = global_pos if global_pos is not None else QCursor.pos()
        pos = self.mapFromGlobal(gp)
        inside = self.rect().contains(pos) and self.mask().contains(pos)
        self._set_cursor_shape(Qt.CursorShape.PointingHandCursor if inside
                               else Qt.CursorShape.ArrowCursor)

    def enterEvent(self, e):
        self._sync_cursor()               # 只在可见区域命中时才是手型
        super().enterEvent(e)

    def leaveEvent(self, e):
        # 移出窗口/移到可穿透的空白处：无条件恢复箭头，不去读 QCursor.pos（可能有延迟）
        self._set_cursor_shape(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(e)

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
                self.pomodoro_next_if_ready()      # 番茄钟阶段结束的提醒被点掉 → 接下一阶段
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

    def toggle_visible(self, *_):
        """托盘双击：隐藏 ↔ 显示。隐藏不退出进程，并停掉动画定时器省 CPU。"""
        if self.isVisible():
            self._set_cursor_shape(Qt.CursorShape.ArrowCursor)   # 隐藏前复位，避免"手型卡住"
            self.hide()
            self.timer.stop()
            log("tray_hide")
        else:
            self.show()
            self.raise_()
            self._apply_mask(force=True)      # 重新显示后掩码重建一次，穿透不失效
            self.timer.start(max(16, round(1000 / self.fps)))
            log("tray_show")

    def open_workshop(self, clip="", pack_id="", origin_state="", new_theme=False):
        """启动素材工坊（可选 --clip/--pack/--new-theme 定位）：独立进程，失败不静默。

        cwd 固定为 deskpet-python\，用相对名 workshop.bat：路径含空格也不用再加引号，
        也不会因为从别处启动而找不到 uv/脚本。
        """
        full = os.path.join(ROOT, "workshop.bat")
        if not os.path.exists(full):
            log("workshop_launch missing=%s（workshop.bat 不在 deskpet-python 根目录）" % full)
            self.notify("找不到 workshop.bat：%s" % full)
            return 0
        args = ["/c", "workshop.bat"]
        if pack_id:
            args += ["--pack", pack_id]
        if clip:
            args += ["--clip", clip]
        if origin_state:
            args += ["--state", origin_state]
        if new_theme:
            args += ["--new-theme"]
        try:
            res = QProcess.startDetached("cmd", args, ROOT)
            ok, pid = res if isinstance(res, tuple) else (bool(res), 0)
        except Exception as e:                     # noqa: BLE001 - 启动失败必须落到日志
            log("workshop_launch exception=%r" % (e,))
            ok, pid = False, 0
        if ok:
            log("workshop_launch ok pid=%s cwd=%s clip=%s pack=%s cmd=cmd %s"
                % (pid, ROOT, clip or "-", pack_id or "-", " ".join(args)))
            QTimer.singleShot(2500, lambda: self._check_workshop(pid))
        else:
            log("workshop_launch FAILED cwd=%s exists=%s" % (ROOT, os.path.exists(full)))
            self.notify("素材工坊启动失败：命令未执行（详见 --log 输出 / 控制台）")
        return pid if ok else 0

    def _check_workshop(self, pid):
        """启动 2.5s 后复查：cmd 还在（它同步等 bat）说明工坊在跑；已退出就提示。

        注意：Windows 上不能用 os.kill(pid, 0) 探活——CPython 会走 TerminateProcess，
        传 0 等于直接杀掉目标进程。这里用 OpenProcess 只查询不修改。
        """
        alive = _pid_alive(pid)
        log("workshop_launch check pid=%s alive=%s" % (pid, alive))
        if not alive:
            self.notify("素材工坊没能启动（cmd 已退出）。请手动运行 deskpet-python\\workshop.bat 查看报错。")

    def notify(self, msg):
        """有托盘就发气泡，没有就messagebox；保证失败原因一定能被看到。"""
        tray = getattr(self, "tray", None)
        if tray is not None and QSystemTrayIcon.supportsMessages():
            tray.showMessage("智灵 · 桌宠", msg, QSystemTrayIcon.MessageIcon.Warning, 5000)
        else:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "智灵 · 桌宠", msg)

    def actions_menu(self, parent):
        """一级「动作 ▸」，组内按系列再分一层；名字过长截断，tooltip 给完整名+clipId。

        菜单宽度上限：显示名最多 8 个字符（约 110px + 缩进），远小于半屏宽度。
        """
        m = QMenu("动作", parent)
        m.setToolTipsVisible(True)
        used = set()
        for gname, ids in ACTION_GROUPS:
            items = [c for c in ids if c in CLIPS and c in ACTIONS]
            if not items:
                continue
            gm = m.addMenu(gname)
            gm.setToolTipsVisible(True)
            for cid in items:
                used.add(cid)
                full = CLIPS[cid]["displayName"]
                label = full if len(full) <= 8 else full[:7] + "…"
                a = QAction(label, gm)
                a.setToolTip("%s（%s）" % (full, cid))
                a.triggered.connect(lambda _=False, c=cid: self.play_action(c))
                gm.addAction(a)
        rest = [c for c in ACTIONS if c not in used]
        if rest:
            gm = m.addMenu("其他")
            gm.setToolTipsVisible(True)
            for cid in rest:
                full = CLIPS[cid]["displayName"]
                label = full if len(full) <= 8 else full[:7] + "…"
                a = QAction(label, gm)
                a.setToolTip("%s（%s）" % (full, cid))
                a.triggered.connect(lambda _=False, c=cid: self.play_action(c))
                gm.addAction(a)
        return m

    def size_menu(self, parent):
        m = QMenu("尺寸", parent)
        grp = QActionGroup(m)
        grp.setExclusive(True)
        self.tier_actions = []          # 菜单可重建（切系列）：每次重建重新收集，避免重复项
        for name in TIERS:
            a = QAction(name, m)
            a.setCheckable(True)
            a.setData(name)
            a.setChecked(name == self.tier_name)
            a.triggered.connect(lambda _=False, n=name: self.set_tier(n))
            grp.addAction(a)
            m.addAction(a)
            self.tier_actions.append(a)
        return m

    def theme_menu(self, parent):
        """「主题 ▸」：与设置窗共用一个切换入口，当前项打勾、互斥。"""
        m = QMenu("主题", parent)
        m.setToolTipsVisible(True)
        grp = QActionGroup(m)
        grp.setExclusive(True)
        for t in packlib.list_themes():
            a = QAction(t["name"], m)
            a.setCheckable(True)
            a.setData(t["id"])
            a.setChecked(t["id"] == PACK_ID)
            a.setToolTip("%s · %d/%d 行状态有动画"
                         % (t["id"], t["bound_count"], t["state_count"]))
            a.triggered.connect(lambda _=False, tid=t["id"]: self.request_switch(tid))
            grp.addAction(a)
            m.addAction(a)
        return m

    def request_switch(self, tid):
        """设置窗与右键菜单唯一共用的切换入口：主题 active 标记是唯一真相。"""
        if not tid or tid == PACK_ID:
            return
        packlib.set_active_theme(tid)
        self.apply_pack(tid, reason="theme-switch")

    def _build_menu(self):
        """桌宠右键：只保留「尺寸」切换与「隐藏」（隐藏到托盘，不退出进程）。"""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addMenu(self.size_menu(menu))
        menu.addSeparator()
        hide_act = QAction("隐藏", self)
        hide_act.setToolTip("隐藏到系统托盘，进程继续运行（托盘里可再「显示 / 隐藏」或「退出」）")
        hide_act.triggered.connect(self.hide_to_tray)
        menu.addAction(hide_act)
        return menu

    def hide_to_tray(self, *_):
        """右键「隐藏」：只隐藏窗口并停动画定时器，进程继续运行。

        与托盘「显示 / 隐藏」的隐藏分支同逻辑，但不做切换（已在显示态才隐藏）。
        """
        if self.isVisible():
            self._set_cursor_shape(Qt.CursorShape.ArrowCursor)   # 隐藏前复位，避免"手型卡住"
            self.hide()
            self.timer.stop()
            log("tray_hide (右键隐藏)")

    def contextMenuEvent(self, e):
        if self.sm is not None:
            self.sm.clear_reminder("右键")
        self._set_cursor_shape(Qt.CursorShape.ArrowCursor)   # 菜单期间恢复箭头
        self._build_menu().exec(e.globalPos())
        self._sync_cursor()                                  # 菜单关掉后按指针位置重新判一次

    def apply_pack(self, pid, reason=""):
        """v2 整套切换主题：主题索引 + idle 段 + 显示画布 + 状态映射，托盘菜单重建。

        动画是全局资产，不需要 v1 的「保留跨系列已登记段」；切主题只换绑定表。
        """
        global CLIPS, ACTIONS, IDLE_ID
        if not pid:
            return False
        old = PACK_ID
        _set_pack(pid)
        packlib.invalidate_cache()
        CLIPS.clear()
        cur, ACTIONS = load_clips(pid)
        CLIPS.update(cur)
        if IDLE_ID not in CLIPS:
            log("切主题失败：%s 没有可用 idle 段" % pid)
            return False
        cw, ch, _src = pack_canvas(IDLE_ID)
        right, bottom = self.x() + self.width(), self.y() + self.height()
        self.canvas_wh = (cw, ch)
        self.canvas = (cw, ch)
        self.resize(self.width_px, self._size()[1])
        self.move(right - self.width(), bottom - self.height())
        self.sm = StateMap(STATE_MAP_PATH, light_clips(pid),
                           self.state_play, self.state_play_idle, log)
        self.sm.hold_reminders = self.hold_reminders
        self.sm.playing = IDLE_ID
        self.sm.set_state("idle", "切主题")
        self.clip = IDLE_ID
        self.idx = 0
        self.cur_loop = True
        self.on_clip_end = None
        self.fps = CLIPS[IDLE_ID]["fps"] or self.fps
        self.timer.setInterval(max(16, round(1000 / self.fps)))
        self.loader.reset(self._size())
        self.mask_cache.clear()
        self.mask_applied_key = None
        self.current_img = self._load_now(IDLE_ID, 0)
        self._prefetch()
        self._apply_mask(force=True)
        self.update()
        builder = getattr(self, "tray_menu_builder", None)
        if callable(builder):
            try:
                builder()
            except Exception as e:                   # noqa: BLE001
                log("tray_menu rebuild 失败: %r" % (e,))
        log("theme_switch %s → %s（%s）%d 段 canvas=%dx%d idle=%s"
            % (old, pid, reason or "-", len(CLIPS), cw, ch, IDLE_ID))
        return True

    def open_settings(self):
        """非模态：设置窗开着时桌宠照样能拖（exec() 的嵌套事件循环会冻结全应用输入）。"""
        self._set_cursor_shape(Qt.CursorShape.ArrowCursor)
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        dlg = SettingsDialog(self)
        self._settings_dlg = dlg     # 非模态必须强引用，否则 Python GC 会立刻把窗关掉
        dlg.show()
        self._sync_cursor()

    def set_tier(self, name):
        if name not in TIERS:
            return
        self.tier_name = name
        save_config({"tier": name})
        self._resize_to(max(140, int(QApplication.primaryScreen().availableGeometry().width()
                                     * TIERS[name])))
        for a in getattr(self, "tier_actions", []):
            if a.data() == name:
                a.setChecked(True)
        self.update()

    # ---- 番茄钟 ---------------------------------------------------------------
    def _pomo_apply_state(self, phase, why):
        """阶段 → 状态键：focus / focus_break；主题没绑对应段时回落 busy / idle。

        「没绑」只认 clipIds 为空（fallbackClipIds 也空），保证用户不必为番茄钟造新素材。
        """
        if not phase or self.sm is None:
            return None
        primary, fallback = ((Pomodoro.FOCUS, "busy") if phase == Pomodoro.FOCUS
                             else ("focus_break", "idle"))
        rule = (self.sm.rules or {}).get(primary) or {}
        has_clips = bool(rule.get("clipIds"))
        target = primary if has_clips else fallback
        self._pomo_state = target
        self.sm.set_state(target, why, force=True)
        log("pomodoro_state %s→%s（%s，主题绑定=%s）" % (phase, target, why, has_clips))
        return target

    def _pomo_clear_state(self, why):
        """停止时只清掉番茄钟自己打的状态，绝不覆盖提醒态 / DSH 状态。"""
        st = getattr(self, "_pomo_state", None)
        self._pomo_state = None
        if self.sm is not None and st and self.sm.state == st:
            self.sm.set_state("idle", why, force=True)

    def _pomo_reassert(self):
        """计时中若被单击/长空闲等临时状态顶掉，1s 内把专注/休息画面拉回来。"""
        p = self.pomodoro
        if not p.running or not p.phase or self.sm is None:
            return
        if self.sm.state in ("idle", "long_idle_1", "long_idle_2", "click", "double_click"):
            self._pomo_apply_state(p.phase, "番茄钟续播")

    def pomodoro_start(self):
        if not self.pomodoro.start():
            return
        self._pomo_apply_state(self.pomodoro.phase, "番茄钟开始")
        self._pomo_refresh_ui()
        log("pomodoro_start phase=%s remaining=%s"
            % (self.pomodoro.phase, self.pomodoro.remaining_text()))

    def pomodoro_pause(self):
        if self.pomodoro.pause():
            self._pomo_refresh_ui()
            log("pomodoro_pause phase=%s remaining=%s"
                % (self.pomodoro.phase, self.pomodoro.remaining_text()))

    def pomodoro_stop(self):
        was = self.pomodoro.stop()
        self._pomo_clear_state("番茄钟停止")
        self._pomo_refresh_ui()
        log("pomodoro_stop phase=%s" % (was or "-"))

    def pomodoro_tick(self):
        """1s 心跳：只做「到点判定 + UI 刷新」，剩余时间由单调时钟现算。"""
        ev = self.pomodoro.tick()
        if ev:
            self._pomo_on_end(ev.split(":", 1)[1])
        self._pomo_reassert()
        self._pomo_refresh_ui()

    def _pomo_on_end(self, finished):
        """阶段结束：复用提醒态常驻机制（done），用户点击桌宠即可解除。"""
        if finished == Pomodoro.FOCUS:
            msg = "专注结束，休息一下（%d 分钟）" % self.pomodoro.break_min
        else:
            msg = "休息结束，准备下一轮专注（%d 分钟）" % self.pomodoro.work_min
        if self.sm is not None:
            self.sm.set_state("done", "番茄钟阶段结束", force=True)
        log("pomodoro_phase_end %s next=%s" % (finished, self.pomodoro.phase or "-"))
        # 有托盘就发气泡；无托盘（离屏 / 托盘不可用）只记日志，绝不弹模态框卡住计时
        tray = getattr(self, "tray", None)
        if tray is not None and QSystemTrayIcon.supportsMessages():
            try:
                tray.showMessage("智灵 · 桌宠", msg, QSystemTrayIcon.MessageIcon.Information, 5000)
            except Exception as e:                   # noqa: BLE001 - 气泡失败不能影响计时
                log("pomodoro_notify failed: %r" % (e,))
        else:
            log("pomodoro_notify skipped（无托盘）: %s" % msg)

    def pomodoro_next_if_ready(self):
        """提醒被点掉后，若下一阶段处于「待开始」，直接开始它（与 Tauri 的 pomodoro_next 一致）。"""
        p = self.pomodoro
        if p.enabled and p.phase and not p.running and not p.started:
            self.pomodoro_start()

    def _pomo_refresh_ui(self):
        """托盘菜单文案 / tooltip 每秒刷新一次（不重建菜单，避免闪烁）。"""
        p = self.pomodoro
        refs = getattr(self, "tray_pomo", None)
        if refs:
            title = "番茄钟"
            if p.phase:
                title = "番茄钟 · %s %s%s" % (
                    p.phase_cn(), p.remaining_text(),
                    "" if p.running else ("（暂停）" if p.started else "（待开始）"))
            refs["menu"].setTitle(title)
            refs["start"].setText(p.start_label())
            refs["start"].setEnabled(bool(p.enabled) and not p.running)
            refs["pause"].setEnabled(bool(p.running))
            refs["stop"].setEnabled(bool(p.phase))
        tray = getattr(self, "tray", None)
        if tray is not None:
            tip = "智灵 · 桌宠"
            if p.enabled and p.phase:
                tip += " · " + p.status_text()
            tray.setToolTip(tip)

    def apply_pomodoro_config(self, cfg):
        """设置保存后热生效：总开关 / 时长（config.json 结构与 Tauri 一致）。

        config.json:
          "pomodoro": { "enabled": true, "workMin": 25, "breakMin": 5 }
        """
        p = self.pomodoro
        pomo = cfg.get("pomodoro") if isinstance(cfg.get("pomodoro"), dict) else {}

        def _int(key, dflt):
            try:
                return int(pomo.get(key, dflt))
            except (TypeError, ValueError):
                return dflt

        work = max(1, min(180, _int("workMin", 25)))
        brk = max(1, min(60, _int("breakMin", 5)))
        was_enabled = p.enabled
        p.configure(enabled=pomo.get("enabled", True) is not False,
                    work_min=work, break_min=brk)
        if not p.enabled:
            self._pomo_clear_state("番茄钟关闭")
        self._pomo_refresh_ui()
        builder = getattr(self, "tray_menu_builder", None)
        if was_enabled != p.enabled and callable(builder):
            try:
                builder()                            # 开关变了：重画托盘（子菜单出现/消失）
            except Exception as e:                   # noqa: BLE001
                log("tray_menu rebuild 失败: %r" % (e,))
        log("pomodoro_config enabled=%s work=%d break=%d"
            % (p.enabled, p.work_min, p.break_min))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            QApplication.quit()


class StateIn:
    """外部状态输入（零依赖）：本地 HTTP 或 JSON 文件 → 主线程切状态。

    选型说明：不做命名管道——HTTP 一行命令就能验证（PowerShell / Node / curl 都行），
    只绑 127.0.0.1 不对外；再给 --state-file 轮询，覆盖完全不想开端口的 DSH。
    线程只负责收包，真正切状态放在主线程的 QTimer 里（Qt 控件不能跨线程碰）。

    协议：POST /state  body={"state":"streaming","priority":30,"timeoutSec":10}
    状态名必须是主题映射 themes/<id>.json 里已有规则的键（idle/streaming/needs_decision/…）。
    """
    def __init__(self, pet, port=0, path=""):
        self.pet = pet
        self.port = int(port or 0)
        self.path = path or ""
        self.q = queue.Queue()
        self.sock = None
        self._file_sig = None
        self._timer = QTimer(pet)
        self._timer.timeout.connect(self.pump)

    def start(self):
        if self.port:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", self.port))
                s.listen(8)
                s.settimeout(0.5)
            except OSError as e:
                log("state_in=http 不可用（127.0.0.1:%d 绑定失败，端口可能被占用: %s）；"
                    "可用 --state-port 换一个端口，或改用 --state-file"
                    % (self.port, e))
            else:
                self.sock = s
                threading.Thread(target=self._http_loop, daemon=True).start()
                log("state_in=http 已监听 http://127.0.0.1:%d/state" % self.port)
        if self.path:
            log("state_in=file 轮询 %s（500ms，内容变化才应用）" % self.path)
        self._timer.start(100)

    # ---- HTTP 线程 --------------------------------------------------------
    def _http_loop(self):
        while self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(conn)
            except Exception as e:                 # noqa: BLE001 - 单个请求坏了不影响服务
                log("state_in=http 请求异常: %s" % e)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn):
        conn.settimeout(2)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 16384:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        parts = lines[0].decode("latin-1").split() if lines else []
        method = parts[0] if parts else ""
        path = (parts[1].split("?")[0] if len(parts) > 1 else "").rstrip("/")
        clen = 0
        for ln in lines[1:]:
            k, _, v = ln.partition(b":")
            if k.strip().lower() == b"content-length":
                try:
                    clen = int(v.strip() or 0)
                except ValueError:
                    clen = 0
        body = rest
        while len(body) < clen:
            chunk = conn.recv(4096)
            if not chunk:
                break
            body += chunk

        if method == "POST" and path == "/state":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except ValueError as e:
                self._reply(conn, 400, {"ok": False, "error": "bad json: %s" % e})
                return
            self.q.put(("http", payload))
            self._reply(conn, 200, {"ok": True})
        elif method in ("GET", "POST") and path == "/state":
            sm = self.pet.sm
            self._reply(conn, 200, {"state": getattr(sm, "state", None),
                                    "playing": getattr(sm, "playing", None)})
        else:
            self._reply(conn, 404, {"ok": False, "error": "use POST /state"})

    @staticmethod
    def _reply(conn, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(code, "OK")
        head = ("HTTP/1.1 %d %s\r\nContent-Type: application/json; charset=utf-8\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (code, reason, len(body))).encode("latin-1")
        conn.sendall(head + body)

    # ---- 主线程：收队列 + 轮询文件 ----------------------------------------
    def pump(self):
        while True:
            try:
                src, payload = self.q.get_nowait()
            except queue.Empty:
                break
            self._apply(src, payload)
        if self.path:
            self._poll_file()

    def _poll_file(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._file_sig:
            return
        self._file_sig = sig
        try:
            text = open(self.path, encoding="utf-8-sig").read().strip()
        except OSError as e:
            log("state_in=file 读取失败: %s" % e)
            return
        if text:
            self._apply("file", text)

    def _apply(self, src, payload):
        if isinstance(payload, str):
            s = payload.strip()
            if not s:
                return
            try:
                payload = json.loads(s)
            except ValueError:
                payload = {"state": s}          # 纯文本 "streaming" 也认
        if not isinstance(payload, dict):
            log("state_in[%s] 忽略：不是 JSON 对象" % src)
            return
        st = payload.get("state")
        sm = self.pet.sm
        if sm is None:
            log("state_in[%s] 状态机还没就绪，丢弃 state=%r" % (src, st))
            return
        ok = sm.external(st, payload)
        log("state_in[%s] recv state=%r prio=%s ttl=%s ok=%s playing=%s"
            % (src, st, payload.get("priority"), payload.get("timeoutSec"),
               ok, sm.playing))


THUMB_ROW_PX = 40          # 左栏行内缩略图
PLAYER_CACHE_PX = 480      # 播放器帧缓存长边：只缓存当前段，按这个尺寸解码


class BridgeAuto:
    """桌宠自带的 DSH 桥托管。

    --state-port 默认 8766（启动即自动桥接）：若 55471 在监听（插件已跑）且还没有别的桥连着，
    就自动拉起 build/tools/dsh_bridge.py；子进程随桌宠退出一起清理，
    桥自身再用 --parent-pid 兜底，桌宠被强杀也不会留下孤儿。
    """
    WS_PORT = DEFAULT_DSH_WS_PORT

    def __init__(self, pet_port):
        self.pet_port = int(pet_port)
        self.proc = None
        self._timer = None
        self._skip_logged = False

    def start_watch(self):
        """先试一次；没就绪就每 5 秒再看，直到 DSH 插件起来或桌宠退出。"""
        self.maybe_start()
        if self.proc is not None:
            return
        self._timer = QTimer()
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        log("bridge=auto watch 每 5s 检查一次 %d" % self.WS_PORT)

    def _tick(self):
        if self.proc is not None:
            self._timer.stop()
            return
        self.maybe_start()
        if self.proc is not None:
            self._timer.stop()

    @staticmethod
    def _listening(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False

    @staticmethod
    def _health():
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % BridgeAuto.WS_PORT, timeout=1.5) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except (OSError, ValueError):
            return None

    def maybe_start(self):
        if not self._listening(self.WS_PORT):
            if not self._skip_logged:
                log("bridge=auto skip（%d 未监听：DSH 里插件没加载/没跑，稍后每 5s 复查）"
                    % self.WS_PORT)
                self._skip_logged = True
            return
        h = self._health()
        if h and int(h.get("sockets") or 0) > 0:
            log("bridge=auto skip（已有桥连着，sockets=%s）" % h.get("sockets"))
            return
        script = os.path.join(ROOT, "build", "tools", "dsh_bridge.py")
        if not os.path.exists(script):
            log("bridge=auto skip（找不到 %s）" % script)
            return
        cmd = [sys.executable, script, "--pet-port", str(self.pet_port),
               "--ws-port", str(self.WS_PORT), "--verbose",
               "--parent-pid", str(os.getpid())]
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log("bridge=auto started pid=%d（日志 build/cache/dsh_bridge.log）" % self.proc.pid)
        except OSError as e:
            log("bridge=auto 启动失败: %s" % e)

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
            log("bridge=auto stopped pid=%d" % self.proc.pid)
        except Exception as e:                     # noqa: BLE001 - 退出路径不报错
            log("bridge=auto stop: %s" % e)


class ActivePackWatcher:
    """监听 themes/*.json 的 active 标记（外加可选的 active.json 镜像）：切主题不重启就换装。

    v2 把启用状态搬进主题文件后，切换会写 2~3 个文件，所以签名必须是「全部文件的
    mtime_ns+size 字典」而不是单个文件；中途看到两个 active=true 也没关系，
    packlib 会按 mtime 裁定唯一赢家。--pack 显式指定的会话不实例化本类。
    """

    def __init__(self, pet, interval_ms=1000):
        self.pet = pet
        self._sig = self._stat()
        self.timer = QTimer(pet)
        self.timer.timeout.connect(self.poll)
        self.timer.start(max(500, int(interval_ms)))

    def _stat(self):
        paths = sorted(glob.glob(os.path.join(packlib.themes_dir(), "*.json")))
        if os.path.exists(packlib.active_json_path()):
            paths.append(packlib.active_json_path())      # 兼容镜像：外部工具改它也触发一次
        sig = {}
        for p in paths:
            try:
                st = os.stat(p)
                sig[p] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        return sig

    def poll(self):
        sig = self._stat()
        if not sig or sig == self._sig:
            return
        self._sig = sig
        tid = packlib.active_theme()
        if tid and tid != PACK_ID:
            log("主题 active 标记变化：切换主题 %s → %s" % (PACK_ID, tid))
            self.pet.apply_pack(tid, reason="theme-active")


def _decode_frame(path, tgt):
    """单帧解码：让解码器直接按缓存尺寸出小图（比「原图解码+缩放」快约 40%）。

    返回 QPixmap；失败返回 None。QPixmap 必须在 GUI 线程创建，所以后台线程只产
    QImage、由 ClipPlayer 在主线程转 QPixmap（见 _on_loaded）。
    """
    r = QImageReader(path)
    sz = r.size()
    if not sz.isValid():
        return None
    s = min(tgt[0] / sz.width(), tgt[1] / sz.height())
    r.setScaledSize(QSize(max(1, int(sz.width() * s)), max(1, int(sz.height() * s))))
    img = r.read()
    if img.isNull():
        return None
    return QPixmap.fromImage(img)


class _FrameLoader(QThread):
    """后台逐帧解码：第 0 帧由 ClipPlayer 同步出图（立即预览），其余帧丢这里。

    gen 是「当前片段代号」：切段时主线程 +1，旧线程的回传会被 gen 校验丢弃，
    不会出现「切到 B 段却把 A 段残留帧塞进 frames」的错位。
    """

    frameReady = pyqtSignal(object, int)
    finished = pyqtSignal(int)

    def __init__(self, paths, tgt, gen, start=1):
        super().__init__()
        self.paths = paths
        self.tgt = tgt
        self.gen = gen
        self.start_idx = start

    def run(self):
        for idx in range(self.start_idx, len(self.paths)):
            r = QImageReader(self.paths[idx])
            sz = r.size()
            if not sz.isValid():
                continue
            s = min(self.tgt[0] / sz.width(), self.tgt[1] / sz.height())
            r.setScaledSize(QSize(max(1, int(sz.width() * s)), max(1, int(sz.height() * s))))
            img = r.read()
            if img.isNull():
                continue
            self.frameReady.emit(img, idx)
        self.finished.emit(len(self.paths))


class ClipPlayer(QWidget):
    """单段动画播放器：从 frames/<clipId>/ 顺序读帧，按 manifest fps 用 QTimer 循环播放。

    只缓存「当前选中段」的帧，切段时先 stop() 再清空缓存，避免十几段一起进内存。
    帧在载入时缩到 ≤480 长边（原帧 576×530，按显示区域够用，内存约为原图 70%）。

    性能：第 0 帧同步解码（瞬时，马上能预览），其余帧交给后台线程逐张补齐——
    所以设置窗里点动作池切段，UI 不再被「一次性 decode 几十张 webp」卡住。
    """

    frameChanged = pyqtSignal(int, int)     # 当前帧序号（0 基）, 总帧数

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frames = []
        self.clip_id = ""
        self.frame_index = 0
        self.fps = 24
        self.missing = ""
        self.total = 0
        self.stats = {"load_ms": 0.0, "mem_mb": 0.0, "frames": 0}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._gen = 0
        self._worker = None
        # 最小高度别定太高：设置窗右栏是竖直堆叠，240 会把内容最小高度顶到
        # 超过窗口 maximumSize，Qt 就会把预览窗下半截直接裁掉（实测 692 > 680）。
        self.setMinimumSize(200, 160)

    def sizeHint(self):
        return QSize(320, 240)

    def minimumSizeHint(self):
        return QSize(200, 160)

    def cache_target(self):
        long_edge = max(240, min(PLAYER_CACHE_PX, max(self.width(), self.height())))
        return long_edge, long_edge

    def _stop_worker(self):
        w = self._worker
        if w is None:
            return
        self._worker = None
        try:
            w.frameReady.disconnect()
            w.finished.disconnect()
        except Exception:                      # noqa: BLE001 - 断开失败不必 abort
            pass
        w.quit()
        if w.isRunning():
            w.wait(800)

    def release(self):
        """释放当前段缓存（切段/关窗时调用）。"""
        self._stop_worker()
        self.timer.stop()
        self.frames = []
        self.clip_id = ""
        self.frame_index = 0
        self.total = 0
        self.missing = ""

    def load(self, cid, fps=24):
        t0 = time.monotonic()
        self.release()
        self.clip_id = cid
        self.fps = max(1, int(fps or 24))
        d = frames_dir(CLIPS[cid]) if cid in CLIPS else ""
        paths = sorted(glob.glob(os.path.join(d, "f*.webp")) +
                       glob.glob(os.path.join(d, "f*.png"))) if d else []
        self.total = len(paths)
        if not paths:
            self.missing = d or "packs/%s/frames/%s（未在 clips.json 登记）" % (PACK_ID, cid)
            self.stats = {"load_ms": (time.monotonic() - t0) * 1000, "mem_mb": 0.0, "frames": 0}
            self.update()
            self.frameChanged.emit(0, 0)
            return self.stats
        tgt = self.cache_target()
        # 第 0 帧同步出图，立即能预览；其余帧后台补齐，切段不再卡 UI
        first = _decode_frame(paths[0], tgt)
        self.frames = [first] if first is not None else []
        self.frame_index = 0
        if self.frames:
            self.timer.start(max(10, int(round(1000.0 / self.fps))))
        self.update()
        self.frameChanged.emit(0, self.total)
        self._gen += 1
        gen = self._gen
        self._worker = _FrameLoader(paths, tgt, gen, start=1)
        self._worker.frameReady.connect(lambda img, idx: self._on_loaded(img, idx, gen))
        self._worker.finished.connect(lambda total: self._on_done(total, gen))
        self._worker.start()
        self.stats = {"load_ms": (time.monotonic() - t0) * 1000,
                      "mem_mb": self._mem(),
                      "frames": self.total}
        return self.stats

    def _mem(self):
        return sum(f.width() * f.height() * 4 for f in self.frames) / 1e6

    def _on_loaded(self, img, idx, gen):
        if gen != self._gen or not self.clip_id:
            return
        pm = QPixmap.fromImage(img)
        if pm.isNull():
            return
        self.frames.append(pm)
        self.update()

    def _on_done(self, total, gen):
        if gen != self._gen:
            return
        self.stats = {"load_ms": self.stats.get("load_ms", 0.0),
                      "mem_mb": self._mem(), "frames": total}

    def _tick(self):
        if not self.frames:
            return
        self.frame_index = (self.frame_index + 1) % len(self.frames)
        self.update()
        self.frameChanged.emit(self.frame_index, self.total)

    def playing(self):
        return self.timer.isActive()

    def play(self):
        if self.frames and not self.timer.isActive():
            self.timer.start(max(10, int(round(1000.0 / self.fps))))

    def pause(self):
        self.timer.stop()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(UI["preview_bg"]))
        step = 16                      # 棋盘格：透明区域一眼可辨，颜色与其它面板同源
        for y in range(0, self.height(), step):
            for x in range(0, self.width(), step):
                if (x // step + y // step) % 2 == 0:
                    p.fillRect(x, y, step, step, QColor(UI["preview_alt"]))
        if self.missing or not self.frames:
            p.setPen(QColor(UI["muted"]))
            p.drawText(self.rect().adjusted(12, 12, -12, -12),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       "该段无可用帧\n%s" % (self.missing or "（帧目录为空）"))
            return
        pm = self.frames[min(self.frame_index, len(self.frames) - 1)]
        # 四周留 6px 呼吸：角色贴着预览窗下边界时，视觉上很像是「被截掉了下半身」
        area = self.rect().adjusted(6, 6, -6, -6)
        s = min(area.width() / pm.width(), area.height() / pm.height())
        rw, rh = max(1, int(pm.width() * s)), max(1, int(pm.height() * s))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawPixmap(area.x() + (area.width() - rw) // 2,
                     area.y() + (area.height() - rh) // 2, rw, rh, pm)


class PairDelegate(QStyledItemDelegate):
    """左栏一行：40×40 缩略图 + 状态名 + 动画名（次行小字）。

    QListWidget 自带文字只能单行单字号，所以用 delegate 自己画两行。
    没有「启用」勾选框：动作池为空即该状态不绑定（到达时保持当前画面）。
    """

    def sizeHint(self, option, index):
        return QSize(0, 62)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""                  # 只让样式画选中/悬停背景，文字自己画
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        txt_col = option.palette.highlightedText().color() if selected else option.palette.text().color()
        # 缩略图
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None:
            icon.paint(painter, QRect(rect.left() + 8, rect.top() + (rect.height() - THUMB_ROW_PX) // 2,
                                      THUMB_ROW_PX, THUMB_ROW_PX))
        # 两行文字
        tx = rect.left() + 8 + THUMB_ROW_PX + 8
        title = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        sub = str(index.data(Qt.ItemDataRole.UserRole + 1) or "")
        f = QFont(option.font)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(txt_col)
        tw = rect.width() - tx - 12
        painter.drawText(QRect(tx, rect.top() + 8, tw, 22),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, tw))
        f2 = QFont(option.font)
        f2.setPointSize(max(7, option.font.pointSize() - 1))
        painter.setFont(f2)
        painter.setPen(QColor(UI["select_text"] if selected else UI["muted"]))
        painter.drawText(QRect(tx, rect.top() + 30, tw, 20),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(sub, Qt.TextElideMode.ElideRight, tw))


class PairList(QListWidget):
    """左栏列表：保留 InternalMove 拖拽排序（顺序即优先级）。"""


class AssetPicker(QDialog):
    """全局动作库：扫 animations/*/meta.json，不配缩略图（点行看信息，点「加入」绑定）。

    来源范围可过滤「全部 / 仅当前主题已用 / 其它主题来源」；每行标出画布尺寸，
    因为 B 方案下比例不同的段切换时会 resize 一下，用户挑的时候要看得见。
    """

    def __init__(self, parent, theme_id):
        super().__init__(parent)
        self.setWindowTitle("从全局动作库挑动画")
        self.resize(600, 520)
        lay = QVBoxLayout(self)
        frow = QHBoxLayout()
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("按名称 / assetId 过滤")
        frow.addWidget(QLabel("过滤："))
        frow.addWidget(self.ed_filter, 1)
        self.cb_scope = QComboBox()
        self.cb_scope.addItems(["全部", "仅本主题已用", "其它主题来源"])
        frow.addWidget(self.cb_scope)
        lay.addLayout(frow)
        self.lst = QListWidget()
        self.lst.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self.lst, 1)
        self.lb_info = QLabel("—")
        self.lb_info.setStyleSheet("color:%s" % UI["muted"])
        lay.addWidget(self.lb_info)
        brow = QHBoxLayout()
        brow.addStretch(1)
        b_cancel = QPushButton("取消")
        b_ok = QPushButton("加入动作池")
        b_ok.setDefault(True)
        brow.addWidget(b_cancel)
        brow.addWidget(b_ok)
        lay.addLayout(brow)
        b_cancel.clicked.connect(self.reject)
        b_ok.clicked.connect(self.accept)
        self.ed_filter.textChanged.connect(self._fill)
        self.cb_scope.currentIndexChanged.connect(self._fill)
        self.lst.itemSelectionChanged.connect(self._info)
        self._all = packlib.list_assets()
        self._used = set()
        for r in (packlib.load_theme(theme_id).get("states") or {}).values():
            if isinstance(r, dict):
                for c in (r.get("clipIds") or []):
                    self._used.add(c)
        self._fill()

    def _fill(self):
        q = self.ed_filter.text().strip().lower()
        scope = self.cb_scope.currentIndex()
        self.lst.clear()
        for aid, m in sorted(self._all.items()):
            if q and q not in ((aid + " " + (m.get("name") or "")).lower()):
                continue
            used = aid in self._used
            if scope == 1 and not used:
                continue
            if scope == 2 and used:
                continue
            w, h = packlib.aspect_pair(m.get("canvas"))
            origin = (m.get("origin") or {}).get("pack") or "-"
            label = "%s   ·   %d×%d   ·   %s%s" % (
                m.get("name") or aid, w, h, origin, "   [本主题]" if used else "")
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, aid)
            self.lst.addItem(it)
        self._info()

    def _info(self):
        sel = self.lst.selectedItems()
        if not sel:
            self.lb_info.setText("共 %d 段 · 选中 0 段" % self.lst.count())
            return
        aid = sel[0].data(Qt.ItemDataRole.UserRole)
        m = self._all.get(aid) or {}
        self.lb_info.setText("选中 %d 段 · %s · %d 帧 · %d fps · 来源 %s"
                             % (len(sel), aid, m.get("count", 0), m.get("fps", 24),
                                m.get("source") or "-"))

    def chosen(self):
        return [i.data(Qt.ItemDataRole.UserRole) for i in self.lst.selectedItems()]


class BridgeStatus(QObject):
    """DSH 链路探测器：三段（插件 55471 → 桥 dsh_bridge.py → 桌宠 /state）。

    关键约束：socket 连接与 HTTP /health 都有超时，必须在后台线程做；
    结果用 pyqtSignal 排队回主线程，设置窗任何时刻都不会被网络阻塞。
    只在设置窗打开期间每 2s 探测一次，closeEvent 里 stop()。

    /health（dsh-deskpet-bridge/index.js:223）返回：
      ok / plugin / pid / port / uptimeSec / sockets / events / tokens / lastType / lastAt
    sockets > 0 表示有 WS 客户端（桥或 selftest）连着；lastAt 是毫秒 epoch。
    """
    updated = pyqtSignal(dict)

    INTERVAL_MS = 2000
    DEFAULT_WS_PORT = DEFAULT_DSH_WS_PORT     # 端口常量见 shared_constants（与 dsh_bridge / Tauri 共用）

    def __init__(self, pet, on_result, parent=None):
        super().__init__(parent)
        self.pet = pet
        self._on_result = on_result
        self._busy = False
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self.updated.connect(self._deliver)     # 跨线程 emit → 主线程排队执行

    def start(self):
        self.refresh()
        self._timer.start()

    def stop(self):
        self._closed = True
        self._timer.stop()

    def refresh(self):
        """主线程：快照本地状态，网络探测交给后台线程。"""
        if self._busy or self._closed:
            return
        self._busy = True
        state_in = getattr(self.pet, "state_in", None)
        bridge = getattr(self.pet, "bridge", None)
        proc = getattr(bridge, "proc", None)
        snap = {
            "ws_port": int(getattr(bridge, "WS_PORT", self.DEFAULT_WS_PORT)
                           or self.DEFAULT_WS_PORT),
            "pet_port": int(getattr(state_in, "port", 0) or 0),
            "enabled": bool(state_in and getattr(state_in, "port", 0)),
            "file_mode": bool(state_in and getattr(state_in, "path", "")),
            "pet_listening": bool(state_in and getattr(state_in, "sock", None) is not None),
            "proc_alive": bool(proc is not None and proc.poll() is None),
            "proc_pid": int(proc.pid) if proc is not None else 0,
        }

        def work():
            out = dict(snap)
            try:
                with socket.create_connection(("127.0.0.1", snap["ws_port"]), timeout=0.6):
                    out["plugin_up"] = True
            except OSError as e:
                out["plugin_up"] = False
                out["plugin_err"] = str(e)
            health = {}
            if out["plugin_up"]:
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/health" % snap["ws_port"], timeout=1.0) as r:
                        health = json.loads(r.read().decode("utf-8", "ignore"))
                except (OSError, ValueError):
                    health = {}
            out["health"] = health if isinstance(health, dict) else {}
            self.updated.emit(out)

        threading.Thread(target=work, daemon=True, name="dsh-link-probe").start()

    def _deliver(self, out):
        self._busy = False                     # 只复位，不残留后台线程
        if not self._closed:
            self._on_result(out)


class SettingsTabStrip(QWidget):
    """页签条 + 紧跟页签的操作按钮 + 内容栈。

    QTabWidget 的页签条铺满整行，右上角只能放 corner widget，按钮会被挤到最右侧；
    这里用 QTabBar + QStackedWidget 自己拼，按钮真正「紧跟页签、左对齐」。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.head = QHBoxLayout()
        self.head.setContentsMargins(0, 0, 0, 0)
        self.head.setSpacing(6)
        self.bar = QTabBar()
        self.bar.setExpanding(False)          # 页签按文字宽度，不铺满
        self.bar.setDrawBase(False)
        self.head.addWidget(self.bar, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(self.head)
        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)
        self.bar.currentChanged.connect(self.stack.setCurrentIndex)
        self.currentChanged = self.bar.currentChanged

    def addTab(self, widget, title):
        self.bar.addTab(title)
        return self.stack.addWidget(widget)

    def count(self):
        return self.bar.count()

    def tabText(self, i):
        return self.bar.tabText(i)

    def widget(self, i):
        return self.stack.widget(i)

    def currentIndex(self):
        return self.bar.currentIndex()

    def setCurrentIndex(self, i):
        self.bar.setCurrentIndex(i)


class SettingsDialog(QDialog):
    """v2 设置窗（两页）：状态与动画 / 提醒与连接。

    - 系列只在「状态与动画」页的下拉里选（重命名 / 新建入口同样在该页）；
    - 左栏 13 行状态（越靠上越优先，可拖拽/上下移；动作池为空即不绑定）；
    - 排序行左侧是「提醒态常驻」（config.json:remindHold），右侧是上移/下移/恢复默认；不参与排序 / priority；
    - 右栏是选中状态的 clipIds[]，顺序 = 轮播顺序，可增删/排序/清空；
    - 底部「素材与工具」只放素材目录；抽帧依赖随 `uv sync` 装好，不再暴露 ffmpeg 配置；
    - 零绑定是一等公民；idle 是 UI 软约束（不允许清空，手写 JSON 不拦）；
    - 保存写 packs/themes/<id>.json，保留全部调度字段，priority 按列表顺序等距重算。
    """

    def __init__(self, pet, parent=None):
        super().__init__(parent if parent is not None else pet)
        # 只挂在本窗口上：QSS 挂到 QApplication 会把透明桌宠主窗口刷成不透明色块。
        # QSS 不改 QPalette，自定义 delegate 画的字必须靠 dark_palette() 才有深色底。
        self.setStyleSheet(QSS)
        self.setPalette(dark_palette())
        self.pet = pet
        self.theme_id = PACK_ID
        self.changed = False
        self.switched_theme = ""
        self._loading = False
        self._editing_state = None
        self._rows = {}          # state -> rule dict（保留主题里的全部字段）
        self._items = {}
        self.setWindowTitle("智灵 · 桌宠 设置")
        # 非模态：设置窗开着时桌宠仍可拖动（模态会冻结整个应用的鼠标输入）。
        # 保存后的热生效改在 save() 里做，不再依赖 exec() 返回。
        self.setWindowModality(Qt.WindowModality.NonModal)
        avail = QApplication.primaryScreen().availableGeometry()
        # 旧版把 maximumSize 卡在可用高的 0.85 倍，而右栏内容 minimumSizeHint 更高
        # （实测 692 > 680）→ Qt 直接把预览窗下半截裁掉。改成「不超过屏幕」即可，
        # 真正的保底交给末尾的 minimumSizeHint 兜底。
        self.setMaximumSize(avail.width(), avail.height())
        self.setMinimumSize(min(880, int(avail.width() * 0.90)),
                            min(560, int(avail.height() * 0.86)))
        self.resize(min(1120, int(avail.width() * 0.86)),
                    min(760, int(avail.height() * 0.84)))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        # ---- 顶部：页签条在上，按钮紧跟页签、左对齐；内容在下方铺满 ----
        self.tabs = SettingsTabStrip()
        self.b_restore = QPushButton("恢复上次配置")
        self.b_restore.setToolTip("读取本主题的单份备份 themes/<id>.json.bak，回填界面；确认后再点「保存配置」才生效")
        self.b_restore.setAutoDefault(False)       # 次要按钮，回车仍只触发「保存配置」
        self.b_restore.setStyleSheet(               # 描边浅色：不与主按钮抢视觉
            "QPushButton { background:transparent; color:%s; border:1px solid %s;"
            " border-radius:4px; padding:5px 10px; }"
            "QPushButton:hover { background:%s; color:%s; border-color:%s; }"
            "QPushButton:pressed { background:%s; }"
            % (UI["muted"], UI["border"], UI["raised_hover"], UI["text"],
               UI["accent_strong"], UI["select"]))
        self.b_restore.clicked.connect(self._restore_last_save)
        self.tabs.head.addWidget(self.b_restore, 0, Qt.AlignmentFlag.AlignVCenter)
        self.b_save = QPushButton("保存配置")
        self.b_save.setDefault(True)
        self.b_save.setEnabled(False)              # 没有未保存的配置改动时禁用
        self.b_save.clicked.connect(self.save)
        self.tabs.head.addWidget(self.b_save, 0, Qt.AlignmentFlag.AlignVCenter)
        self.b_cancel = QPushButton("取消")
        self.b_cancel.clicked.connect(self.reject)
        self.tabs.head.addWidget(self.b_cancel, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lb_save_status = QLabel("")
        self.lb_save_status.setStyleSheet("color:%s; font-weight:600;" % UI["ok"])
        self.tabs.head.addWidget(self.lb_save_status, 0, Qt.AlignmentFlag.AlignVCenter)
        self.tabs.head.addStretch(1)               # 空白只留右侧，按钮组不飘走
        lay.addWidget(self.tabs, 1)
        cfg = load_config()                      # 两个页签共用一份配置快照

        # ======== 页 1：状态与动画（系列选择 + 13 行状态 × 动作池 + 提醒态常驻）========
        tab_states = QWidget()
        ts = QVBoxLayout(tab_states)
        ts.setContentsMargins(0, 0, 0, 0)   # 统一用对话框的 10px 外边距
        ts.setSpacing(10)

        # 先选系列，再看 / 改该系列的状态↔动画绑定；重命名 / 新建入口也留在这里。
        row_theme = QHBoxLayout()
        row_theme.addWidget(QLabel("当前主题："))
        self.cb_theme = QComboBox()
        self.cb_theme.setMinimumWidth(190)
        self.cb_theme.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cb_theme.currentTextChanged.connect(self.cb_theme.setToolTip)  # 长系列名省略时可看全名
        row_theme.addWidget(self.cb_theme)
        self.b_rename = QPushButton("重命名主题…")
        self.b_rename.setToolTip("只改显示名，不动主题 id、动画资产与状态绑定；立即写盘")
        self.b_rename.clicked.connect(self._rename_theme)
        row_theme.addWidget(self.b_rename)
        self.b_new_theme = QPushButton("新建主题…")
        self.b_new_theme.setToolTip("在素材工坊里新建一张主题（状态→动画的绑定索引），空映射起步")
        self.b_new_theme.clicked.connect(self._new_theme)
        row_theme.addWidget(self.b_new_theme)
        self.lb_theme_info = QLabel("")
        self.lb_theme_info.setStyleSheet("color:%s" % UI["muted"])
        row_theme.addWidget(self.lb_theme_info, 1)
        ts.addLayout(row_theme)

        # 示例素材提示：选中 sample 时可见，引导装的完整包；其它系列自动隐藏。
        row_sample = QHBoxLayout()
        self.lb_sample = QLabel("")
        self.lb_sample.setWordWrap(True)
        self.lb_sample.setStyleSheet("color:%s" % UI["warn"])
        row_sample.addWidget(self.lb_sample, 1)
        self.b_sample_readme = QPushButton("查看安装说明")
        self.b_sample_readme.clicked.connect(self._open_install_readme)
        row_sample.addWidget(self.b_sample_readme)
        ts.addLayout(row_sample)
        self._update_sample_notice()

        body = QHBoxLayout()
        self.list = PairList()
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setSpacing(2)
        self.list.setMinimumWidth(300)
        self.list.setItemDelegate(PairDelegate(self.list))
        self.list.currentItemChanged.connect(self._sync_editor)
        self.list.model().rowsMoved.connect(self._mark_dirty)

        # 左栏 = 状态列表 + 排序行；整组宽度与列表一致（最大 440），不横跨到右栏下方
        left_box = QWidget()
        left = QVBoxLayout(left_box)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        left.addWidget(self.list, 1)
        left_box.setMaximumWidth(440)
        body.addWidget(left_box, 38)

        # 右栏整体是「当前选中状态」的编辑框体；新建动画按钮放在框体内，
        # 保证它跟着左栏选中的状态走，不会看起来像主题级操作。
        self.state_box = QGroupBox("当前状态：—")
        right = QVBoxLayout(self.state_box)

        self.pool = QListWidget()
        self.pool.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.pool.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.pool.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.pool.setIconSize(QSize(28, 28))
        self.pool.setMinimumHeight(100)
        self.pool.setMaximumHeight(132)
        self.pool.currentItemChanged.connect(self._on_pool_selected)
        self.pool.model().rowsMoved.connect(self._renumber_pool)
        # 动作池给固定高度，剩余空间全留给预览窗，别两个 stretch 互相抢
        right.addWidget(self.pool, 0)

        pbtn = QHBoxLayout()
        b_add = QPushButton("从动作库添加…")
        b_add.clicked.connect(self._add_from_library)
        b_rm = QPushButton("移除")
        b_rm.clicked.connect(self._remove_selected)
        b_up = QPushButton("↑")
        b_up.setFixedWidth(34)
        b_up.clicked.connect(lambda: self._pool_move(-1))
        b_dn = QPushButton("↓")
        b_dn.setFixedWidth(34)
        b_dn.clicked.connect(lambda: self._pool_move(1))
        for b in (b_add, b_rm, b_up, b_dn):
            pbtn.addWidget(b)
        pbtn.addStretch(1)
        self.b_new = QPushButton("＋ 用视频做一段新动作")
        self.b_new.setToolTip("带着当前选中的状态进素材工坊；生成完自动绑定回这个状态")
        self.b_new.clicked.connect(self._new_animation)
        pbtn.addWidget(self.b_new)
        right.addLayout(pbtn)

        self.player = ClipPlayer()
        self.player.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.player.frameChanged.connect(self._on_frame)
        right.addWidget(self.player, 1)

        prow = QHBoxLayout()
        self.b_play = QPushButton("暂停")
        self.b_play.setFixedWidth(72)
        self.b_play.clicked.connect(self._toggle_play)
        self.lb_prog = QLabel("第 0 / 0 帧")
        prow.addWidget(self.b_play)
        prow.addWidget(self.lb_prog)
        prow.addStretch(1)
        self.b_ws = QPushButton("在工坊中打开这段")
        self.b_ws.clicked.connect(self._open_in_workshop)
        prow.addWidget(self.b_ws)
        right.addLayout(prow)
        body.addWidget(self.state_box, 62)
        ts.addLayout(body, 1)

        # 排序行：左侧「提醒态常驻」开关，右侧三个排序按钮（同一行，不另起一行）。
        # 常驻开关只是播放策略，不属于状态行，不参与拖拽排序与 priority 重算。
        nav = QHBoxLayout()
        _hold_tip = "完成 / 出错 / 待你决策 未点击时持续播放，不回 idle"
        lb_hold = QLabel("提醒态常驻")
        lb_hold.setToolTip(_hold_tip)
        nav.addWidget(lb_hold)
        self.cb_hold = QCheckBox()
        self.cb_hold.setToolTip(_hold_tip)
        self.cb_hold.setChecked(bool(getattr(pet, "hold_reminders", True)))
        self.cb_hold.toggled.connect(self._mark_dirty)
        nav.addWidget(self.cb_hold, 0, Qt.AlignmentFlag.AlignVCenter)
        nav.addStretch(1)
        b_su = QPushButton("上移状态")
        b_su.clicked.connect(lambda: self._move_state(-1))
        b_sd = QPushButton("下移状态")
        b_sd.clicked.connect(lambda: self._move_state(1))
        nav.addWidget(b_su)
        nav.addWidget(b_sd)
        self.b_reset = QPushButton("恢复默认排序")
        self.b_reset.setToolTip("把左栏 13 行状态按默认优先级重排（只改顺序，不改绑定）")
        self.b_reset.clicked.connect(self.reset_defaults)
        nav.addWidget(self.b_reset)
        left.addLayout(nav)          # 只占左栏宽度，不跨到右侧

        # ---- 素材与工具：只保留素材目录（ffmpeg 不再暴露配置）----
        tools_box = QGroupBox("素材与工具")
        tools_box.setToolTip(
            "素材目录：packs 根（themes/ 与 animations/ 的父目录），存 config.json:packsDir。\n"
            "抽帧依赖随 `uv sync` 一起装好（imageio-ffmpeg 自带），无需配置。")
        tv = QVBoxLayout(tools_box)

        self._packs_dir = str(cfg.get("packsDir") or "").strip()
        dirrow = QHBoxLayout()
        dirrow.addWidget(QLabel("素材目录："))
        self.ed_packs = QLineEdit()
        self.ed_packs.setReadOnly(True)      # 只读：只能通过「浏览…」或「恢复默认」改，避免手滑
        self.ed_packs.setMinimumWidth(340)
        dirrow.addWidget(self.ed_packs, 1)
        b_browse = QPushButton("浏览…")
        b_browse.clicked.connect(self._pick_packs_dir)
        dirrow.addWidget(b_browse)
        b_packs_default = QPushButton("恢复默认")
        b_packs_default.clicked.connect(self._reset_packs_dir)
        dirrow.addWidget(b_packs_default)
        tv.addLayout(dirrow)
        self._render_packs_hint()

        ts.addWidget(tools_box)
        self.tabs.addTab(tab_states, "状态与动画")

        # ======== 页 2：提醒与连接（番茄钟 + DSH 链路）========
        pomo_cfg = cfg.get("pomodoro") if isinstance(cfg.get("pomodoro"), dict) else {}

        def _pomo_int(key, dflt):
            """番茄钟配置结构与 Tauri 一致：pomodoro.{enabled,workMin,breakMin}。"""
            try:
                return int(pomo_cfg.get(key, dflt))
            except (TypeError, ValueError):
                return dflt

        tab_misc = QWidget()
        tm = QVBoxLayout(tab_misc)
        tm.setContentsMargins(0, 0, 0, 0)   # 与页 1 同口径：10px 外边距 + 10px 分组间距
        tm.setSpacing(10)

        pomo_box = QGroupBox("番茄钟")
        pomo_box.setToolTip("专注 → 状态键 focus，休息 → focus_break；未绑定时自动回落 busy / idle")
        pv = QVBoxLayout(pomo_box)
        self.cb_pomo = QCheckBox("启用番茄钟（托盘菜单与状态映射一并开关）")
        self.cb_pomo.setChecked(pomo_cfg.get("enabled", True) is not False)
        self.cb_pomo.toggled.connect(self._mark_dirty)
        pv.addWidget(self.cb_pomo)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("专注时长"))
        self.sp_pomo_work = QSpinBox()
        self.sp_pomo_work.setRange(1, 180)
        self.sp_pomo_work.setSuffix(" 分钟")
        self.sp_pomo_work.setValue(_pomo_int("workMin", 25))
        self.sp_pomo_work.valueChanged.connect(self._mark_dirty)
        pr.addWidget(self.sp_pomo_work)
        pr.addWidget(QLabel("休息时长"))
        self.sp_pomo_break = QSpinBox()
        self.sp_pomo_break.setRange(1, 60)
        self.sp_pomo_break.setSuffix(" 分钟")
        self.sp_pomo_break.setValue(_pomo_int("breakMin", 5))
        self.sp_pomo_break.valueChanged.connect(self._mark_dirty)
        pr.addWidget(self.sp_pomo_break)
        pr.addStretch(1)
        pv.addLayout(pr)
        tm.addWidget(pomo_box)

        conn_box = QGroupBox("DSH 链路")
        conn_box.setToolTip("DSH 插件(:%d) → dsh_bridge.py → 桌宠 /state；只读显示，不额外探测端口"
                            % DEFAULT_DSH_WS_PORT)
        cv = QVBoxLayout(conn_box)
        link = QHBoxLayout()
        self.dot_link = QLabel()
        self.dot_link.setFixedSize(12, 12)
        link.addWidget(self.dot_link)
        self.lb_link = QLabel("DSH 插件：检测中…")
        link.addWidget(self.lb_link)
        self.lb_link_detail = QLabel("")
        self.lb_link_detail.setStyleSheet("color:%s" % UI["muted"])
        link.addWidget(self.lb_link_detail, 1)
        self.b_link_refresh = QPushButton("重新检测")
        self.b_link_refresh.setFixedWidth(88)
        self.b_link_refresh.clicked.connect(lambda: self.link_probe.refresh())
        link.addWidget(self.b_link_refresh)
        cv.addLayout(link)
        tm.addWidget(conn_box)
        tm.addStretch(1)
        self.tabs.addTab(tab_misc, "提醒与连接")
        self.link_probe = BridgeStatus(pet, self._apply_link_status, self)
        self.link_probe.start()
        # 切回「状态与动画」页时刷新系列下拉：工坊里新建/改名后不用重开设置窗
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._fill_themes()
        self._load_theme(self.theme_id)
        self._update_save_state()
        # 布局完成后用 minimumSizeHint 兜底：窗口再怎么缩都不比内容小，
        # 初始高度也至少够把动画预览窗整段画出来（不再出现下半截被裁）。
        msh = self.minimumSizeHint()
        self.setMinimumSize(min(msh.width(), avail.width()),
                            min(msh.height(), avail.height()))
        if self.height() < msh.height():
            self.resize(self.width(), min(msh.height(), avail.height()))

    # ---- 素材目录 -----------------------------------------------------------
    def _pick_packs_dir(self):
        """浏览选择 packs 根目录；只改草稿值，点「保存配置」才落 config.json。"""
        start = self._packs_dir or packlib.packs_resolution()["path"]
        d = QFileDialog.getExistingDirectory(self, "选择素材目录", start)
        if not d:
            return
        self._packs_dir = os.path.normpath(d)
        self._render_packs_hint()
        self._mark_dirty()
        log("settings_packs_dir picked=%s" % self._packs_dir)

    def _reset_packs_dir(self):
        self._packs_dir = ""
        self._render_packs_hint()
        self._mark_dirty()
        log("settings_packs_dir reset（按默认顺序解析）")

    def _render_packs_hint(self):
        """把当前生效目录 + 来源写进只读框 tooltip；自定义目录不存在时边框标黄。"""
        res = packlib.packs_resolution()
        custom = self._packs_dir
        cur = custom or res["path"]
        if not custom:
            src = res["source"]
        elif not os.path.isdir(custom):
            src = "自定义（目录不存在，已回落到 %s）" % res["source"]
        elif os.path.normcase(custom) == os.path.normcase(res["path"]):
            src = "自定义（已生效）"
        else:
            src = "自定义（保存后重启桌宠生效）"
        self.ed_packs.setText(cur)
        lines = ["当前解析：%s（来源 %s）" % (cur, src)]
        warn = bool(custom) and not os.path.isdir(custom)
        if warn:
            lines.append("⚠ 素材目录不存在：%s；运行时会回落到下一个候选（本次进程实际使用 %s）"
                         % (custom, res["path"]))
        env = os.environ.get("DESKPET_PACKS", "").strip()
        if env:
            lines.append("环境变量 DESKPET_PACKS 优先级最高：%s" % env)
        lines.append("候选顺序：DESKPET_PACKS → 本项 → %s → 就地（deskpet-python、仓库根）"
                     % packlib.default_packs_dir())
        self.ed_packs.setToolTip("\n".join(lines))
        self.ed_packs.setStyleSheet("border:1px solid %s;" % UI["warn"] if warn else "")

    # ---- 主题 ---------------------------------------------------------------
    def _on_tab_changed(self, idx):
        if idx == 0 and not self._loading:
            self._fill_themes()              # 工坊新建/改名后切回本页即可见

    def _new_theme(self):
        """新建系列：交给素材工坊建主题（空映射起步），建完切回本页刷新下拉。"""
        self.pet.open_workshop(pack_id=self.theme_id, new_theme=True)

    def _rename_theme(self):
        """改主题显示名：只动 name 字段并立即写盘，不碰 id / 动画 / 状态绑定。

        重命名不走「保存配置」：它和状态编辑是两件事，写盘时只从磁盘读一份干净的
        索引改 name，不会把设置窗里尚未保存的状态编辑一起写进去。
        """
        tid = self.theme_id
        old = packlib.load_theme(tid).get("name") or tid
        name, ok = QInputDialog.getText(self, "重命名主题",
                                        "主题显示名：", text=old)
        if not ok:
            return
        name = (name or "").strip()
        if not name or name == old:
            return
        th = packlib.load_theme(tid)
        th["name"] = name
        try:
            packlib.write_theme(tid, th)
        except Exception as e:                 # noqa: BLE001 - 对话框里不能 abort
            QMessageBox.warning(self, "重命名", "写盘失败：%s" % e)
            return
        self._raw["name"] = name             # 防止之后点「保存配置」用旧名覆盖
        self._fill_themes()                  # 下拉立即显示新名
        self.lb_theme_info.setText("已重命名为「%s」" % name)
        log("theme_rename %s: %s -> %s" % (tid, old, name))

    def _fill_themes(self):
        self._loading = True
        # 重命名/主题增删会重复调用本方法；先断开，避免 currentIndexChanged 被连多次
        try:
            self.cb_theme.currentIndexChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.cb_theme.clear()
        for t in packlib.list_themes():
            self.cb_theme.addItem(t["name"], t["id"])
        i = self.cb_theme.findData(self.theme_id)
        if i >= 0:
            self.cb_theme.setCurrentIndex(i)
        self._loading = False
        self.cb_theme.currentIndexChanged.connect(self._on_theme_change)
        self._update_sample_notice()

    def _update_sample_notice(self):
        """当前系列是随包示例时，标注并引导安装完整素材包；其它系列隐藏。"""
        if not hasattr(self, "lb_sample"):
            return
        is_sample = packlib.theme_is_sample(self.theme_id)
        if is_sample:
            self.lb_sample.setText(
                "当前使用随包示例素材（只有待机动画）。安装完整素材包：按 README「获取与安装素材包」"
                "解压到 %s，重启后在主题下拉里选择完整主题。" % packlib.default_packs_dir())
        self.lb_sample.setVisible(is_sample)
        self.b_sample_readme.setVisible(is_sample)

    def _open_install_readme(self):
        how = open_install_readme()
        log("settings: 打开安装说明（%s）" % how)
        if how == "none":
            QMessageBox.warning(self, "打开失败", "没能打开 README，请手动访问：\n%s" % REPO_README_URL)

    def _on_theme_change(self):
        if self._loading:
            return
        tid = self.cb_theme.currentData()
        if not tid or tid == self.theme_id:
            return
        if self.changed:
            r = QMessageBox.question(
                self, "切换主题", "当前主题有未保存的配置改动，切换会丢弃。继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                self._loading = True
                self.cb_theme.setCurrentIndex(self.cb_theme.findData(self.theme_id))
                self._loading = False
                return
        self._stash_pool()
        self.pet.request_switch(tid)
        self.switched_theme = tid
        self.theme_id = tid
        self.changed = False
        self._load_theme(tid)
        self._update_save_state()

    def _load_theme(self, tid, raw=None):
        import copy
        raw = packlib.load_theme(tid) if raw is None else raw
        self._raw = raw
        defaults = packlib.empty_theme(tid)["states"]
        self._rows = {}
        for st in STATE_KEYS:
            r = raw["states"].get(st)
            if isinstance(r, dict):
                r = copy.deepcopy(r)
                r.pop("enabled", None)        # 旧字段：读取时忽略，不再暴露
            else:
                r = copy.deepcopy(defaults[st])
            self._rows[st] = r
        prios = {st: self._rows[st].get("priority", packlib.DEFAULT_PRIORITY[st])
                 for st in STATE_KEYS}
        order = sorted(STATE_KEYS, key=lambda s: (-float(prios[s]), list(STATE_KEYS).index(s)))
        self._loading = True
        self.list.clear()
        self._items = {}
        self._editing_state = None
        for st in order:
            self._add_item(st)
        self._loading = False
        self.list.setCurrentRow(0)
        self._refresh_theme_info()

    def _refresh_theme_info(self):
        # 不常驻显示绑定统计/说明：只留「已重命名」等即时反馈。
        self.lb_theme_info.setText("")
        self._update_sample_notice()

    # ---- 左栏 ---------------------------------------------------------------
    def _add_item(self, st):
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole, st)
        it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsDragEnabled)
        it.setSizeHint(QSize(0, 62))
        self.list.addItem(it)
        self._items[st] = it
        self._refresh_item(st)

    def _refresh_item(self, st):
        it = self._items.get(st)
        if it is None:
            return
        pool = self._rows[st].get("clipIds") or []
        names = " / ".join(self._clip_name(c) for c in pool) if pool else "0 段"
        it.setText(STATE_CN.get(st, st))
        it.setData(Qt.ItemDataRole.UserRole + 1, names)
        if pool:
            it.setIcon(QIcon(clip_thumb(pool[0])))
        if st in ("focus", "focus_break"):
            it.setToolTip("番茄钟专用状态：不绑动画时自动回落 %s"
                          % ("busy（忙碌）" if st == "focus" else "idle（待机）"))
        self._refresh_theme_info()

    def _clip_name(self, aid):
        return (CLIPS.get(aid) or packlib.load_asset(aid) or {}).get("name") or                (CLIPS.get(aid) or {}).get("displayName") or aid

    def _cur_state(self):
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _move_state(self, delta):
        stash = self._cur_state()
        row = self.list.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < self.list.count()):
            return
        self._stash_pool()
        it = self.list.takeItem(row)
        self.list.insertItem(new, it)
        self.list.setCurrentRow(new)
        self._mark_dirty()
        log("settings_move_state %s %+d" % (stash, delta))

    def reset_defaults(self):
        order = sorted(STATE_KEYS, key=lambda s: (-packlib.DEFAULT_PRIORITY[s],
                                                  list(STATE_KEYS).index(s)))
        self._stash_pool()
        self._loading = True
        self.list.clear()
        self._items = {}
        for st in order:
            self._add_item(st)
        self._loading = False
        self.list.setCurrentRow(0)
        self._mark_dirty()

    # ---- 右栏动作池 ---------------------------------------------------------
    def _stash_pool(self):
        """把右栏顺序回写进上一个编辑状态的 clipIds（拖拽 InternalMove 没有信号）。"""
        st = self._editing_state
        if not st or st not in self._rows:
            return
        self._rows[st]["clipIds"] = [self.pool.item(i).data(Qt.ItemDataRole.UserRole)
                                     for i in range(self.pool.count())]

    def _sync_editor(self, *a):
        if self._loading:
            return
        self._stash_pool()
        st = self._cur_state()
        self._editing_state = st
        if st is None:
            self.state_box.setTitle("当前状态：—")
            self.b_new.setEnabled(False)
            self.pool.clear()
            self.player.release()
            return
        r = self._rows[st]
        name = STATE_CN.get(st, st)
        pool_ids = r.get("clipIds") or []
        self.state_box.setTitle("当前状态：%s [%s] · %d 段" % (name, st, len(pool_ids)))
        self.b_new.setEnabled(True)
        self.b_new.setToolTip("带着「%s」进素材工坊；生成完自动绑定回这个状态" % name)
        self._loading = True
        self.pool.clear()
        for i, aid in enumerate(pool_ids):
            it = QListWidgetItem("%02d   %s   [%s]" % (i + 1, self._clip_name(aid), aid))
            it.setToolTip("状态动作池第 %d 段：%s\n资产 id：%s"
                          % (i + 1, self._clip_name(aid), aid))
            it.setData(Qt.ItemDataRole.UserRole, aid)
            it.setIcon(QIcon(clip_thumb(aid)))
            self.pool.addItem(it)
        self._loading = False
        if self.pool.count():
            self.pool.setCurrentRow(0)
        else:
            self._reload_player(None)

    def _on_pool_selected(self):
        if self._loading:
            return
        it = self.pool.currentItem()
        self._reload_player(it.data(Qt.ItemDataRole.UserRole) if it else None)

    def _add_from_library(self):
        st = self._cur_state()
        if not st:
            return
        dlg = AssetPicker(self, self.theme_id)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        picked = dlg.chosen()
        if not picked:
            return
        cur = [self.pool.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.pool.count())]
        added = 0
        for aid in picked:
            if aid not in cur:
                cur.append(aid)
                added += 1
        self._fill_pool(cur)
        self._stash_pool()
        self._refresh_item(st)
        self._mark_dirty()
        log("settings_add_assets theme=%s state=%s added=%d" % (self.theme_id, st, added))

    def _remove_selected(self):
        st = self._cur_state()
        row = self.pool.currentRow()
        if not st or row < 0:
            return
        rest = [self.pool.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.pool.count()) if i != row]
        if st == "idle" and not rest:
            QMessageBox.information(self, "待机", "待机建议至少保留一段动画（UI 软约束）。")
            return
        self._fill_pool(rest)
        self._stash_pool()
        self._refresh_item(st)
        self._mark_dirty()

    def _pool_move(self, delta):
        row = self.pool.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < self.pool.count()):
            return
        it = self.pool.takeItem(row)
        self.pool.insertItem(new, it)
        self.pool.setCurrentRow(new)
        self._renumber_pool()

    def _renumber_pool(self, *a):
        """动作池序号重排（上下移 / 拖拽后都走这里）：回写 clipIds 并标脏。"""
        if self._loading:
            return
        self._stash_pool()
        self._loading = True
        for i in range(self.pool.count()):
            it = self.pool.item(i)
            aid = it.data(Qt.ItemDataRole.UserRole)
            it.setText("%02d   %s   [%s]" % (i + 1, self._clip_name(aid), aid))
        self._loading = False
        st = self._cur_state()
        if st:
            self._refresh_item(st)
        self._mark_dirty()

    def _fill_pool(self, ids):
        self._loading = True
        self.pool.clear()
        for i, aid in enumerate(ids):
            it = QListWidgetItem("%02d   %s   [%s]" % (i + 1, self._clip_name(aid), aid))
            it.setToolTip("状态动作池第 %d 段：%s\n资产 id：%s"
                          % (i + 1, self._clip_name(aid), aid))
            it.setData(Qt.ItemDataRole.UserRole, aid)
            it.setIcon(QIcon(clip_thumb(aid)))
            self.pool.addItem(it)
        self._loading = False
        self.pool.setCurrentRow(min(0, max(0, self.pool.count() - 1)))
        self._on_pool_selected()

    # ---- 预览 ---------------------------------------------------------------
    def _reload_player(self, aid):
        if not aid:
            self.player.release()
            self.lb_prog.setText("第 0 / 0 帧")
            self.b_ws.setEnabled(False)
            return
        ensure_clip(aid)
        c = CLIPS.get(aid) or {}
        self.b_ws.setEnabled(True)
        self.player.load(aid, c.get("fps") or 24)
        self.b_play.setText("暂停" if self.player.playing() else "播放")

    def _on_frame(self, idx, n):
        self.lb_prog.setText("第 %d / %d 帧" % ((idx + 1) if n else 0, n))

    def _toggle_play(self):
        if self.player.playing():
            self.player.pause()
            self.b_play.setText("播放")
        else:
            self.player.play()
            self.b_play.setText("暂停" if self.player.playing() else "播放")

    # ---- DSH 链路指示灯 ------------------------------------------------------
    LINK_GREY = LINK_COLORS["grey"]      # 未启用
    LINK_RED = LINK_COLORS["red"]        # 断
    LINK_AMBER = LINK_COLORS["amber"]    # 半通：插件在线但桥没接上 / 端口被占
    LINK_GREEN = LINK_COLORS["green"]    # 全通

    def _apply_link_status(self, s):
        """BridgeStatus 探测快照 → 灯色 + 一行诊断 + 悬停详情。"""
        def tick(ok):
            return "✓" if ok else "✗"

        h = s.get("health") or {}
        ws_port = int(s.get("ws_port") or BridgeStatus.DEFAULT_WS_PORT)
        pet_port = int(s.get("pet_port") or 0)
        sockets = int(h.get("sockets") or 0)
        events = int(h.get("events") or 0)
        plugin_up = bool(s.get("plugin_up"))
        health_ok = bool(h.get("ok"))
        pet_up = bool(s.get("pet_listening"))
        bridge_up = sockets > 0 or bool(s.get("proc_alive"))
        enabled = bool(s.get("enabled"))
        # /health 的 lastAt 是 ISO 字符串（index.js:234 → toISOString()），
        # 不是 epoch 毫秒；两种都兼容，避免把它误判成「尚无事件」。
        last_at = h.get("lastAt")
        ago = None
        if isinstance(last_at, (int, float)) and last_at > 0:
            ago = max(0, int(time.time() - last_at / 1000.0))
        elif isinstance(last_at, str) and last_at:
            try:
                dt = datetime.datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                ago = max(0, int(time.time() - dt.timestamp()))
            except ValueError:
                ago = None
        ago_txt = ("尚无事件" if ago is None else
                   ("%d 秒前" % ago) if ago < 60 else ("%d 分钟前" % (ago // 60)))

        if not enabled:
            color, title = self.LINK_GREY, "DSH 联动未启用"
            hint = ("当前用状态文件轮询（DSH 自动桥接未启用）" if s.get("file_mode")
                    else "状态入口已关闭（--state-port 0），桌宠不会随 DSH 变")
        elif not plugin_up:
            color, title = self.LINK_RED, "DSH 插件未连接"
            hint = "127.0.0.1:%d 未监听（DSH 里插件没加载/没跑）" % ws_port
        elif not health_ok:
            color, title = self.LINK_AMBER, "端口被占用，但不是 DSH 插件"
            hint = "%d 上有别的服务，/health 不是本插件的响应" % ws_port
        elif not pet_up:
            color, title = self.LINK_RED, "桌宠状态入口不可用"
            hint = ("127.0.0.1:%d 绑定失败（端口可能被占用）；"
                    "重启时用 --state-port 换一个端口" % pet_port)
        elif not bridge_up:
            color, title = self.LINK_AMBER, "插件在线 · 桥未接入"
            hint = "桌宠每 5s 自动重连；或手动跑 build/tools/dsh_bridge.py"
        else:
            color, title = self.LINK_GREEN, "已连接 DSH 插件"
            hint = "插件 → 桥 → 桌宠 /state 全链路就绪"

        pid_txt = (" pid=%d" % s["proc_pid"]) if s.get("proc_pid") else ""
        detail = ("插件 :%d %s · /health %s（sockets=%d）· 桥 %s%s · 桌宠 :%d %s · 事件 %d（最近 %s）"
                  % (ws_port, tick(plugin_up), tick(health_ok), sockets,
                     tick(bridge_up), pid_txt, pet_port, tick(pet_up), events, ago_txt))
        self.dot_link.setStyleSheet(
            "background:%s; border-radius:6px; border:1px solid rgba(0,0,0,0.18);" % color)
        self.lb_link.setText(title)
        self.lb_link.setStyleSheet("font-weight:bold; color:%s;" % color)
        self.lb_link_detail.setText("%s ｜ %s" % (hint, detail))
        tip = ("DSH 链路诊断（每 %d 秒刷新）\n"
               "  插件 WebSocket   ws://127.0.0.1:%d   %s\n"
               "  插件 /health      %s  sockets=%d  events=%d  lastAt=%s\n"
               "  中间桥           %s%s\n"
               "  桌宠状态入口     http://127.0.0.1:%d/state   %s\n"
               "  桥日志           build/cache/dsh_bridge.log"
               % (BridgeStatus.INTERVAL_MS // 1000, ws_port,
                  "在线" if plugin_up else "离线",
                  "正常" if health_ok else "无响应", sockets, events, ago_txt,
                  "运行中" if s.get("proc_alive") else "未由桌宠托管",
                  ("（pid=%d）" % s["proc_pid"]) if s.get("proc_pid") else "",
                  pet_port, "已监听" if pet_up else "未监听"))
        for w in (self.dot_link, self.lb_link, self.lb_link_detail):
            w.setToolTip(tip)

    def _confirm_discard(self):
        return QMessageBox.question(
            self, "未保存的配置改动", "有未保存的配置改动，确定关闭设置？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def _cleanup(self):
        self.link_probe.stop()
        self.player.release()

    def closeEvent(self, e):
        if self.changed and not self._confirm_discard():
            e.ignore()
            return
        self._cleanup()
        e.accept()

    def reject(self):
        if self.changed and not self._confirm_discard():
            return
        self._cleanup()
        super().reject()

    # ---- 工坊 ---------------------------------------------------------------
    def _new_animation(self):
        """带目标状态进工坊：生成完默认勾这个状态（V3 裁决）。"""
        st = self._cur_state()
        if not st:
            return
        self.pet.open_workshop(origin_state=st, pack_id=self.theme_id)

    def _open_in_workshop(self):
        it = self.pool.currentItem()
        if it:
            self.pet.open_workshop(it.data(Qt.ItemDataRole.UserRole), self.theme_id)

    # ---- 未保存状态 / 备份 ---------------------------------------------------
    def _mark_dirty(self, *a):
        """任何改动都走这里：置脏并刷新「保存配置」按钮可用性。"""
        if getattr(self, "_loading", False):
            return
        self.changed = True
        self._update_save_state()

    def _update_save_state(self):
        self.b_save.setEnabled(bool(self.changed))
        self.b_save.setToolTip("保存配置并热生效" if self.changed else "没有未保存的配置改动")

    def _restore_last_save(self):
        """读本系列的单份 .bak 回填界面；不立即写盘，用户再点「保存配置」才生效。"""
        p = packlib.theme_bak_path(self.theme_id)
        if not os.path.isfile(p):
            QMessageBox.information(
                self, "恢复上次配置",
                "还没有上次配置的备份：\n%s\n\n保存配置一次之后才会生成这份 .bak。" % p)
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "恢复上次配置", "读取备份失败：%s\n%s" % (p, e))
            return
        if not isinstance(data, dict) or not isinstance(data.get("states"), dict):
            QMessageBox.warning(self, "恢复上次配置", "备份内容不是有效的主题映射：\n%s" % p)
            return
        r = QMessageBox.question(
            self, "恢复上次配置",
            "用上次保存的配置回填当前界面？\n（不会立即写盘，确认无误后点「保存配置」才生效）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        self._load_theme(self.theme_id, raw=data)
        self._mark_dirty()
        log("settings_restore_backup theme=%s bak=%s" % (self.theme_id, p))

    # ---- 保存 ---------------------------------------------------------------
    def save(self):
        try:
            self._stash_pool()
            order = [self.list.item(i).data(Qt.ItemDataRole.UserRole)
                     for i in range(self.list.count())]
            n = len(order)
            prios = {st: (n - 1 - i) * 10 for i, st in enumerate(order)}
            for st in order:
                r = self._rows[st]
                r.pop("enabled", None)            # 不再写「启用」字段；空池即不绑定
                r["priority"] = prios[st]
                r["clipIds"] = list(r.get("clipIds") or [])
            self._raw["states"] = {st: self._rows[st] for st in STATE_KEYS}
            self._raw["name"] = self._raw.get("name") or self.theme_id
            self.write_theme()
            patch = {                                # 读-改-写，保留未知键
                # tier 不在设置里改（仍走托盘/菜单），这里不写入，避免覆盖 Tauri 侧的值
                "remindHold": bool(self.cb_hold.isChecked()),
                "packsDir": self._packs_dir,          # 空串 = 按默认顺序解析
                "pomodoro": {                         # 与 Tauri config.json 同构
                    "enabled": bool(self.cb_pomo.isChecked()),
                    "workMin": int(self.sp_pomo_work.value()),
                    "breakMin": int(self.sp_pomo_break.value()),
                },
            }
            cfg = save_config(patch)
            if not cfg:
                raise OSError("config 写入失败，详见日志")
            self.pet.hold_reminders = cfg["remindHold"]
            self.pet.apply_pomodoro_config(cfg)
            self.changed = False
            self._update_save_state()
            # 非模态：保存时立即热生效（原来靠 open_settings 的 exec() 返回后处理）
            if getattr(self.pet, "sm", None) is not None:
                self.pet.sm.hold_reminders = cfg["remindHold"]
                self.pet.sm.load(force=True)
            log("settings_save_v2 theme=%s tier=%s bound=%d/%d packsDir=%s pomo=%s/%d/%d"
                % (self.theme_id, getattr(self.pet, "tier_name", "?"),
                   sum(1 for r in self._rows.values() if r["clipIds"]), n,
                   cfg["packsDir"] or "(默认)",
                   cfg["pomodoro"]["enabled"], cfg["pomodoro"]["workMin"],
                   cfg["pomodoro"]["breakMin"]))
            # 保存后不关窗：给个内联提示，3 秒后自动淡出，方便连续改多项
            self.lb_save_status.setText("✓ 已保存配置（窗口保持打开）")
            QTimer.singleShot(3000, lambda: self.lb_save_status.setText("")
                              if self.lb_save_status.text().startswith("✓") else None)
        except Exception as e:                     # noqa: BLE001 - 对话框里不能 abort
            log("settings_save exception=%r" % (e,))
            QMessageBox.warning(self, "设置", "保存配置失败：%s" % e)

    def write_theme(self):
        """写 packs/themes/<id>.json：只保留单份 <同名>.bak（每次保存覆盖），原子替换。"""
        packlib.backup_theme(self.theme_id)
        return packlib.write_theme(self.theme_id, self._raw)


def setup_tray(app, pet):
    """系统托盘：显示/隐藏、动作（分组）、尺寸、素材工坊、设置、退出。

    无系统托盘环境（少数精简版 Windows / 远程会话）下只打日志并返回 None，
    窗口右键菜单与 Esc 仍可正常退出，不阻塞启动。
    诊断日志并入主日志（默认关闭，DESKPET_LOG=1 打开）：pythonw 启动时没有控制台，只靠 isVisible 不够。
    """
    icon = pet.windowIcon()
    dpr = app.devicePixelRatio() if hasattr(app, "devicePixelRatio") else 1
    il = integrity_level()
    tray_log("tray_init integrity=%s platform=%s screens=%d dpr=%s available=%s supports_messages=%s "
             "icon_null=%s sizes=%s qpa=%s"
             % (il, app.platformName(), len(QApplication.screens()), dpr,
                QSystemTrayIcon.isSystemTrayAvailable(), QSystemTrayIcon.supportsMessages(),
                icon.isNull(), ["%dx%d" % (s.width(), s.height()) for s in icon.availableSizes()],
                os.environ.get("QT_QPA_PLATFORM", "windows(default)")))
    if il == "S-1-16-4096":
        tray_log("tray_init ⚠ 当前进程是 Low 完整性，Windows 会拒绝托盘图标（Shell_NotifyIcon=5）。"
                 "修复：icacls .venv\\Scripts\\python.exe /setintegritylevel Medium "
                 "（pythonw.exe 同理），然后重启桌宠")
    if not QSystemTrayIcon.isSystemTrayAvailable():
        tray_log("tray available=False reason=isSystemTrayAvailable()==False（本机没有系统托盘，跳过；"
                 "右键菜单与 Esc 仍可用）")
        return None
    tray = QSystemTrayIcon(icon, app)          # 父对象是 QApplication，不是隐藏的 Tool 窗口
    tray.setToolTip("智灵 · 桌宠")
    menu = QMenu()
    menu.setToolTipsVisible(True)

    def rebuild_menu():
        """按当前系列重建托盘菜单（切系列后菜单不能残留旧系列）。"""
        menu.clear()
        act_toggle = QAction("显示 / 隐藏", menu)
        act_toggle.triggered.connect(pet.toggle_visible)
        menu.addAction(act_toggle)
        menu.addMenu(pet.size_menu(menu))
        menu.addMenu(pet.theme_menu(menu))
        # 番茄钟 ▸ 开始 / 暂停 / 停止（总开关关闭时整块不显示）
        pom = getattr(pet, "pomodoro", None)
        pet.tray_pomo = None
        if pom is not None and pom.enabled:
            pm = QMenu("番茄钟", menu)
            pm.setToolTipsVisible(True)
            a_start = QAction("开始", pm)
            a_start.setToolTip("开始/继续当前阶段（专注 → 休息 → 专注…）")
            a_start.triggered.connect(pet.pomodoro_start)
            a_pause = QAction("暂停", pm)
            a_pause.setToolTip("暂停后剩余时间冻结，继续时从冻结处接着走")
            a_pause.triggered.connect(pet.pomodoro_pause)
            a_stop = QAction("停止", pm)
            a_stop.setToolTip("停止并清空当前阶段，专注画面回到待机")
            a_stop.triggered.connect(pet.pomodoro_stop)
            for a in (a_start, a_pause, a_stop):
                pm.addAction(a)
            menu.addMenu(pm)
            pet.tray_pomo = {"menu": pm, "start": a_start,
                             "pause": a_pause, "stop": a_stop}
        menu.addSeparator()
        act_ws = QAction("素材工坊…", menu)      # 只保留在托盘菜单，桌宠右键不出现
        act_ws.triggered.connect(lambda _=False: pet.open_workshop())
        menu.addAction(act_ws)
        act_set = QAction("设置…", menu)
        act_set.triggered.connect(pet.open_settings)
        menu.addAction(act_set)
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(QApplication.quit)
        menu.addAction(act_quit)
        pet._pomo_refresh_ui()      # 新菜单先排一次文案（切系列/开关后立即正确）
        tray_log("tray menu rebuilt pack=%s actions=%d" % (PACK_ID, len(menu.actions())))

    rebuild_menu()
    tray.setContextMenu(menu)
    tray._menu = menu            # Qt 不接管所有权，必须自己留引用，否则菜单会被回收
    pet.tray_menu_builder = rebuild_menu      # 切系列后 Pet.apply_pack 会调用它重建

    def on_activated(reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            pet.toggle_visible()

    tray.activated.connect(on_activated)
    tray.setVisible(True)        # 显式 setVisible：show() 在部分环境首次不生效
    pet.tray = tray              # 强引用：局部变量在 main() 里虽活着，但显式保存更好排查
    pet.tray_menu = menu         # 菜单也留强引用（setContextMenu 不转移所有权）
    pet._pomo_refresh_ui()       # 初次排版番茄钟菜单文案 + tooltip（此时 tray 已就绪）

    def _snap(tag):
        try:
            actual = tray.icon().actualSize(QSize(16, 16))
            tray_log("tray_state[%s] available=%s visible=%s visible_icon=%s geometry=%s "
                     "icon_null=%s actual16=%dx%d sizes=%s"
                     % (tag, QSystemTrayIcon.isSystemTrayAvailable(), tray.isVisible(),
                        tray.isVisible(), tray.geometry(), tray.icon().isNull(),
                        actual.width(), actual.height(),
                        ["%dx%d" % (s.width(), s.height()) for s in tray.icon().availableSizes()]))
        except Exception as e:                 # noqa: BLE001 - 诊断日志不能反过来炸主流程
            tray_log("tray_state[%s] exception=%r" % (tag, e))

    _snap("after_create")
    QTimer.singleShot(1000, lambda: _snap("t+1s"))
    QTimer.singleShot(5000, lambda: _snap("t+5s"))
    tray_log("tray menu_items=%d（Win11 若没看到：点任务栏 ^ 展开；"
             "设置→个性化→任务栏→其他系统托盘图标 可把「智灵 · 桌宠」设为常显）"
             % len(menu.actions()))
    return tray


def _install_excepthook():
    """pythonw 没有控制台：未捕获异常必须落到日志，否则崩了完全无声。"""
    def hook(exc_type, exc, tb):
        log_fatal("UNCAUGHT %s: %s" % (exc_type.__name__, exc))
        try:
            log_fatal("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        if sys.stderr is not None:
            traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = hook

    def thook(args):
        hook(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = thook


def _claim_instance():
    """同目录启动多个 pet.py 时记一条警告（重复托盘图标常被误判为「没显示」）。"""
    lock = os.path.join(ROOT, "build", "cache", "pet.lock")
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        other = None
        if os.path.exists(lock):
            try:
                other = int(open(lock, encoding="utf-8").read().strip() or 0)
            except (OSError, ValueError):
                other = None
        if other and other != os.getpid() and _pid_alive(other):
            tray_log("WARN another pet instance pid=%d is still running（托盘里可能有两个图标）" % other)
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def main():
    global META, MANIFEST, CLIPS, ACTIONS, LOG_ENABLED, VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=0, help="宠物宽度(像素)，默认中档=屏幕宽 15%%")
    ap.add_argument("--pack", default="", help="指定素材包（默认读 themes/*.json 里 active=true 的主题）")
    ap.add_argument("--verbose", action="store_true", help="启动时打印 pack 画布与素材 license（同时打开日志）")
    ap.add_argument("--log", default="", help="事件日志路径（传路径即打开；默认关闭，也可用 DESKPET_LOG=1）")
    ap.add_argument("--clip", default="", help="启动时直接播某个动作（调试用）")
    ap.add_argument("--state", default="", help="启动时强制进入某个状态（调试用：click/done/…）")
    ap.add_argument("--state-map", default="", help="状态映射配置路径（默认当前主题的 themes/<id>.json）")
    ap.add_argument("--state-port", type=int, default=DEFAULT_STATE_PORT,
                    help="监听本地 HTTP 状态输入：POST http://127.0.0.1:<port>/state"
                         "（默认 %d，启动时自动桥接 DSH；传 0 关闭。只绑 127.0.0.1）"
                         % DEFAULT_STATE_PORT)
    ap.add_argument("--state-file", default="",
                    help="轮询 JSON 状态文件（内容变化才应用；纯文本 streaming 也认）")
    ap.add_argument("--no-bridge", action="store_true",
                    help="默认会自动拉起 build/tools/dsh_bridge.py 桥接 DSH；"
                         "加这个开关则只保留手动方式（状态入口仍开着）")
    ap.add_argument("--freeze", action="store_true",
                    help="调试用：停在第一帧不播，供命中测试取稳定样本")
    args = ap.parse_args()
    # 日志默认关闭（生产不写常规日志）；DESKPET_LOG=1 或 --log <路径> / --verbose 临时打开，
    # 打开后按大小轮转：单文件 2 MB，归档固定 .1，不按时间戳累积。
    _resolve_log(args.log, args.verbose)
    if LOG_ENABLED:
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        except OSError:
            pass
    VERBOSE = args.verbose
    _install_excepthook()
    _claim_instance()

    # 旧 %LOCALAPPDATA%\DeskPet → 新 %LOCALAPPDATA%\ZhiLing（首次运行一次性迁移）。
    # 迁移说明无论日志开关都写一条：迁移成功后旧目录已不在，不记就再也查不到。
    _mig = packlib.root_migration()
    if _mig["status"] == "ok":
        _write_log(_always_log_path(),
                   "user_root_migrate %s → %s（迁移 %d 项）"
                   % (_mig["legacy_root"], _mig["new_root"], _mig["moved"]))
    elif _mig["failed"]:
        _write_log(_always_log_path(),
                   "user_root_migrate 失败: %s → %s err=%s；继续使用旧目录 %s"
                   % (_mig["legacy_root"], _mig["new_root"], _mig["error"],
                      packlib.app_root()))

    # 素材目录统一解析：DESKPET_PACKS → config.json:packsDir → %LOCALAPPDATA%\ZhiLing\packs → 就地。
    # 解析告警（自定义目录不存在等）直接进日志，便于用户按提示处理。
    packlib.set_warn_sink(log)
    _cfg_src = ("env:DESKPET_CONFIG" if os.environ.get("DESKPET_CONFIG", "").strip()
                else "default:LocalAppData")
    log("log_path=%s（轮转 2MB×1）" % (LOG_PATH if LOG_ENABLED else "关闭"))
    log("config_path=%s（来源 %s）" % (packlib.config_path(), _cfg_src))
    packlib.migrate_legacy_config()            # 旧 deskpet-python\config.json 一次性合并
    _packs_res = packlib.packs_resolution()
    log("packs_root=%s（来源 %s）" % (_packs_res["path"], _packs_res["source"]))
    for _w in _packs_res["warnings"]:
        log(_w)

    # Win11 托盘图标按 AppUserModelID 归档；显式设置可避免图标被当成「临时进程」折叠或错位
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Zhiling.DeskPet.Python")
            tray_log("win_appid=Zhiling.DeskPet.Python")
        except Exception as e:                 # noqa: BLE001
            tray_log("win_appid failed: %r" % (e,))
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)       # 关掉宠物窗口不退进程；只在这里设置，后面不再覆盖

    # A+C：无可用素材 → 先装随包示例（只写默认位置、不覆盖用户数据）；
    # 仍无素材才弹一次可见提示（pythonw / run.vbs 下同样有窗口，不依赖 stdout）。
    if not args.pack:
        if not packlib.has_usable_material():
            ok, msg = packlib.install_sample_pack()
            log("first_run: " + msg)
            if ok:
                _r = packlib.packs_resolution()
                log("packs_root=%s（来源 %s，已用随包示例）" % (_r["path"], _r["source"]))
        if not packlib.has_usable_material():
            if not first_run_asset_guard():
                return
            _r = packlib.packs_resolution()
            log("packs_root=%s（来源 %s，首次引导选择后）" % (_r["path"], _r["source"]))
        _set_pack("")                          # 示例 / 所选目录可能换了当前系列

    if args.pack:
        if not os.path.exists(packlib.theme_path(args.pack)):
            raise SystemExit("找不到主题：packs/themes/%s.json" % args.pack)
        _set_pack(args.pack)

    META = {}                     # v2 不再读 meta.json / manifest.json
    MANIFEST = {}
    CLIPS, ACTIONS = load_clips()
    canvas = pack_canvas()[:2]
    validate_canvas_frames()
    if PACK_ID:
        log("pack=%s（packs/%s），%d 段，待机=%s，canvas=%dx%d" %
            (PACK_ID, PACK_ID, len(CLIPS), IDLE_ID, canvas[0], canvas[1]))
    else:
        log("packs/ 里没有可用素材包，回退旧 deskpet-python/frames_* 目录；"
            "建议运行 python build/tools/migrate_packs.py 迁移到 packs/<packId>/")
    if VERBOSE:
        lic = (MANIFEST or {}).get("license") or {}
        msg = ("[verbose] canvas=%dx%d（%s） license=%s 来源=%s%s"
               % (canvas[0], canvas[1], pack_canvas()[2],
                  lic.get("kind", "unknown"), lic.get("source") or "（未填写）",
                  (" 备注=" + lic["note"]) if lic.get("note") else ""))
        log(msg)                      # pythonw 下 sys.stderr 为 None，只写日志
        if sys.stderr is not None:
            print(msg, file=sys.stderr)

    screen = app.primaryScreen().availableGeometry()
    cfg = load_config()
    cfg_tier = cfg.get("tier")
    hold_reminders = cfg.get("remindHold", True) is not False
    if args.width:
        width, tier = args.width, ""
    else:
        tier = cfg_tier if cfg_tier in TIERS else DEFAULT_TIER   # 设置里保存的默认尺寸
        width = max(140, int(screen.width() * TIERS[tier]))

    pet = Pet(width)
    pet.tier_name = tier
    pet.hold_reminders = hold_reminders
    pet.move(screen.right() - pet.width() - 24, screen.bottom() - pet.height() - 12)

    # ---- 主题热生效：主题 active 标记改了就换装，不用重启 ----
    if not args.pack:
        pet.pack_watcher = ActivePackWatcher(pet)

    # ---- 状态映射：当前系列的 themes/<id>.json，热加载 ----
    light = {cid: {"fps": c["fps"], "count": c["count"], "name": c["displayName"]}
             for cid, c in CLIPS.items()}
    sm_path = args.state_map or STATE_MAP_PATH
    pet.sm = StateMap(sm_path, light, pet.state_play, pet.state_play_idle, log)
    pet.sm.hold_reminders = pet.hold_reminders
    pet.sm.playing = IDLE_ID
    pet.sm.set_state("idle", "启动")

    def state_tick():
        pet.sm.load()          # mtime 没变就什么都不做（800ms 级去抖按内容比对）
        pet.sm.tick()

    state_timer = QTimer(pet)
    state_timer.timeout.connect(state_tick)
    state_timer.start(1000)

    # ---- 番茄钟：纯逻辑计时 + 1s UI 心跳；剩余时间由单调时钟现算，隐藏/失焦不漂移 ----
    pet.apply_pomodoro_config(cfg)
    pomo_timer = QTimer(pet)
    pomo_timer.timeout.connect(pet.pomodoro_tick)
    pomo_timer.start(1000)

    # ---- 外部状态输入：只有显式给参才开启，避免默认占用端口 ----
    state_in = None
    if args.state_port or args.state_file:
        state_in = StateIn(pet, args.state_port, args.state_file)
        state_in.start()
        pet.state_in = state_in        # 强引用，避免被回收后监听线程成孤儿

    # ---- DSH 桥托管：只开一个进程，用户只需记住桌宠这一条命令 ----
    bridge = None
    if args.state_port and not args.no_bridge:
        bridge = BridgeAuto(args.state_port)
        bridge.start_watch()
        app.aboutToQuit.connect(bridge.stop)   # 桌宠退出时一并终止桥
    pet.bridge = bridge        # 设置窗指示灯读桥进程状态；None = --no-bridge / 未开状态端口

    pet.show()
    pet.repaint()          # 同步画完第一帧，再记时间
    tray = setup_tray(app, pet)            # 隐藏到托盘不等于退出：quit 策略在创建 app 时已设
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
