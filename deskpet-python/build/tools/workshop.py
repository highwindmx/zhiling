"""素材工坊：把绿幕视频抠成透明帧序列，写进 packs/<packId>/frames/<clipId> 并登记 manifest。

三步流程（一屏三块）：① 导入 → ② 抠像 → ③ 导出。
抠像全部调用 build/keyer.py 的 key_frame()，生成调用 build/build_frames.py 的 build_clip()。

运行：
    uv run python build/workshop.py
    .venv\\Scripts\\python.exe build/workshop.py      （没有 uv 时的降级路径）
"""
import json, os, re, subprocess, sys, time, traceback
import numpy as np
from PIL import Image
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QPen
from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QListWidget, QListWidgetItem,
                             QPushButton, QHBoxLayout, QVBoxLayout, QSlider, QDoubleSpinBox,
                             QComboBox, QSpinBox, QPlainTextEdit, QProgressBar, QSplitter,
                             QFileDialog, QSizePolicy, QCheckBox, QLineEdit, QInputDialog,
                             QMessageBox, QGroupBox, QToolButton, QFormLayout, QDialog,
                             QDialogButtonBox)

try:                                    # 可选：装了 pypinyin 就能把中文名转成拼音 id
    from pypinyin import lazy_pinyin
except Exception:                       # 没装就退回 clip_NN，不拦主流程
    lazy_pinyin = None

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
REPO = os.path.dirname(ROOT)
sys.path.insert(0, BASE)
from keyer import key_frame
import build_frames
import pack

EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")

# 高级参数：只有展开"高级"才出现。默认值就是第一轮验证过的保守参数。
ADV_DEFS = [
    ("hue_in", "色相内", 0, 90, 18.0, "与背景色相差在这个角度以内 → 完全判为背景。"),
    ("hue_out", "色相外", 0, 120, 34.0, "超出这个角度 → 完全判为前景；两者之间是软过渡。"),
    ("sat_lo", "饱和度下限", 0, 1, 0.30, "防止把角色灰白衣物当背景抠掉。"),
    ("sat_hi", "饱和度上限", 0, 1, 0.55, "高于此值才「够绿」。"),
    ("floor", "alpha 软地板", 0, 0.95, 0.16, "回收淡绿雾：alpha 低于此值直接归零。"),
    ("despill", "去溢色", 0, 1, 0.75, "削掉边缘绿光。"),
    ("blur", "羽化", 0, 8, 1.0, "alpha 边缘模糊半径（像素）。"),
]
HUE_DEFAULT = 136.0
FIXED = dict(val_lo=0.30, val_hi=0.45, erode=1, watermarks=[], keep_frac=0.05)
PRESET_DIR = os.path.join(ROOT, "presets")


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


def sample_hue_from(rgb):
    """点四角取样：返回 (色相中位数, 饱和度中位数)。"""
    h, w, _ = rgb.shape
    k = max(8, min(h, w) // 12)
    px = np.concatenate([c.reshape(-1, 3) for c in
                         (rgb[:k, :k], rgb[:k, -k:], rgb[-k:, :k], rgb[-k:, -k:])])
    mx, mn = px.max(1), px.min(1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    d = mx - mn + 1e-6
    hue = np.where(mx == px[:, 0], ((px[:, 1] - px[:, 2]) / d) % 6,
                   np.where(mx == px[:, 1], (px[:, 2] - px[:, 0]) / d + 2,
                            (px[:, 0] - px[:, 1]) / d + 4)) * 60
    pick = sat >= np.percentile(sat, 60)
    return float(np.median(hue[pick])), float(np.median(sat[pick]))


class Canvas(QWidget):
    """棋盘格 + 居中预览；自动裁边时把被裁掉的区域盖上半透明遮罩。"""

    def __init__(self):
        super().__init__()
        self.pix = None
        self.rect_image = (0, 0)      # 原图尺寸（用于把 crop 映射到显示坐标）
        self.crop = None              # (l, t, r, b)
        self.setMinimumSize(360, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_image(self, pix, size=None, crop=None):
        self.pix = pix
        self.rect_image = size or ((pix.width(), pix.height()) if pix else (0, 0))
        self.crop = crop
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#26262b"))
        step = 12
        for y in range(0, self.height(), step):
            for x in range(0, self.width(), step):
                if (x // step + y // step) % 2 == 0:
                    p.fillRect(x, y, step, step, QColor("#33333a"))
        if not self.pix or self.pix.isNull():
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "把绿幕视频拖进左边")
            return
        w, h = self.pix.width(), self.pix.height()
        s = min(self.width() / w, self.height() / h)
        rw, rh = max(1, int(w * s)), max(1, int(h * s))
        x0, y0 = (self.width() - rw) // 2, (self.height() - rh) // 2
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawPixmap(x0, y0, rw, rh, self.pix)
        if self.crop and self.rect_image[0]:
            iw, ih = self.rect_image
            l, t, r, b = self.crop
            sx, sy = rw / iw, rh / ih
            bands = [(x0, y0, l * sx, rh), (x0 + r * sx, y0, rw - r * sx, rh),
                     (x0 + l * sx, y0, (r - l) * sx, t * sy),
                     (x0 + l * sx, y0 + b * sy, (r - l) * sx, rh - b * sy)]
            for (bx, by, bw, bh) in bands:
                if bw > 0 and bh > 0:
                    p.fillRect(int(bx), int(by), int(bw), int(bh), QColor(255, 90, 90, 60))
            p.setPen(QPen(QColor("#ff5a5a"), 1, Qt.PenStyle.DashLine))
            p.drawRect(int(x0 + l * sx), int(y0 + t * sy), int((r - l) * sx), int((b - t) * sy))


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
        lab.setFixedWidth(78)
        self.sld = QSlider(Qt.Orientation.Horizontal)
        self.sld.setRange(0, 1000)
        self.sld.setMinimumWidth(90)
        self.spb = QDoubleSpinBox()
        self.spb.setRange(lo, hi)
        self.spb.setDecimals(2 if hi <= 1 else 1)
        self.spb.setSingleStep(round((hi - lo) / 100, 3))
        self.spb.setFixedWidth(84)
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
    """新建素材包：只问 packId / 角色名 / 画布，画布默认 576×530（pack.DEFAULT_CANVAS），可改。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建素材包")
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
        self.sp_w.setValue(pack.DEFAULT_CANVAS[0])
        self.sp_h = QSpinBox()
        self.sp_h.setRange(192, 1024)
        self.sp_h.setSingleStep(32)
        self.sp_h.setValue(pack.DEFAULT_CANVAS[1])
        row.addWidget(self.sp_w)
        row.addWidget(QLabel("×"))
        row.addWidget(self.sp_h)
        row.addStretch(1)
        form.addRow("packId", self.ed_id)
        form.addRow("角色名", self.ed_role)
        form.addRow("画布", row)
        lay.addLayout(form)
        tip = QLabel("为什么建议别改画布：画布是 pack 级全局参数，所有动作都按同一画布做缩放和"
                     "脚底锚点对齐。改小了以后 pack 里所有 clip 都要重抠一遍；改大了旧 clip 会被"
                     "拉伸模糊。没有特别理由请保持默认 576×530；改了以后这个 pack 的所有 clip 都要重导。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#c90")
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
        except Exception:
            self.failed.emit(traceback.format_exc())


class Workshop(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("素材工坊 — 视频 → 素材包（pack）")
        self.resize(1320, 880)
        self.videos = []          # [{path, cid, name, frames, fps}]
        self._last_row = -1
        self.raw_rgb = None
        self.raw_size = None      # 原片尺寸（遮罩用）
        self.crop = None          # 自动裁边结果（整片统一裁一次）
        self.gen = None
        self.queue = []           # 批量排队 [(video, cid, name)]
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self.render)
        self._build_ui()
        self.refresh_packs()

    # ---------- 界面 ----------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        # ① 导入
        g1 = QGroupBox("① 导入")
        lv = QVBoxLayout(g1)
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
        lv.addLayout(rowb)
        form = QFormLayout()
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("中文名（菜单里显示，如「得意状」）")
        self.ed_name.editingFinished.connect(self.save_row_meta)
        self.ed_id = QLineEdit()
        self.ed_id.setReadOnly(True)
        self.ed_id.setStyleSheet("color:#888;background:transparent;border:none")
        self.ed_id.setToolTip("由显示名自动生成的 ASCII 键（稳定键，菜单/配置里用它）；要手改去「高级参数」")
        form.addRow("显示名", self.ed_name)
        form.addRow("clipId（自动）", self.ed_id)
        lv.addLayout(form)
        self.lb_info = QLabel("")
        self.lb_info.setWordWrap(True)
        self.lb_info.setStyleSheet("color:#9aa")
        lv.addWidget(self.lb_info)
        split.addWidget(g1)

        # ② 抠像
        g2 = QGroupBox("② 抠像")
        mv = QVBoxLayout(g2)
        rowv = QHBoxLayout()
        rowv.addWidget(QLabel("视图"))
        self.view_mode = QComboBox()
        self.view_mode.addItems(["原片", "抠像后", "alpha"])
        self.view_mode.currentIndexChanged.connect(self.schedule_render)
        rowv.addWidget(self.view_mode)
        rowv.addStretch(1)
        self.lb_perf = QLabel("")
        self.lb_perf.setStyleSheet("color:#888")
        rowv.addWidget(self.lb_perf)
        mv.addLayout(rowv)
        self.canvas = Canvas()
        mv.addWidget(self.canvas, 1)
        roww = QHBoxLayout()
        roww.addWidget(QLabel("帧"))
        self.frame_sld = QSlider(Qt.Orientation.Horizontal)
        self.frame_sld.setRange(0, 0)
        self.frame_sld.valueChanged.connect(self.on_frame)
        roww.addWidget(self.frame_sld, 1)
        self.lb_frame = QLabel("0 / 0")
        self.lb_frame.setFixedWidth(80)
        roww.addWidget(self.lb_frame)
        mv.addLayout(roww)

        quick = QHBoxLayout()
        self.cb_crop = QCheckBox("自动裁黑边")
        self.cb_crop.setToolTip("扫全片近黑/深色均匀边，取并集裁一次；裁掉的区域在预览里标红")
        self.cb_crop.setChecked(True)          # 默认开：拖进来什么都不改也能一次跑通
        self.cb_crop.stateChanged.connect(self.on_crop_toggled)
        quick.addWidget(self.cb_crop)
        b_hue = QPushButton("取背景色（点四角取样）")
        b_hue.clicked.connect(self.sample_hue)
        quick.addWidget(b_hue)
        quick.addStretch(1)
        mv.addLayout(quick)

        # 高级（默认收起）
        self.adv_btn = QToolButton()
        self.adv_btn.setText("高级参数")
        self.adv_btn.setCheckable(True)
        self.adv_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.adv_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.adv_btn.clicked.connect(self.toggle_adv)
        mv.addWidget(self.adv_btn)
        self.adv = QWidget()
        av = QVBoxLayout(self.adv)
        av.setContentsMargins(0, 0, 0, 0)
        rid = QHBoxLayout()
        rid.addWidget(QLabel("手改 clipId"))
        self.ed_id_manual = QLineEdit()
        self.ed_id_manual.setPlaceholderText("留空 = 按显示名自动生成；只能 ASCII")
        self.ed_id_manual.editingFinished.connect(self.apply_manual_id)
        rid.addWidget(self.ed_id_manual, 1)
        av.addLayout(rid)
        self.rows = {}
        self.rows["hue_ref"] = SliderRow("背景色相", 0, 360, HUE_DEFAULT,
                                        "绿幕色相（0~360°）。用「取背景色」自动填。")
        self.rows["hue_ref"].changed.connect(self.schedule_render)
        av.addWidget(self.rows["hue_ref"])
        for key, name, lo, hi, val, tip in ADV_DEFS:
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
        self.adv.setVisible(False)
        mv.addWidget(self.adv)
        split.addWidget(g2)

        # ③ 导出
        g3 = QGroupBox("③ 导出")
        rv = QVBoxLayout(g3)
        form3 = QFormLayout()
        prow = QHBoxLayout()
        self.cb_pack = QComboBox()
        self.cb_pack.setMinimumWidth(120)
        prow.addWidget(self.cb_pack, 1)
        b_newpack = QPushButton("新建…")
        b_newpack.clicked.connect(self.new_pack)
        prow.addWidget(b_newpack)
        form3.addRow("目标 pack", prow)
        rv.addLayout(form3)
        self.cmp = QLabel("生成后这里显示：新素材 vs 当前待机帧（看角色有没有突然变大）")
        self.cmp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cmp.setMinimumHeight(120)
        self.cmp.setStyleSheet("color:#888;background:#202025;border:1px solid #333")
        rv.addWidget(self.cmp)
        self.lb_result = QLabel("")
        self.lb_result.setWordWrap(True)
        self.lb_result.setStyleSheet("color:#7fb;font-weight:600")
        rv.addWidget(self.lb_result)
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        rv.addWidget(self.prog)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        rv.addWidget(self.log_box, 1)
        split.addWidget(g3)
        split.setSizes([280, 640, 400])

        bottom = QHBoxLayout()
        self.b_gen = QPushButton("开始生成")
        self.b_gen.clicked.connect(self.generate)
        self.b_cancel = QPushButton("取消")
        self.b_cancel.setEnabled(False)
        self.b_cancel.clicked.connect(self.cancel)
        self.b_open = QPushButton("打开输出目录")
        self.b_open.clicked.connect(self.open_out)
        for b in (self.b_gen, self.b_cancel, self.b_open):
            bottom.addWidget(b)
        bottom.addStretch(1)
        outer.addLayout(bottom)

    def toggle_adv(self, on):
        self.adv.setVisible(bool(on))
        self.adv_btn.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    # ---------- pack ----------
    def refresh_packs(self):
        names = pack.list_packs()
        self.cb_pack.clear()
        self.cb_pack.addItems(names)
        act = pack.active_pack()
        if act in names:
            self.cb_pack.setCurrentText(act)
        elif pack.DEFAULT_PACK in names:
            self.cb_pack.setCurrentText(pack.DEFAULT_PACK)

    def cur_pack(self):
        return self.cb_pack.currentText() or pack.DEFAULT_PACK

    def pack_conf(self):
        """pack 级输出参数（画布 / fps / 格式 / 质量）——全部来自 manifest，不在导出页暴露。"""
        d = {"w": pack.DEFAULT_CANVAS[0], "h": pack.DEFAULT_CANVAS[1], "fps": 24, "fmt": "webp", "quality": 80}
        try:
            m = pack.load_manifest(self.cur_pack())
            cv, fr = m.get("canvas") or {}, m.get("frames") or {}
            d.update(w=int(cv.get("w", pack.DEFAULT_CANVAS[0])), h=int(cv.get("h", pack.DEFAULT_CANVAS[1])),
                     fps=int(fr.get("fps", 24)), fmt=fr.get("format", "webp"),
                     quality=int(fr.get("quality", 80)))
        except Exception:
            pass
        return d

    def pack_clip_ids(self):
        try:
            return {c["clipId"] for c in pack.load_manifest(self.cur_pack()).get("clips", [])}
        except Exception:
            return set()

    def new_pack(self):
        dlg = NewPackDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pid, role, canvas = dlg.values()
        try:
            pack.new_pack(pid, role, canvas)
        except FileExistsError:
            QMessageBox.warning(self, "已存在", "packs\\%s 已经存在，直接选它就行" % pid)
            return
        self.refresh_packs()
        self.cb_pack.setCurrentText(pid)
        self.log("新建素材包 packs/%s（角色：%s，画布 %d×%d）" % (pid, role, canvas[0], canvas[1]))

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

    def _refresh_item(self, row):
        v = self.videos[row]
        it = self.list.item(row) or QListWidgetItem("", self.list)
        info = "%s    [%s]" % (os.path.basename(v["path"]), v["cid"])
        if v["frames"]:
            info += "   %d 帧" % len(v["frames"])
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
            self.raw_rgb = None
            self.canvas.set_image(None)
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
        self.ed_id_manual.setText(v["cid"] if v.get("manual") else "")
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

    def apply_manual_id(self):
        row = self.list.currentRow()
        if not (0 <= row < len(self.videos)):
            return
        v = self.videos[row]
        cid = self.ed_id_manual.text().strip()
        if not cid:
            v["manual"] = False
            v["cid"] = suggest_cid(v["name"], {x["cid"] for x in self.videos if x is not v})
        elif not re.match(r"^[0-9A-Za-z_\-]+$", cid):
            self.log("手改 clipId 只能用字母/数字/下划线/短横线；已保持原值")
        elif any(x is not v and x["cid"] == cid for x in self.videos):
            self.log("clipId %s 已被列表里另一段占用；已保持原值" % cid)
        else:
            v["cid"] = cid
            v["manual"] = True
        self.ed_id.setText(v["cid"])
        self.ed_id_manual.setText(v["cid"] if v.get("manual") else "")
        self._refresh_item(row)

    def on_select(self, *a):
        row = self.list.currentRow()
        if not (0 <= row < len(self.videos)):
            return
        v = self.videos[row]
        self.ed_id.setText(v["cid"])
        self.ed_id_manual.setText(v["cid"] if v.get("manual") else "")
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
            self.lb_info.setText("%s\n%d 帧 @ %d fps · 源片 %s"
                                 % (v["name"], len(v["frames"]), fps, "%d×%d" % im.size))
            self._refresh_item(row)
            self.frame_sld.blockSignals(True)
            self.frame_sld.setRange(0, max(0, len(v["frames"]) - 1))
            if row != self._last_row:
                self.frame_sld.setValue(len(v["frames"]) // 2)
                self._last_row = row
                self.crop = None            # 换视频，旧的裁边结果作废
            self.frame_sld.blockSignals(False)
        except Exception as e:
            msg = "打不开这个视频：\n%s\n\n常见原因：文件被占用/损坏、不是视频、或 ffmpeg 抽不出帧。" % e
            self.log(msg)
            QMessageBox.warning(self, "视频打不开", msg)
            return
        if self.cb_crop.isChecked():
            self.auto_crop()
        self.render()

    # ---------- 自动裁黑边 ----------
    def auto_crop(self):
        v = self.cur()
        if not v:
            return
        probe = os.path.join(BASE, "..", "cache", "probe", v["cid"])
        try:
            files = build_frames.extract_frames(v["path"], probe, 1)
        except Exception as e:
            self.log("裁边失败（抽 1fps 探针帧出错）：%s" % e)
            return
        crop, detail = detect_crop(files)
        self.crop = crop
        if crop:
            self.log("自动裁边：%s → crop=%s（全片只裁一次，避免逐帧抖动）" % (detail, list(crop)))
        else:
            self.log("未发现明显暗边（%s），不裁" % detail)
        self.render()

    def on_crop_toggled(self, on):
        if on:
            self.auto_crop()
        else:
            self.crop = None
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

    def schedule_render(self, *a):
        self.render_timer.start(100)

    def params(self):
        p = default_params()
        p.update({k: self.rows[k].value() for k in self.rows})
        p["keep_frac"] = FIXED["keep_frac"] if self.cb_keep.isChecked() else 1.0
        return p

    def reset_adv(self):
        self.rows["hue_ref"].set_value(HUE_DEFAULT)
        for key, _n, _lo, _hi, val, _t in ADV_DEFS:
            self.rows[key].set_value(val)
        self.cb_keep.setChecked(True)
        self.log("高级参数已恢复默认")
        self.schedule_render()

    def render(self):
        v = self.cur()
        if not v:
            return
        i = min(self.frame_sld.value(), len(v["frames"]) - 1)
        t0 = time.monotonic()
        raw = Image.open(v["frames"][i]).convert("RGB")
        lim = 560
        s = min(1.0, lim / max(raw.size))
        if s < 1.0:
            raw = raw.resize((max(1, round(raw.width * s)), max(1, round(raw.height * s))),
                             Image.BILINEAR)
        self.raw_rgb = raw
        p = self.params()
        mode = self.view_mode.currentIndex()
        note = ""
        if mode == 0:
            out = raw.convert("RGBA")
        else:
            p = dict(p, watermarks=[[int(x0 * s), int(y0 * s), int(x1 * s), int(y1 * s)]
                                    for (x0, y0, x1, y1) in p["watermarks"]])
            rgba = key_frame(np.asarray(raw), **p)
            if mode == 2:
                a = np.asarray(rgba)[:, :, 3]
                out = Image.fromarray(a, "L").convert("RGB").convert("RGBA")
            else:
                out = rgba
            bg = float((np.asarray(rgba)[:, :, 3] < 8).mean())
            note = " · 透明 %.0f%%" % (bg * 100)
            if bg < 0.25:
                note += " ⚠ 背景可能没抠净：确认是纯绿背景，或调色相/饱和度"
        crop = tuple(int(round(x * s)) for x in self.crop) if self.crop else None
        self.canvas.set_image(pil_to_pixmap(out), raw.size, crop)
        dt = (time.monotonic() - t0) * 1000
        self.lb_perf.setText("%.0f ms · 预览 %d 长边%s" % (dt, lim, note))
        self.lb_frame.setText("%d / %d" % (i + 1, len(v["frames"])))

    def sample_hue(self):
        v = self.cur()
        if not v:
            self.log("先选一个视频")
            return
        i = min(self.frame_sld.value(), len(v["frames"]) - 1)
        rgb = np.asarray(Image.open(v["frames"][i]).convert("RGB")).astype(np.float32) / 255.0
        href, sat_med = sample_hue_from(rgb)
        self.rows["hue_ref"].set_value(href)
        self.adv_btn.setChecked(True)
        self.toggle_adv(True)
        self.log("四角背景：色相 %.1f°，饱和度中位数 %.2f" % (href, sat_med))
        if sat_med < 0.2:
            self.log("⚠ 四角不够绿：可能不是纯绿幕，切到 alpha 视图看边框是否干净")
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

    # ---------- 生成 ----------
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

    def generate(self):
        if self.gen and self.gen.isRunning():
            return
        items = self.collect_queue()
        if not items:
            self.log("先导入并选中至少一个视频")
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
        # 重抠同一段：旧帧备份到 .backup/，不产生 _v2 目录
        bak = pack.backup_frames(pack_id, cid)
        if bak:
            self.log("同 clipId 重抠：旧帧已备份到 %s（可回滚）" % os.path.relpath(bak, REPO))
        spec = {"id": cid, "video": v["path"], "params": self.params(),
                "fps": c["fps"], "width": c["w"], "height": c["h"],
                "fmt": c["fmt"], "quality": c["quality"],
                "crop": list(self.crop) if self.crop else None,
                "out_dir": pack.frames_dir(pack_id, cid)}
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
        v, cid, name = self.queue.pop(0)
        pack_id = self.cur_pack()
        h = r["height"]
        entry = {
            "clipId": cid, "displayName": name or cid,
            "type": "idle" if (cid == pack.load_manifest(pack_id).get("defaultIdle")) else "action",
            "loop": False, "priority": 20,
            "dir": "frames/" + cid, "fps": r["fps"], "count": r["count"],
            "scale": r["scale"], "offset": r["offset"],
            "anchor": {"foot": [0.5, round(r["foot"][1] / h, 4)]},
            "tags": [], "crop": r["crop"], "format": r["format"],
            "source": os.path.basename(v["path"]),
        }
        pack.upsert_clip(pack_id, entry)
        pack.sync_runtime(pack_id)
        q = self.qc(r["out_dir"], self.rows["hue_ref"].value())
        self.prog.setValue(100)
        self.lb_result.setText("已生成 %d 帧 · %.2f MB · 脚底锚点 y=%d · 残留绿边 %.2f%% · scale %.3f offset %s"
                               % (r["count"], r["bytes"] / 1e6, r["foot"][1], q["resid"], r["scale"],
                                  r["offset"]))
        self.log("%s 完成：%d 帧，%.2f MB，%d×%d；manifest + clips.json 已更新"
                 % (cid, r["count"], r["bytes"] / 1e6, r["width"], r["height"]))
        self.log("如需让它出现在某个状态，改 packs/%s/state-map.json 里那条规则的 clipId"
                 "（不自动改，避免覆盖你手选的动作）" % pack_id)
        self.show_compare(r["out_dir"])
        self._run_next()

    def qc(self, out_dir, hue):
        import glob
        fs = sorted(glob.glob(os.path.join(out_dir, "f*.webp"))) or \
             sorted(glob.glob(os.path.join(out_dir, "f*.png")))
        if not fs:
            return {"resid": 0.0}
        step = max(1, len(fs) // 8)
        vals = []
        for p in fs[::step]:
            im = Image.open(p).convert("RGBA")
            a = np.asarray(im)[..., 3]
            hsv = np.asarray(im.convert("HSV"))
            hh = hsv[..., 0].astype(np.int32) * 360 // 255
            ss = hsv[..., 1].astype(np.float32) / 255
            fg = a > 40
            if fg.any():
                dh = np.abs(((hh - hue + 180) % 360) - 180)
                vals.append(float(((dh < 30) & (ss > 0.3) & fg).sum()) / fg.sum() * 100)
        return {"resid": float(np.median(vals)) if vals else 0.0}

    def show_compare(self, out_dir):
        """新素材首帧 vs 当前 pack 的待机帧，并排显示，防止角色突然变大。"""
        import glob
        fs = sorted(glob.glob(os.path.join(out_dir, "f*.webp"))) or \
             sorted(glob.glob(os.path.join(out_dir, "f*.png")))
        if not fs:
            return
        try:
            data = pack.load_manifest(self.cur_pack())
            idle = data.get("defaultIdle")
            ref_dir = pack.frames_dir(self.cur_pack(), idle) if idle else None
            if ref_dir and not os.path.isdir(ref_dir):
                ref_dir = os.path.join(ROOT, "frames_" + (idle or ""))
            ref = sorted(glob.glob(os.path.join(ref_dir, "f*.webp")))
            ref = ref or sorted(glob.glob(os.path.join(ref_dir, "f*.png")))
            a = Image.open(fs[0]).convert("RGBA")
            b = Image.open(ref[0]).convert("RGBA") if ref else None
            if b:
                w = 240
                ha = max(1, round(a.height * w / a.width))
                hb = max(1, round(b.height * w / b.width))
                canvas = Image.new("RGBA", (w * 2 + 8, max(ha, hb)), (32, 32, 37, 255))
                canvas.alpha_composite(a.resize((w, ha), Image.LANCZOS), (0, 0))
                canvas.alpha_composite(b.resize((w, hb), Image.LANCZOS), (w + 8, 0))
                self.cmp.setPixmap(pil_to_pixmap(canvas))
                self.cmp.setText("")
            else:
                self.cmp.setPixmap(pil_to_pixmap(a))
        except Exception:
            self.log("对比图生成失败：\n" + traceback.format_exc())

    def on_gen_fail(self, s):
        self.log("生成失败：\n" + s)
        self.queue = []
        self.b_gen.setEnabled(True)
        self.b_cancel.setEnabled(False)

    def cancel(self):
        self.queue = []
        self.log("已取消：当前这一段会写完再停（中间帧不能中断），后续排队已清空")
        self.b_cancel.setEnabled(False)

    def open_out(self):
        d = pack.frames_dir(self.cur_pack(), "")
        d = os.path.dirname(d)
        if os.path.isdir(d):
            os.startfile(d)
        else:
            self.log("输出目录还不存在：%s" % d)

    # ---------- 日志 ----------
    def log(self, s):
        self.log_box.appendPlainText(s)
        self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())


def main():
    app = QApplication(sys.argv)
    w = Workshop()
    w.show()
    w.refresh_presets()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        with open(os.path.join(BASE, "..", "cache", "tmp", "workshop.log"), "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        raise
