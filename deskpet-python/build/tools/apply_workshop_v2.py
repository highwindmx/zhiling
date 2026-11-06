"""把 workshop.py 从 v1（写 manifest + clips.json）改到 v2（写 meta.json + 主题索引）。

用法：python build/tools/apply_workshop_v2.py [--apply]
"""
import argparse
import os

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
TARGET = os.path.join(BASE, "workshop.py")


def rep(old, new, s, label, count=1):
    n = s.count(old)
    if n != count:
        raise RuntimeError("%s: 期望 %d 处，实际 %d 处：%r" % (label, count, n, old[:80]))
    return s.replace(old, new)


def cut(start, end, new, s, label):
    i = s.find(start)
    if i < 0:
        raise RuntimeError("%s: 找不到起始 %r" % (label, start[:60]))
    j = s.find(end, i + len(start))
    if j < 0:
        raise RuntimeError("%s: 找不到结束 %r" % (label, end[:60]))
    return s[:i] + new + s[j:]


R_INIT_OLD = '''class Workshop(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("素材工坊 — 视频 → 素材包（pack）")'''
R_INIT_NEW = '''class Workshop(QWidget):
    def __init__(self, origin_state="", theme_id=""):
        super().__init__()
        self.setWindowTitle("素材工坊 — 视频 → 动画资产（v2）")
        self.origin_state = origin_state or ""    # 从设置带过来的目标状态（生成完默认绑定）
        self.start_theme = theme_id or ""'''

R_REFRESH_OLD = '''    def refresh_packs(self):
        names = pack.list_packs()
        self.cb_pack.clear()
        self.cb_pack.addItems(names)
        act = pack.active_pack()
        if act in names:
            self.cb_pack.setCurrentText(act)
        elif pack.DEFAULT_PACK in names:
            self.cb_pack.setCurrentText(pack.DEFAULT_PACK)

    def cur_pack(self):
        return self.cb_pack.currentText() or pack.DEFAULT_PACK'''
R_REFRESH_NEW = '''    def refresh_packs(self):
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
        return self.cb_pack.currentData() or pack.active_theme()'''

R_CONF_OLD = '''    def pack_conf(self):
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
        return d'''
R_CONF_NEW = '''    def pack_conf(self):
        """导出参数：画布取当前主题 idle 段的 meta.canvas（v2 里画布归动画，不归主题）。

        没有 idle 段时回退全局默认。质量沿用历史值 80，不进 meta.json。
        """
        d = {"w": pack.V2_DEFAULT_CANVAS[0], "h": pack.V2_DEFAULT_CANVAS[1],
             "fps": 24, "fmt": "webp", "quality": 80}
        try:
            th = pack.load_theme(self.cur_pack())
            pool = (th["states"].get("idle") or {}).get("clipIds") or []
            m = pack.load_asset(pool[0]) if pool else None
            w, h = pack.aspect_pair((m or {}).get("canvas"))
            d.update(w=w, h=h, fps=int((m or {}).get("fps") or 24),
                     fmt=(m or {}).get("format") or "webp")
        except Exception:
            pass
        return d'''

R_IDS_OLD = '''    def pack_clip_ids(self):
        try:
            return {c["clipId"] for c in pack.load_manifest(self.cur_pack()).get("clips", [])}
        except Exception:
            return set()'''
R_IDS_NEW = '''    def pack_clip_ids(self):
        """v2：assetId 全局唯一，冲突检查对全库做（不再按主题隔离）。"""
        return set(pack.list_assets())'''

R_NEWPACK_OLD = '''    def new_pack(self):
        dlg = NewPackDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pid, role, canvas = dlg.values()
        try:
            pack.new_pack(pid, role, canvas)
        except FileExistsError:
            QMessageBox.warning(self, "已存在", "packs\\\\%s 已经存在，直接选它就行" % pid)
            return
        self.refresh_packs()
        self.cb_pack.setCurrentText(pid)
        self.log("新建素材包 packs/%s（角色：%s，画布 %d×%d）" % (pid, role, canvas[0], canvas[1]))'''
R_NEWPACK_NEW = '''    def new_pack(self):
        """v2：新建的是「主题」（一张纯绑定索引），不再建 packs/<id>/frames 目录。"""
        dlg = NewPackDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        pid, role, canvas = dlg.values()
        if os.path.exists(pack.theme_path(pid)):
            QMessageBox.warning(self, "已存在", "packs\\\\themes\\\\%s.json 已经存在，直接选它就行" % pid)
            return
        pack.write_theme(pid, pack.empty_theme(pid, name=role))
        self.refresh_packs()
        i = self.cb_pack.findData(pid)
        if i >= 0:
            self.cb_pack.setCurrentIndex(i)
        self.log("新建主题 %s（角色：%s）· 空映射：11 行状态均为 0 段" % (pid, role))'''

R_CLIPENTRY_OLD = '''    def _clip_entry(self, pack_id, cid):
        try:
            m = pack.load_manifest(pack_id)
        except Exception:
            return {}
        return next((c for c in m.get("clips", []) if c.get("clipId") == cid), {})'''
R_CLIPENTRY_NEW = '''    def _clip_entry(self, pack_id, cid):
        """v2：一段动画的元数据就在它自己的 meta.json 里。"""
        return pack.load_asset(cid) or {}'''

R_REMEMBER_OLD = '''    def remember_source(self, pack_id, cid, src):
        """选定源路径写回 manifest：source 仍是文件名（老字段），sourcePath 记完整路径。"""
        try:
            pack.upsert_clip(pack_id, {"clipId": cid, "source": os.path.basename(src),
                                       "sourcePath": rel_repo(src)})
            self.log("源视频已写回 manifest：%s → %s" % (cid, rel_repo(src)))
        except Exception as e:
            self.log("写回 manifest 失败（不影响本次生成）：%r" % (e,))'''
R_REMEMBER_NEW = '''    def remember_source(self, pack_id, cid, src):
        """选定源路径写回该段 meta.json：source 是文件名，sourcePath 记仓库相对路径。"""
        try:
            meta = pack.load_asset(cid) or {"assetId": cid, "name": cid}
            meta.update(source=os.path.basename(src), sourcePath=rel_repo(src))
            pack.write_asset(cid, meta)
            self.log("源视频已写回 meta.json：%s → %s" % (cid, rel_repo(src)))
        except Exception as e:
            self.log("写回 meta.json 失败（不影响本次生成）：%r" % (e,))'''

R_DONE_START = "    def on_gen_done(self, r):"
R_DONE_END = "    def qc(self, out_dir, hue):"
R_DONE_NEW = '''    def on_gen_done(self, r):
        """生成完：写 animations/<assetId>/meta.json，再回写主题索引（默认勾来路状态）。

        V3 裁决：从设置的空状态进工坊，生成完默认勾对应状态；直接打开工坊（无来路）
        则只入库、不动绑定，由用户在设置里从动作库自行挑选。
        """
        v, cid, name = self.queue.pop(0)
        theme_id = self.cur_pack()
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
            "origin": {"theme": theme_id, "generated": True},
        }
        if is_video_file(src):
            meta["sourcePath"] = rel_repo(src)
        old = pack.load_asset(cid) or {}
        for k in ("keyer", "license", "legacyDir", "displayName"):
            if k in old and k not in meta:
                meta[k] = old[k]
        pack.write_asset(cid, meta)
        if self.origin_state and self.origin_state in pack.STATE_KEYS:
            pack.theme_add_clip(theme_id, self.origin_state, cid)
            self.log("已绑定到主题 %s 的状态「%s」" %
                     (theme_id, pack.STATE_CN_V2.get(self.origin_state, self.origin_state)))
        else:
            self.log("已入库（未指定目标状态，未自动绑定）；可在设置里从动作库挑选")
        q = self.qc(r["out_dir"], self.rows["hue_ref"].value())
        self.prog.setValue(100)
        self.lb_result.setText("已生成 %d 帧 · %.2f MB · 脚底锚点 y=%d · 残留绿边 %.2f%% · scale %.3f offset %s"
                               % (r["count"], r["bytes"] / 1e6, r["foot"][1], q["resid"], r["scale"],
                                  r["offset"]))
        self.log("%s 完成：%d 帧，%.2f MB，%d×%d；animations/%s/meta.json 已写入"
                 % (cid, r["count"], r["bytes"] / 1e6, r["width"], h, cid))
        self.show_compare(r["out_dir"])
        self._run_next()

'''

R_REPS = [
    (R_INIT_OLD, R_INIT_NEW, "Workshop.__init__"),
    (R_REFRESH_OLD, R_REFRESH_NEW, "refresh_packs/cur_pack"),
    (R_CONF_OLD, R_CONF_NEW, "pack_conf"),
    (R_IDS_OLD, R_IDS_NEW, "pack_clip_ids"),
    (R_NEWPACK_OLD, R_NEWPACK_NEW, "new_pack"),
    (R_CLIPENTRY_OLD, R_CLIPENTRY_NEW, "_clip_entry"),
    (R_REMEMBER_OLD, R_REMEMBER_NEW, "remember_source"),
    ('        bak = pack.backup_frames(pack_id, cid)',
     '        bak = pack.backup_asset_frames(cid)', "backup"),
    ('                "out_dir": pack.frames_dir(pack_id, cid)}',
     '                "out_dir": pack.asset_dir(cid)}', "out_dir"),
    ('''            data = pack.load_manifest(self.cur_pack())
            idle = data.get("defaultIdle")
            ref_dir = pack.frames_dir(self.cur_pack(), idle) if idle else None''',
     '''            th = pack.load_theme(self.cur_pack())
            pool = (th["states"].get("idle") or {}).get("clipIds") or []
            ref_dir = pack.asset_dir(pool[0]) if pool else None''', "show_compare"),
    ('            d = os.path.dirname(pack.frames_dir(self.cur_pack(), ""))',
     '            d = pack.anim_dir()', "open_out"),
    ('''        for nm in (entry.get("displayName"), v.get("name"), cid):''',
     '''        for nm in (entry.get("name"), entry.get("displayName"), v.get("name"), cid):''',
     "resolve_source_video 名称"),
    ('''        try:
            m = pack.load_manifest(self.cur_pack())
        except Exception as e:
            self.log("定位失败：读不到 packs/%s/manifest.json：%s" % (self.cur_pack(), e))
            return
        clip = next((c for c in m.get("clips", []) if c.get("clipId") == cid), None)
        if not clip:
            self.log("定位失败：packs/%s 里没有 clipId=%s" % (self.cur_pack(), cid))
            return
        d = pack.frames_dir(self.cur_pack(), cid)''',
     '''        clip = pack.load_asset(cid)
        if not clip:
            self.log("定位失败：animations/ 里没有 assetId=%s" % cid)
            return
        d = pack.asset_dir(cid)''', "focus_clip"),
]

R_MAIN_START = "def main():"
R_MAIN_END = 'if __name__ == "__main__":'
R_MAIN_NEW = '''def main():
    ap = argparse.ArgumentParser(description="知灵素材工坊")
    ap.add_argument("--clip", default="", help="启动后定位到已有成品段 assetId（从设置面板跳转）")
    ap.add_argument("--state", default="", help="从设置带过来的目标状态：生成完自动绑定到该状态")
    ap.add_argument("--pack", default="", help="目标主题 id（沿用旧参数名，值是 packs/themes/<id>.json）")
    args, _unknown = ap.parse_known_args()
    app = QApplication(sys.argv)
    w = Workshop(origin_state=args.state, theme_id=args.pack)
    w.show()
    w.refresh_presets()
    if args.clip:
        w.focus_clip(args.clip)
    return app.exec()


'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with open(TARGET, encoding="utf-8") as f:
        s = f.read()
    orig = s
    for old, new, label in R_REPS:
        s = rep(old, new, s, label)
    s = cut(R_DONE_START, R_DONE_END, R_DONE_NEW, s, "on_gen_done")
    s = cut(R_MAIN_START, R_MAIN_END, R_MAIN_NEW, s, "main")
    compile(s, TARGET, "exec")
    print("语法检查通过：%d → %d 字符，%d 处替换" % (len(orig), len(s), len(R_REPS) + 2))
    if args.apply:
        with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
            f.write(s)
        print("已写盘：%s" % TARGET)
    else:
        print("dry-run（加 --apply 才写盘）")


if __name__ == "__main__":
    main()
