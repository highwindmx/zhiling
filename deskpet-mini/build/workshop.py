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
                             QFileDialog, QSizePolicy)

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, BASE)
from keyer import key_frame
import build_frames

EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")
PREVIEW_MAX = 640          # 预览最长边（半分辨率预览，生成时才用全分辨率）
DEBOUNCE_MS = 120

# 暴露给界面的 7 个核心参数：key -> (名称, 下限, 上限, 默认值, tooltip)
PARAM_DEFS = [
    ("hue_ref", "背景色相", 0, 360, 136.0,
     "画面里绿幕的色相（0-360 度）。纯绿约 120-150；用“取背景色”自动填。"),
    ("hue_in", "色相内", 0, 90, 20.0, "与背景色相差在这个角度以内 → 完全判为背景。"),
    ("hue_out", "色相外", 0, 120, 34.0, "超出这个角度 → 完全判为前景；两者之间是软过渡。"),
    ("sat_lo", "饱和度下限", 0, 1, 0.30, "饱和度低于此值的像素不算背景（避免吃掉角色灰白衣物）。"),
    ("floor", "alpha 软地板", 0, 0.95, 0.16, "回收淡绿雾：alpha 低于此值直接归零。"),
    ("despill", "去溢色强度", 0, 1, 0.75, "削掉绿边（绿比红蓝高出的部分按此比例扣掉）。"),
    ("blur", "羽化", 0, 8, 1.0, "alpha 边缘高斯模糊半径（像素），越大边缘越柔。"),
]
# 界面不暴露、沿用既有默认值的参数
FIXED = dict(sat_hi=0.55, val_lo=0.30, val_hi=0.45, erode=1, keep_frac=1.0, watermarks=[])


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
        self.render_timer.timeout.connect(self.render)
        self._build_ui()

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
        rv = QVBoxLayout(right)
        rv.addWidget(QLabel("抠像参数"))
        self.rows = {}
        for key, name, lo, hi, val, tip in PARAM_DEFS:
            r = SliderRow(name, lo, hi, val, tip)
            r.changed.connect(self.schedule_render)
            rv.addWidget(r)
            self.rows[key] = r
        b_hue = QPushButton("取背景色（四角取样）")
        b_hue.clicked.connect(self.sample_hue)
        rv.addWidget(b_hue)
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([240, 660, 380])

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
        self.render_timer.start(DEBOUNCE_MS)

    def params(self):
        p = {k: self.rows[k].value() for k, _, _, _, _, _ in PARAM_DEFS}
        p.update(FIXED)
        return p

    def render(self):
        v = self.cur()
        if not v:
            return
        i = min(self.frame_sld.value(), len(v["frames"]) - 1)
        t0 = time.monotonic()
        raw = Image.open(v["frames"][i]).convert("RGB")
        s = min(1.0, PREVIEW_MAX / max(raw.size))
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
        self.lb_perf.setText("%.0f ms · %.1f fps（预览 %d 长边）" % (dt, 1000.0 / dt, PREVIEW_MAX))
        self.lb_frame.setText("%d / %d" % (i + 1, len(v["frames"])))

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

    # ---- 生成 ----
    def generate(self):
        v = self.cur()
        if not v:
            self.log("先选一个视频")
            return
        if self.gen and self.gen.isRunning():
            return
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
