"""一次性把 pet.py 从 v1 改到 packs v2（主题索引 + 动画自描述 + B+预判画布）。

按「起止标记」切块替换，每块都断言标记唯一；任何一处不匹配就整体不写盘。
用法：python build/tools/apply_pet_v2.py [--apply]
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
TARGET = os.path.join(ROOT, "pet.py")


def cut(start, end, new, s, label):
    i = s.find(start)
    if i < 0:
        raise RuntimeError("%s: 找不到起始标记 %r" % (label, start[:60]))
    j = s.find(end, i + len(start))
    if j < 0:
        raise RuntimeError("%s: 找不到结束标记 %r" % (label, end[:60]))
    return s[:i] + new + s[j:]


def rep(old, new, s, label, count=1):
    n = s.count(old)
    if n != count:
        raise RuntimeError("%s: 期望 %d 处，实际 %d 处：%r" % (label, count, n, old[:70]))
    return s.replace(old, new)


# ---------------------------------------------------------------------------
# 1) import 补两个控件
# ---------------------------------------------------------------------------
B_IMPORT_OLD = """                             QListWidget, QListWidgetItem, QStyledItemDelegate,
                             QStyleOptionViewItem, QStyle, QSizePolicy)"""
B_IMPORT_NEW = """                             QListWidget, QListWidgetItem, QStyledItemDelegate,
                             QStyleOptionViewItem, QStyle, QSizePolicy,
                             QLineEdit, QMessageBox, QAbstractItemView)"""

# ---------------------------------------------------------------------------
# 2) 状态常量：11 行
# ---------------------------------------------------------------------------
B_CONST_START = "# 设置对话框里 10 个状态的中文名 + 出厂默认"
B_CONST_END = "PREFETCH = 8"
B_CONST_NEW = '''# ---- v2：11 行状态（long_idle 拆两档），中文名 / 默认优先级统一来自 pack v2 ----
STATE_KEYS = packlib.STATE_KEYS
STATE_CN = dict(packlib.STATE_CN_V2)
DEFAULT_MAP = {st: ("", packlib.DEFAULT_PRIORITY[st]) for st in STATE_KEYS}
'''

# ---------------------------------------------------------------------------
# 3) 数据层：active_pack → frames_dir 整段重写
# ---------------------------------------------------------------------------
B_DATA_START = "def active_pack():"
B_DATA_END = "def resolve_ext(clip):"
B_DATA_NEW = '''def active_pack():
    """v2：当前主题 id（packs/active.json 的 theme）。"""
    return packlib.active_theme()


PACK_ID = active_pack()
PACK_INFO = {}
PACK_DIR = ""
STATE_MAP_PATH = ""


def _set_pack(pid):
    """统一解析：主题索引路径、idle 段、初始画布全部来自 v2 数据层。"""
    global PACK_ID, PACK_DIR, STATE_MAP_PATH, PACK_INFO
    PACK_ID = pid or packlib.active_theme()
    PACK_DIR = packlib.packs_root()   # 素材目录统一解析（环境变量 / config / Documents / 就地）
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
        raise SystemExit("\\n".join(lines))


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


'''

# ---------------------------------------------------------------------------
# 4) Loader 缩放条件：与 _load_now 对齐
# ---------------------------------------------------------------------------
B_LOADER_OLD = """            if w and img.width() != w:
                img = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)"""
B_LOADER_NEW = """            # 与 _load_now 完全对齐：按 (w,h) 一起判断。
            # v1 只判宽度，「宽同高不同」时 Loader 不缩放而 _load_now 会拉高，两条路径分叉。
            if w and (img.width() != w or img.height() != h):
                img = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)"""

# ---------------------------------------------------------------------------
# 5) Pet.__init__ 画布初值
# ---------------------------------------------------------------------------
B_INIT_OLD = """        cw, ch, cv_src = pack_canvas()
        self.canvas = (cw, ch)
        self.canvas_src = cv_src
        self.aspect = ch / cw
        self.width_px = width
        self.resize(width, round(width * self.aspect))"""
B_INIT_NEW = """        cw, ch, cv_src = pack_canvas(IDLE_ID)
        self.canvas = (cw, ch)
        self.canvas_wh = (cw, ch)      # 整数对：B 方案窗口比例唯一来源，绝不存浮点 aspect
        self.canvas_src = cv_src
        self.width_px = width
        self.resize(width, packlib.display_height(width, {"w": cw, "h": ch}))"""

# ---------------------------------------------------------------------------
# 6) _size / _load_now / _resize_to + 新增 _apply_canvas
# ---------------------------------------------------------------------------
B_SIZE_START = "    def _size(self):"
B_SIZE_END = "    # ---- 点击穿透：降采样 alpha 掩码 ----"
B_SIZE_NEW = '''    def _size(self):
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

'''

# ---------------------------------------------------------------------------
# 7) _switch：加 B+预判
# ---------------------------------------------------------------------------
B_SWITCH_START = "    def _switch(self, clip, loop=None, on_end=None):"
B_SWITCH_END = "    # ---- 状态映射对接 ----"
B_SWITCH_NEW = '''    def _switch(self, clip, loop=None, on_end=None):
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

'''

# ---------------------------------------------------------------------------
# 8) 右键菜单：主题子菜单 + 统一切换入口
# ---------------------------------------------------------------------------
B_MENU_START = "    def _build_menu(self):"
B_MENU_END = "    def contextMenuEvent(self, e):"
B_MENU_NEW = '''    def theme_menu(self, parent):
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
        """设置窗与右键菜单唯一共用的切换入口：active.json 是唯一真相。"""
        if not tid or tid == PACK_ID:
            return
        packlib.set_active_theme(tid)
        self.apply_pack(tid, reason="theme-switch")

    def _build_menu(self):
        """桌宠右键：动作 ▸ / 尺寸 ▸ / 主题 ▸ / 设置… / 退出。"""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        menu.addMenu(self.actions_menu(menu))
        menu.addMenu(self.size_menu(menu))
        menu.addMenu(self.theme_menu(menu))
        menu.addSeparator()
        set_act = QAction("设置…", self)
        set_act.triggered.connect(self.open_settings)
        menu.addAction(set_act)
        quit_act = QAction("退出", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(quit_act)
        return menu

'''

# ---------------------------------------------------------------------------
# 9) apply_pack → v2 主题切换
# ---------------------------------------------------------------------------
B_APPLY_START = '    def apply_pack(self, pid, reason=""):'
B_APPLY_END = "    def open_settings(self):"
B_APPLY_NEW = '''    def apply_pack(self, pid, reason=""):
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

'''

# ---------------------------------------------------------------------------
# 10) ActivePackWatcher
# ---------------------------------------------------------------------------
B_WATCH_START = "class ActivePackWatcher:"
B_WATCH_END = "class ClipPlayer(QWidget):"
B_WATCH_NEW = '''class ActivePackWatcher:
    """监听 packs/active.json 的 theme：切主题后桌宠不重启就换装。

    只比 mtime_ns/size 签名；--pack 显式指定的会话不实例化本类（命令行优先）。
    """

    def __init__(self, pet, interval_ms=1000):
        self.pet = pet
        self.path = packlib.active_json_path()
        self._sig = self._stat()
        self.timer = QTimer(pet)
        self.timer.timeout.connect(self.poll)
        self.timer.start(max(500, int(interval_ms)))

    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def poll(self):
        sig = self._stat()
        if sig is None or sig == self._sig:
            return
        self._sig = sig
        tid = packlib.active_theme()
        if tid and tid != PACK_ID:
            log("active.json 变化：切换主题 %s → %s" % (PACK_ID, tid))
            self.pet.apply_pack(tid, reason="active.json")


'''

# ---------------------------------------------------------------------------
# 11) SettingsDialog 全量重写
# ---------------------------------------------------------------------------
B_DLG_START = "class SettingsDialog(QDialog):"
B_DLG_END = "def setup_tray(app, pet):"
B_DLG_NEW = '''class AssetPicker(QDialog):
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
        self.lb_info.setStyleSheet("color:#8a8a95")
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


class SettingsDialog(QDialog):
    """v2 设置窗：主题切换 + 11 行状态 × 0~N 段动作池 + 全局动作库 + 工坊闭环。

    V5 契约：
    - 主题条常驻顶部；切换时若有未保存改动先确认；
    - 左栏 11 行状态（越靠上越优先，可拖拽/上下移，勾选启用）；
    - 右栏是选中状态的 clipIds[]，顺序 = 顺序轮播顺序，可增删/排序/清空；
    - 零绑定是一等公民；idle 是 UI 软约束（不允许清空，手写 JSON 不拦）；
    - 「还有 N 个状态没有动画」引导条 + 「＋ 用视频做一段新动作」带目标状态进工坊；
    - 保存写 packs/themes/<id>.json，保留全部调度字段，priority 按列表顺序等距重算。
    """

    def __init__(self, pet, parent=None):
        super().__init__(parent if parent is not None else pet)
        self.pet = pet
        self.theme_id = PACK_ID
        self.changed = False
        self.switched_theme = ""
        self._loading = False
        self._editing_state = None
        self._rows = {}          # state -> rule dict（保留主题里的全部字段）
        self._items = {}
        self.setWindowTitle("智灵 · 桌宠 设置")
        self.setModal(True)
        avail = QApplication.primaryScreen().availableGeometry()
        self.setMaximumSize(int(avail.width() * 0.90), int(avail.height() * 0.85))
        self.setMinimumSize(min(940, int(avail.width() * 0.90)), min(640, int(avail.height() * 0.85)))
        self.resize(min(1120, int(avail.width() * 0.86)), min(760, int(avail.height() * 0.84)))
        lay = QVBoxLayout(self)

        # ---- 顶部：主题条 ----
        top = QHBoxLayout()
        top.addWidget(QLabel("当前主题："))
        self.cb_theme = QComboBox()
        self.cb_theme.setMinimumWidth(190)
        top.addWidget(self.cb_theme)
        self.lb_theme_info = QLabel("")
        self.lb_theme_info.setStyleSheet("color:#8a8a95")
        top.addWidget(self.lb_theme_info, 1)
        self.b_new = QPushButton("＋ 用视频做一段新动作")
        self.b_new.clicked.connect(self._new_animation)
        top.addWidget(self.b_new)
        lay.addLayout(top)

        self.lb_gap = QLabel("")
        self.lb_gap.setStyleSheet("color:#A85B26; padding:4px 0;")
        lay.addWidget(self.lb_gap)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("默认尺寸："))
        self.cb_tier = QComboBox()
        self.cb_tier.addItems(list(TIERS))
        self.cb_tier.setCurrentText(pet.tier_name if pet.tier_name in TIERS else DEFAULT_TIER)
        srow.addWidget(self.cb_tier)
        srow.addStretch(1)
        lay.addLayout(srow)

        body = QHBoxLayout()
        self.list = PairList()
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list.setSpacing(2)
        self.list.setMinimumWidth(300)
        self.list.setMaximumWidth(440)
        self.list.setItemDelegate(PairDelegate(self.list))
        self.list.currentItemChanged.connect(self._sync_editor)
        self.list.itemChanged.connect(self._on_item_changed)
        body.addWidget(self.list, 38)

        right = QVBoxLayout()
        self.lb_state = QLabel("—")
        self.lb_state.setStyleSheet("font-weight:bold")
        right.addWidget(self.lb_state)

        self.pool = QListWidget()
        self.pool.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.pool.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.pool.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.pool.setMinimumHeight(130)
        self.pool.currentItemChanged.connect(self._on_pool_selected)
        right.addWidget(self.pool, 1)

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
        right.addLayout(pbtn)

        self.lb_pinfo = QLabel("—")
        self.lb_pinfo.setStyleSheet("color:#8a8a95")
        right.addWidget(self.lb_pinfo)
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
        body.addLayout(right, 62)
        lay.addLayout(body, 1)

        btns = QHBoxLayout()
        btns.addWidget(QLabel("越靠上越优先 · 动作池按固定顺序轮播"))
        b_su = QPushButton("上移状态")
        b_su.clicked.connect(lambda: self._move_state(-1))
        b_sd = QPushButton("下移状态")
        b_sd.clicked.connect(lambda: self._move_state(1))
        btns.addWidget(b_su)
        btns.addWidget(b_sd)
        btns.addStretch(1)
        b_reset = QPushButton("恢复默认排序")
        b_reset.clicked.connect(self.reset_defaults)
        btns.addWidget(b_reset)
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_cancel)
        b_save = QPushButton("保存")
        b_save.setDefault(True)
        b_save.clicked.connect(self.save)
        btns.addWidget(b_save)
        lay.addLayout(btns)

        self._fill_themes()
        self._load_theme(self.theme_id)

    # ---- 主题 ---------------------------------------------------------------
    def _fill_themes(self):
        self._loading = True
        self.cb_theme.clear()
        for t in packlib.list_themes():
            self.cb_theme.addItem(t["name"], t["id"])
        i = self.cb_theme.findData(self.theme_id)
        if i >= 0:
            self.cb_theme.setCurrentIndex(i)
        self._loading = False
        self.cb_theme.currentIndexChanged.connect(self._on_theme_change)

    def _on_theme_change(self):
        if self._loading:
            return
        tid = self.cb_theme.currentData()
        if not tid or tid == self.theme_id:
            return
        if self.changed:
            r = QMessageBox.question(
                self, "切换主题", "当前主题有未保存的改动，切换会丢弃。继续？",
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

    def _load_theme(self, tid):
        import copy
        raw = packlib.load_theme(tid)
        self._raw = raw
        defaults = packlib.empty_theme(tid)["states"]
        self._rows = {}
        for st in STATE_KEYS:
            r = raw["states"].get(st)
            self._rows[st] = copy.deepcopy(r) if isinstance(r, dict) else copy.deepcopy(defaults[st])
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
        n = sum(1 for r in self._rows.values() if r.get("clipIds"))
        total = len(self._rows)
        themes = packlib.list_themes()
        cur = next((t for t in themes if t["id"] == self.theme_id), {})
        self.lb_theme_info.setText("%s · %d 段动画可用" % (cur.get("id", "-"), len(packlib.list_assets())))
        gap = total - n
        self.lb_gap.setText(
            "还有 %d 个状态没有动画（零绑定 = 到达时保持当前画面）" % gap if gap
            else "全部 %d 个状态都已绑定动画" % total)

    # ---- 左栏 ---------------------------------------------------------------
    def _add_item(self, st):
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole, st)
        it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
        it.setCheckState(Qt.CheckState.Checked if self._rows[st].get("enabled", True) is not False
                         else Qt.CheckState.Unchecked)
        it.setSizeHint(QSize(0, 62))
        self.list.addItem(it)
        self._items[st] = it
        self._refresh_item(st)

    def _refresh_item(self, st):
        it = self._items.get(st)
        if it is None:
            return
        pool = self._rows[st].get("clipIds") or []
        names = " / ".join(self._clip_name(c) for c in pool) if pool else "0 段 · 保持当前画面"
        it.setText(STATE_CN.get(st, st))
        it.setData(Qt.ItemDataRole.UserRole + 1, names)
        self._refresh_theme_info()

    def _clip_name(self, aid):
        return (CLIPS.get(aid) or packlib.load_asset(aid) or {}).get("name") or \
               (CLIPS.get(aid) or {}).get("displayName") or aid

    def _cur_state(self):
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _on_item_changed(self, it):
        if self._loading:
            return
        st = it.data(Qt.ItemDataRole.UserRole)
        if st in self._rows:
            self._rows[st]["enabled"] = it.checkState() == Qt.CheckState.Checked
            self.changed = True

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
        self.changed = True
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
        self.changed = True

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
            self.pool.clear()
            self.player.release()
            return
        r = self._rows[st]
        extra = ""
        if st.startswith("long_idle"):
            extra = "   ·   分档门槛 %ds" % int(r.get("afterSec", 0))
        self.lb_state.setText("%s%s" % (STATE_CN.get(st, st), extra))
        self._loading = True
        self.pool.clear()
        for i, aid in enumerate(r.get("clipIds") or []):
            it = QListWidgetItem("%02d   %s   [%s]" % (i + 1, self._clip_name(aid), aid))
            it.setData(Qt.ItemDataRole.UserRole, aid)
            self.pool.addItem(it)
        self._loading = False
        if self.pool.count():
            self.pool.setCurrentRow(0)
        else:
            self._reload_player(None, st)

    def _on_pool_selected(self):
        if self._loading:
            return
        it = self.pool.currentItem()
        st = self._cur_state()
        self._reload_player(it.data(Qt.ItemDataRole.UserRole) if it else None, st)

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
        self.changed = True
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
        self.changed = True

    def _pool_move(self, delta):
        row = self.pool.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < self.pool.count()):
            return
        it = self.pool.takeItem(row)
        self.pool.insertItem(new, it)
        self.pool.setCurrentRow(new)
        self._stash_pool()
        self._refresh_item(self._cur_state())
        self.changed = True

    def _fill_pool(self, ids):
        self._loading = True
        self.pool.clear()
        for i, aid in enumerate(ids):
            it = QListWidgetItem("%02d   %s   [%s]" % (i + 1, self._clip_name(aid), aid))
            it.setData(Qt.ItemDataRole.UserRole, aid)
            self.pool.addItem(it)
        self._loading = False
        self.pool.setCurrentRow(min(0, max(0, self.pool.count() - 1)))
        self._on_pool_selected()

    # ---- 预览 ---------------------------------------------------------------
    def _reload_player(self, aid, st):
        if not aid:
            self.player.release()
            self.lb_pinfo.setText("该状态 0 段动画 · 到达时保持当前画面")
            self.lb_prog.setText("第 0 / 0 帧")
            self.b_ws.setEnabled(False)
            return
        ensure_clip(aid)
        c = CLIPS.get(aid) or {}
        w, h = c.get("canvas") or packlib.V2_DEFAULT_CANVAS
        self.lb_pinfo.setText("%s · %s · %d 帧 · canvas %d×%d"
                              % (STATE_CN.get(st, st), self._clip_name(aid),
                                 int(c.get("count") or 0), w, h))
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

    def closeEvent(self, e):
        self.player.release()
        super().closeEvent(e)

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
                it = self._items.get(st)
                r["enabled"] = (it.checkState() == Qt.CheckState.Checked
                                if it is not None else r.get("enabled", True))
                r["priority"] = prios[st]
                r["clipIds"] = list(r.get("clipIds") or [])
            self._raw["states"] = {st: self._rows[st] for st in STATE_KEYS}
            self._raw["name"] = self._raw.get("name") or self.theme_id
            self.write_theme()
            cfg = load_config()
            cfg["tier"] = self.cb_tier.currentText()
            save_config(cfg)
            self.pet.set_tier(cfg["tier"])
            self.changed = True
            log("settings_save_v2 theme=%s tier=%s bound=%d/%d"
                % (self.theme_id, cfg["tier"],
                   sum(1 for r in self._rows.values() if r["clipIds"]), n))
            self.accept()
        except Exception as e:                     # noqa: BLE001 - 对话框里不能 abort
            log("settings_save exception=%r" % (e,))
            QMessageBox.warning(self, "设置", "保存失败：%s" % e)

    def write_theme(self):
        """写 packs/themes/<id>.json：写前备份到 build/cache/，原子替换。"""
        import shutil
        src = packlib.theme_path(self.theme_id)
        bak_dir = os.path.join(ROOT, "build", "cache")
        os.makedirs(bak_dir, exist_ok=True)
        if os.path.exists(src):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(src, os.path.join(bak_dir, "theme.backup-%s.json" % stamp))
        return packlib.write_theme(self.theme_id, self._raw)


'''

# ---------------------------------------------------------------------------
# 12) main() 若干精确替换
# ---------------------------------------------------------------------------
B_MAIN_REPS = [
    ("""    if args.pack:
        _set_pack(args.pack)
        if not os.path.isdir(os.path.join(PACKS_ROOT, args.pack)):
            raise SystemExit("找不到素材包：packs/%s" % args.pack)

    META = json.load(open(os.path.join(ROOT, "meta.json"), encoding="utf-8"))
    if PACK_DIR and os.path.exists(os.path.join(PACK_DIR, "manifest.json")):
        MANIFEST = json.load(open(os.path.join(PACK_DIR, "manifest.json"), encoding="utf-8"))
    CLIPS, ACTIONS = load_clips()
    canvas = pack_canvas()[:2]
    validate_canvas_frames(canvas)""",
     """    if args.pack:
        if not os.path.exists(packlib.theme_path(args.pack)):
            raise SystemExit("找不到主题：packs/themes/%s.json" % args.pack)
        _set_pack(args.pack)

    META = {}                     # v2 不再读 meta.json / manifest.json
    MANIFEST = {}
    CLIPS, ACTIONS = load_clips()
    canvas = pack_canvas()[:2]
    validate_canvas_frames()""", "main: 启动加载"),

    ("""    pet = Pet(width)
    pet.tier_name = tier
    pet.move(screen.right() - pet.width() - 24, screen.bottom() - pet.height() - 12)
""",
     """    pet = Pet(width)
    pet.tier_name = tier
    pet.move(screen.right() - pet.width() - 24, screen.bottom() - pet.height() - 12)

    # ---- 主题热生效：active.json 改了就换装，不用重启 ----
    if not args.pack:
        pet.pack_watcher = ActivePackWatcher(pet)
""", "main: 实例化 watcher"),

    ("""        menu.addMenu(pet.actions_menu(menu))
        menu.addMenu(pet.size_menu(menu))
        menu.addSeparator()
""",
     """        menu.addMenu(pet.actions_menu(menu))
        menu.addMenu(pet.size_menu(menu))
        menu.addMenu(pet.theme_menu(menu))
        menu.addSeparator()
""", "tray: 主题子菜单"),

    ("""    def open_workshop(self, clip="", pack_id=""):""",
     """    def open_workshop(self, clip="", pack_id="", origin_state=""):""", "workshop: 签名"),

    ("""        if clip:
            args += ["--clip", clip]
        try:""",
     """        if clip:
            args += ["--clip", clip]
        if origin_state:
            args += ["--state", origin_state]
        try:""", "workshop: 带目标状态"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with open(TARGET, encoding="utf-8") as f:
        s = f.read()
    orig = s

    s = rep(B_IMPORT_OLD, B_IMPORT_NEW, s, "import")
    s = cut(B_CONST_START, B_CONST_END, B_CONST_NEW, s, "状态常量")
    s = cut(B_DATA_START, B_DATA_END, B_DATA_NEW, s, "数据层")
    s = rep(B_LOADER_OLD, B_LOADER_NEW, s, "Loader 缩放")
    s = rep(B_INIT_OLD, B_INIT_NEW, s, "Pet 画布初值")
    s = cut(B_SIZE_START, B_SIZE_END, B_SIZE_NEW, s, "_size/_resize_to")
    s = cut(B_SWITCH_START, B_SWITCH_END, B_SWITCH_NEW, s, "_switch")
    s = cut(B_MENU_START, B_MENU_END, B_MENU_NEW, s, "右键菜单")
    s = cut(B_APPLY_START, B_APPLY_END, B_APPLY_NEW, s, "apply_pack")
    s = cut(B_WATCH_START, B_WATCH_END, B_WATCH_NEW, s, "ActivePackWatcher")
    s = cut(B_DLG_START, B_DLG_END, B_DLG_NEW, s, "SettingsDialog")
    for old, new, label in B_MAIN_REPS:
        s = rep(old, new, s, label)

    compile(s, TARGET, "exec")           # 语法不过就整体不写盘
    print("语法检查通过：%d → %d 字符，%d 处切块替换" % (len(orig), len(s), 10 + len(B_MAIN_REPS)))
    if args.apply:
        with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
            f.write(s)
        print("已写盘：%s" % TARGET)
    else:
        print("dry-run（加 --apply 才写盘）")


if __name__ == "__main__":
    main()
