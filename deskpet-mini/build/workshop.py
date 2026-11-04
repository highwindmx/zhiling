"""素材工坊：把绿幕视频抠成透明帧序列 frames_<clipId>，并写 meta.json。

单文件 PyQt6。抠像全部调用 build/keyer.py 的 key_frame()，生成调用 build/build_frames.py 的
build_clip()；本文件不复制任何抠像逻辑。
"""
import json, os, re, sys, time, traceback
import numpy as np
from PIL import Image
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor
from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QListWidget, QListWidgetItem,
                             QPushButton, QHBoxLayout, QVBoxLayout, QSlider, QDoubleSpinBox,
                             QComboBox, QSpinBox, QPlainTextEdit, QProgressBar, QSplitter,
                             QFileDialog, QSizePolicy, QCheckBox, QLineEdit, QInputDialog)

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)
from keyer import key_frame
import build_frames

EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")
PREVIEW_MAX = 640         # 完整预览最长边
PREVIEW_FAST = 320        # 拖动中用的快速预览最长边（面积约为完整的 1/4）
DEBOUNCE_MS = 120

# 暴露给界面的 7 个核心参数：key -> (名称, 下限, 上限, 默认值, tooltip)
PARAM_DEFS = [
    ("hue_ref", "背景色相", 0, 360, 136.0,
     "画面里绿幕的色相（0-360 度）。纯绿约 120-150；用“取背景色”自动填。"),
    ("hue_in", "色相内", 0, 90, 20.0, "与背景色相差在这个角度以内 → 完全判为背景。"),
    ("hue_out", "色相外", 0, 120, 34.0, "超出这个角度 → 完全判为前景；两者之间是软过渡。"),
    ("sat_lo", "饱和度下限", 0, 1, 0.30, "饱和度低于此值的像素不算背景（避免吃掉角色灰白衣物）。"),
    ("sat_hi", "饱和度上限", 0, 1, 0.55, "饱和度高于此值才“够绿”；与下限之间是软过渡。"),
    ("floor", "alpha 软地板", 0, 0.95, 0.16, "回收淡绿雾：alpha 低于此值直接归零。"),
    ("despill", "去溢色强度", 0, 1, 0.75, "削掉绿边（绿比红蓝高出的部分按此比例扣掉）。"),
    ("blur", "羽化", 0, 8, 1.0, "alpha 边缘高斯模糊半径（像素），越大边缘越柔。"),
]
# 界面不暴露、沿用既有默认值的参数（keep_frac 由“只保留最大连通块”开关决定）
FIXED = dict(val_lo=0.30, val_hi=0.45, erode=1, watermarks=[])
PRESET_DIR = os.path.join(ROOT, "presets")


def pil_to_pixmap(im):
    im = im.convert("RGBA")
    q = QImage(im.tobytes("raw", "RGBA"), im.width, im.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(q.copy())


def make_cid(path, used):
    base = os.path.splitext(os.path.basename(path))[0]
    s = re.sub(r"[^0-9a-zA-Z]+", "_", base).strip("_").lower()
    s = s[-24:].strip("_") or "clip"
    cid, n = s, 1
    while cid in used:
        n += 1
        cid = "%s_%d" % (s, n)
    return cid


class Canvas(QWidget):
    """棋盘格背景 + 居中等比缩放的预览图。"""

    def __init__(self):
        super().__init__()
        self.pix = None
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_image(self, pix):
        self.pix = pix
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
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "选择左侧视频后可预览")
            return
        w, h = self.pix.width(), self.pix.height()
        s = min(self.width() / w, self.height() / h)
        rw, rh = max(1, int(w * s)), max(1, int(h * s))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawPixmap((self.width() - rw) // 2, (self.height() - rh) // 2, rw, rh, self.pix)


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
        lab.setFixedWidth(74)
        lab.setToolTip(tip)
        self.sld = QSlider(Qt.Orientation.Horizontal)
        self.sld.setRange(0, 1000)
        self.sld.setMinimumWidth(110)
        self.sld.setToolTip(tip)
        self.spb = QDoubleSpinBox()
        self.spb.setRange(lo, hi)
        self.spb.setDecimals(2 if hi <= 1 else 1)
        self.spb.setSingleStep(round((hi - lo) / 100, 3))
        self.spb.setFixedWidth(96)
        self.spb.setToolTip(tip)
        lay.addWidget(lab)
        lay.addWidget(self.sld, 1)
        lay.addWidget(self.spb)
        self.lb_fx = QLabel("—")            # 该参数能影响多少像素（低分辨率估算）
        self.lb_fx.setFixedWidth(40)
        self.lb_fx.setStyleSheet("color:#888")
        self.lb_fx.setToolTip("把该参数往一侧推 12% 量程后，alpha 变化超过 6 的像素占比")
        lay.addWidget(self.lb_fx)
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


class GenThread(QThread):
    msg = pyqtSignal(str)
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, spec):
        super().__init__()
        self.spec = spec

    def run(self):
        try:
            self.done.emit(build_frames.build_clip(self.spec, log=self.msg.emit))
        except Exception:
            self.failed.emit(traceback.format_exc())


class Workshop(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("素材工坊 — 绿幕视频转透明帧序列")
        self.resize(1280, 860)
        self.videos = []          # [{path, cid, frames, fps}]
        self._last_row = -1
        self.raw_rgb = None       # 当前帧的 RGB（预览尺寸）
        self.prev_scale = 1.0
        self.t = None
        self.gen = None
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(lambda: self.render(True))
        self.full_timer = QTimer(self)        # 停手 400ms 后补一次完整分辨率预览
        self.full_timer.setSingleShot(True)
        self.full_timer.timeout.connect(lambda: self.render(False))
        self.impact_timer = QTimer(self)      # 受影响像素统计比渲染更贵，单独再等 250ms
        self.impact_timer.setSingleShot(True)
        self.impact_timer.timeout.connect(self.compute_impact)
        self._build_ui()
        self.refresh_presets()

    # ---- 界面 ----
    def _build_ui(self):
        outer = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        # 左：视频列表
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("视频（可拖入，支持多选）"))
        self.list = VideoList()
        self.list.dropped.connect(self.add_files)
        self.list.currentRowChanged.connect(self.on_select)
        lv.addWidget(self.list, 1)
        rowb = QHBoxLayout()
        b_add = QPushButton("添加视频…")
        b_add.clicked.connect(self.pick_files)
        b_del = QPushButton("移除")
        b_del.clicked.connect(self.remove_cur)
        rowb.addWidget(b_add)
        rowb.addWidget(b_del)
        lv.addLayout(rowb)
        self.lb_info = QLabel("")
        self.lb_info.setWordWrap(True)
        self.lb_info.setStyleSheet("color:#888")
        lv.addWidget(self.lb_info)
        split.addWidget(left)

        # 中：预览
        mid = QWidget()
        mv = QVBoxLayout(mid)
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
        split.addWidget(mid)

        # 右：参数
        right = QWidget()
        right.setMinimumWidth(400)
        rv = QVBoxLayout(right)
        rv.addWidget(QLabel("抠像参数"))
        pre_row = QHBoxLayout()
        pre_row.addWidget(QLabel("预设"))
        self.cb_preset = QComboBox()
        self.cb_preset.setToolTip("选一个就载入它的参数；存为… 会把当前参数写进 presets\\*.json")
        self.cb_preset.currentIndexChanged.connect(self.load_preset)
        pre_row.addWidget(self.cb_preset, 1)
        b_save = QPushButton("存为…")
        b_save.clicked.connect(self.save_preset)
        pre_row.addWidget(b_save)
        rv.addLayout(pre_row)
        self.rows = {}
        for key, name, lo, hi, val, tip in PARAM_DEFS:
            r = SliderRow(name, lo, hi, val, tip)
            r.changed.connect(self.schedule_render)
            rv.addWidget(r)
            self.rows[key] = r
        b_hue = QPushButton("取背景色（四角取样）")
        b_hue.clicked.connect(self.sample_hue)
        rv.addWidget(b_hue)
        self.cb_keep = QCheckBox("只保留最大连通块")
        self.cb_keep.setChecked(True)
        self.cb_keep.setToolTip("开：只留最大的一块前景，气泡/碎边一并丢掉；关：保留全部碎片")
        self.cb_keep.stateChanged.connect(self.schedule_render)
        rv.addWidget(self.cb_keep)
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([240, 620, 420])

        # 底：输出设置 + 生成
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("宽度"))
        self.sp_w = QSpinBox()
        self.sp_w.setRange(192, 1024)
        self.sp_w.setSingleStep(32)
        self.sp_w.setValue(576)
        out_row.addWidget(self.sp_w)
        out_row.addWidget(QLabel("帧率"))
        self.sp_fps = QSpinBox()
        self.sp_fps.setRange(6, 60)
        self.sp_fps.setValue(24)
        out_row.addWidget(self.sp_fps)
        out_row.addWidget(QLabel("格式"))
        self.cb_fmt = QComboBox()
        self.cb_fmt.addItems(["webp", "png"])
        out_row.addWidget(self.cb_fmt)
        out_row.addWidget(QLabel("质量"))
        self.sp_q = QSpinBox()
        self.sp_q.setRange(40, 100)
        self.sp_q.setValue(80)
        out_row.addWidget(self.sp_q)
        self.b_gen = QPushButton("生成帧序列")
        self.b_gen.clicked.connect(self.generate)
        out_row.addWidget(self.b_gen)
        out_row.addStretch(1)
        outer.addLayout(out_row)

        reg_row = QHBoxLayout()
        self.cb_reg = QCheckBox("登记到 clips.json")
        self.cb_reg.setChecked(True)
        self.cb_reg.setToolTip("生成成功后把这个 clip 写进 clips.json；重启宠物才生效")
        reg_row.addWidget(self.cb_reg)
        reg_row.addWidget(QLabel("clipId"))
        self.ed_id = QLineEdit()
        self.ed_id.setFixedWidth(150)
        self.ed_id.setPlaceholderText("如 action3")
        reg_row.addWidget(self.ed_id)
        reg_row.addWidget(QLabel("显示名"))
        self.ed_name = QLineEdit()
        self.ed_name.setFixedWidth(150)
        self.ed_name.setPlaceholderText("右键菜单里显示的名字")
        reg_row.addWidget(self.ed_name)
        reg_row.addWidget(QLabel("类型"))
        self.cb_type = QComboBox()
        self.cb_type.addItems(["action", "idle"])
        reg_row.addWidget(self.cb_type)
        self.cb_loop = QCheckBox("循环")
        self.cb_loop.setToolTip("idle 必须循环；action 一般关掉（播完自动回待机）")
        reg_row.addWidget(self.cb_loop)
        reg_row.addWidget(QLabel("优先级"))
        self.sp_prio = QSpinBox()
        self.sp_prio.setRange(0, 99)
        self.sp_prio.setValue(4)
        self.sp_prio.setToolTip("双击时按这个从小到大依次播放动作")
        reg_row.addWidget(self.sp_prio)
        reg_row.addStretch(1)
        outer.addLayout(reg_row)

        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setValue(0)
        outer.addWidget(self.prog)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(130)
        outer.addWidget(self.log_box)

    # ---- 文件 ----
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
            cid = make_cid(p, used)
            used.add(cid)
            self.videos.append({"path": p, "cid": cid, "frames": [], "fps": None})
            QListWidgetItem("%s    [%s]" % (os.path.basename(p), cid), self.list)
        self.log("添加 %d 个视频，共 %d 个" % (len(paths), len(self.videos)))
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)

    def remove_cur(self):
        rows = sorted({i.row() for i in self.list.selectedIndexes()}, reverse=True)
        for r in rows:
            self.list.takeItem(r)
            del self.videos[r]
        if self.videos:
            self.list.setCurrentRow(min(rows[-1] if rows else 0, len(self.videos) - 1))
        else:
            self.raw_rgb = None
            self.canvas.set_image(None)

    def on_select(self, row):
        if row < 0 or row >= len(self.videos):
            return
        v = self.videos[row]
        self.ed_id.setText(v["cid"])
        fps = self.sp_fps.value()
        try:
            if v["fps"] != fps or not v["frames"]:
                raw = os.path.join(BASE, "raw_" + v["cid"])
                if os.path.isdir(raw) and v["fps"] != fps:
                    import glob
                    for f in glob.glob(os.path.join(raw, "*.png")):
                        os.remove(f)
                t0 = time.monotonic()
                v["frames"] = build_frames.extract_frames(v["path"], raw, fps)
                v["fps"] = fps
                self.log("抽帧：%s → %d 帧（%d fps，%.1fs）"
                         % (os.path.basename(v["path"]), len(v["frames"]), fps,
                            time.monotonic() - t0))
            self.lb_info.setText("%s\n%d 帧 @ %d fps" % (v["cid"], len(v["frames"]), fps))
            self.frame_sld.blockSignals(True)
            self.frame_sld.setRange(0, max(0, len(v["frames"]) - 1))
            if row != self._last_row:          # 换了视频才跳中间帧，不打断当前浏览位置
                self.frame_sld.setValue(len(v["frames"]) // 2)
                self._last_row = row
            self.frame_sld.blockSignals(False)
            self.render()
        except Exception as e:
            self.log("打不开这个视频：%s" % e)
            self.lb_info.setText("读取出错，换一个文件试试")

    # ---- 预览 ----
    def cur(self):
        r = self.list.currentRow()
        if 0 <= r < len(self.videos) and self.videos[r]["frames"]:
            return self.videos[r]
        return None

    def on_frame(self, i):
        v = self.cur()
        if v:
            i = min(i, len(v["frames"]) - 1)
            self.lb_frame.setText("%d / %d" % (i + 1, len(v["frames"])))
            self.schedule_render()

    def schedule_render(self, *a):
        self.render_timer.start(DEBOUNCE_MS)    # 拖动中先给快速预览
        self.full_timer.start(400)              # 停手后再渲染完整预览

    def params(self):
        p = {k: self.rows[k].value() for k, _, _, _, _, _ in PARAM_DEFS}
        p.update(FIXED)
        p["keep_frac"] = 1.0 if self.cb_keep.isChecked() else 0.0
        return p

    def compute_impact(self):
        """每个参数往一侧推 12% 量程，看 alpha 变化像素占比（低分辨率估算，成本约 10ms/参数）。"""
        if self.raw_rgb is None or self.view_mode.currentIndex() == 0:
            for r in self.rows.values():
                r.lb_fx.setText("—")
            return
        small = self.raw_rgb.copy()
        small.thumbnail((200, 200), Image.BILINEAR)
        rgb = np.asarray(small)
        p = self.params()
        a0 = np.asarray(key_frame(rgb, **p))[:, :, 3].astype(np.int16)
        for key, _, lo, hi, _, _ in PARAM_DEFS:
            d = 0.12 * (hi - lo)
            v = p[key] + d if p[key] + d <= hi else max(lo, p[key] - d)
            a1 = np.asarray(key_frame(rgb, **dict(p, **{key: v})))[:, :, 3].astype(np.int16)
            self.rows[key].lb_fx.setText("%.1f%%" % (float((np.abs(a1 - a0) > 6).mean()) * 100))

    def render(self, fast=False):
        v = self.cur()
        if not v:
            return
        i = min(self.frame_sld.value(), len(v["frames"]) - 1)
        t0 = time.monotonic()
        raw = Image.open(v["frames"][i]).convert("RGB")
        lim = PREVIEW_FAST if fast else PREVIEW_MAX
        s = min(1.0, lim / max(raw.size))
        self.prev_scale = s
        if s < 1.0:
            raw = raw.resize((max(1, round(raw.width * s)), max(1, round(raw.height * s))),
                             Image.BILINEAR)
        self.raw_rgb = raw
        p = self.params()
        mode = self.view_mode.currentIndex()
        if mode == 0:
            out = raw.convert("RGBA")
        else:
            wm = [[int(x0 * s), int(y0 * s), int(x1 * s), int(y1 * s)]
                  for (x0, y0, x1, y1) in p["watermarks"]]
            p = dict(p, watermarks=wm)
            rgba = key_frame(np.asarray(raw), **p)
            if mode == 2:
                a = np.asarray(rgba)[:, :, 3]
                out = Image.fromarray(a, "L").convert("RGB").convert("RGBA")
            else:
                out = rgba
        self.canvas.set_image(pil_to_pixmap(out))
        dt = (time.monotonic() - t0) * 1000
        self.lb_perf.setText("%s %.0f ms · %.1f fps（预览 %d 长边）"
                             % ("快速" if fast else "完整", dt, 1000.0 / dt, lim))
        self.lb_frame.setText("%d / %d" % (i + 1, len(v["frames"])))
        self.impact_timer.start(250)

    def sample_hue(self):
        v = self.cur()
        if not v:
            self.log("先选一个视频")
            return
        i = min(self.frame_sld.value(), len(v["frames"]) - 1)
        rgb = np.asarray(Image.open(v["frames"][i]).convert("RGB")).astype(np.float32) / 255.0
        h, w, _ = rgb.shape
        k = max(8, min(h, w) // 12)
        corners = [rgb[:k, :k], rgb[:k, -k:], rgb[-k:, :k], rgb[-k:, -k:]]
        px = np.concatenate([c.reshape(-1, 3) for c in corners])
        r, g, b = px[:, 0], px[:, 1], px[:, 2]
        mx, mn = px.max(1), px.min(1)
        sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
        d = mx - mn + 1e-6
        hue = np.where(mx == r, ((g - b) / d) % 6,
              np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60
        pick = sat >= np.percentile(sat, 60)          # 只看四角里较饱和的那批像素
        href = float(np.median(hue[pick]))
        self.rows["hue_ref"].set_value(href)
        self.log("四角背景：色相 %.1f 度，饱和度中位数 %.2f" % (href, float(np.median(sat[pick]))))
        self.schedule_render()

    # ---- 预设 ----
    def refresh_presets(self):
        os.makedirs(PRESET_DIR, exist_ok=True)
        names = sorted(os.path.splitext(f)[0] for f in os.listdir(PRESET_DIR)
                       if f.endswith(".json"))
        self.cb_preset.blockSignals(True)
        self.cb_preset.clear()
        self.cb_preset.addItems(names)
        self.cb_preset.setCurrentIndex(-1)      # 不预选，否则再点同一项不会触发载入
        self.cb_preset.setPlaceholderText("（未选）")
        self.cb_preset.blockSignals(False)

    def save_preset(self):
        name, ok = QInputDialog.getText(self, "存为预设", "预设名（会存成 presets\\<名>.json）")
        if not ok or not name.strip():
            return
        name = re.sub(r'[\\/:*?"<>|]', "_", name.strip())
        data = {"params": {k: self.rows[k].value() for k, _, _, _, _, _ in PARAM_DEFS},
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

    # ---- 生成 ----
    def generate(self):
        v = self.cur()
        if not v:
            self.log("先选一个视频")
            return
        if self.gen and self.gen.isRunning():
            return
        cid = self.ed_id.text().strip() or v["cid"]
        if not re.match(r"^[0-9A-Za-z_\-]+$", cid):
            self.log("clipId 只能用字母/数字/下划线/短横线：%r" % cid)
            return
        v["cid"] = cid
        spec = {"id": v["cid"], "video": v["path"], "params": self.params(),
                "fps": self.sp_fps.value(), "width": self.sp_w.value(),
                "fmt": self.cb_fmt.currentText(), "quality": self.sp_q.value()}
        self.log("开始生成 %s：%d fps，宽 %d，%s q%d"
                 % (v["cid"], spec["fps"], spec["width"], spec["fmt"], spec["quality"]))
        self.prog.setValue(0)
        self.b_gen.setEnabled(False)
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
        self.b_gen.setEnabled(True)
        self.prog.setValue(100)
        self.log("完成：frames_%s 共 %d 帧，%.2f MB，%d×%d；meta.json 已更新"
                 % (r["id"], r["count"], r["bytes"] / 1e6, r["width"], r["height"]))
        if self.cb_reg.isChecked():
            try:
                self.register(r["id"])
            except Exception:
                self.log("写 clips.json 失败：\n" + traceback.format_exc())

    def register(self, cid):
        """把 clip 写进 clips.json（同 id 覆盖）。新 clip 要重启宠物才生效。"""
        path = os.path.join(ROOT, "clips.json")
        data = {"version": 1, "clips": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        arr = data.setdefault("clips", [])
        typ = self.cb_type.currentText()
        entry = {"id": cid, "name": self.ed_name.text().strip() or cid, "type": typ,
                 "loop": bool(self.cb_loop.isChecked()) or typ == "idle",
                 "priority": int(self.sp_prio.value()), "dir": "frames_" + cid}
        for i, old in enumerate(arr):
            if old.get("id") == cid:
                arr[i] = dict(old, **entry)
                break
        else:
            arr.append(entry)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.log("已写入 clips.json：%s（%s，优先级 %d，循环 %s）；重启宠物（双击 run.bat）生效"
                 % (cid, typ, entry["priority"], "是" if entry["loop"] else "否"))

    def on_gen_fail(self, s):
        self.b_gen.setEnabled(True)
        self.log("生成失败：\n" + s)

    # ---- 日志 ----
    def log(self, s):
        self.log_box.appendPlainText(s)
        self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())


def main():
    app = QApplication(sys.argv)
    w = Workshop()
    w.show()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        with open(os.path.join(BASE, "workshop.log"), "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        raise
