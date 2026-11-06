"""工坊自测：加载新视频 → 测拖动预览 fps → 三视图 → 生成到临时 clipId → 截图。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PyQt6.QtWidgets import QApplication
import workshop as ws

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
OUT = BASE

app = QApplication(sys.argv)
w = ws.Workshop()
w.show()
app.processEvents()

vids = [p for p in [
    os.path.join(os.path.dirname(ROOT), "海螺_视频_@图片1请基于这个大_574308429835190278.mp4"),
] if os.path.exists(p)]
print("待测视频:", [os.path.basename(p) for p in vids], flush=True)
if not vids:
    print("没找到测试视频", flush=True)
    sys.exit(2)

w.add_files(vids)
app.processEvents()
w.rows["hue_ref"].set_value(136.4)
w.list.setCurrentRow(0)
app.processEvents()
print("列表项:", [w.list.item(i).text() for i in range(w.list.count())], flush=True)
print("帧范围:", w.frame_sld.minimum(), "-", w.frame_sld.maximum(), flush=True)


def bench(mode, n=24):
    w.view_mode.setCurrentIndex(mode)
    w.render(True)
    t = []
    for i in range(n):
        w.rows["blur"].set_value(0.6 + (i % 8) * 0.2)
        t0 = time.monotonic()
        w.render(True)                    # 模拟拖动中的快速预览
        t.append(time.monotonic() - t0)
    avg = sum(t) / len(t)
    print("视图=%s 拖动单帧 %.0f ms → %.1f fps" % (w.view_mode.currentText(), avg * 1000,
                                                  1.0 / avg), flush=True)
    return 1.0 / avg


fps2 = bench(1)
fps_a = bench(2)
fps0 = bench(0)
w.view_mode.setCurrentIndex(1)
w.render()

# 截屏：整窗 + 全屏
w.grab().save(os.path.join(OUT, "proof_workshop.png"))
app.primaryScreen().grabWindow(0).save(os.path.join(OUT, "proof_workshop_screen.png"))
print("截图 -> build/proof_workshop.png", flush=True)

# 生成（临时 clipId，测完删掉）
w.videos[0]["cid"] = "ws_test"
w.ed_id.setText("ws_test")
w.ed_name.setText("工坊自测")
w.cb_type.setCurrentText("action")
w.sp_prio.setValue(9)
w.sp_w.setValue(256)

# B2：受影响像素统计
t0 = time.monotonic()
w.compute_impact()
print("受影响像素统计 %.0f ms：" % ((time.monotonic() - t0) * 1000)
      + " ".join("%s=%s" % (k, w.rows[k].lb_fx.text()) for k in w.rows), flush=True)
# B2：最大连通块开关
w.cb_keep.setChecked(False)
w.render()
w.cb_keep.setChecked(True)

# B2：预设存/读（直接写文件，避免弹对话框）
pp = os.path.join(ROOT, "presets")
os.makedirs(pp, exist_ok=True)
with open(os.path.join(pp, "自测预设.json"), "w", encoding="utf-8") as f:
    json.dump({"params": {"hue_ref": 120.0, "sat_lo": 0.4, "blur": 2.0}, "keep_largest": False},
              f, ensure_ascii=False)
w.refresh_presets()
print("预设列表:", [w.cb_preset.itemText(i) for i in range(w.cb_preset.count())], flush=True)
w.cb_preset.setCurrentText("自测预设")
print("载入预设后 hue_ref=%.1f sat_lo=%.2f blur=%.1f 只留最大块=%s"
      % (w.rows["hue_ref"].value(), w.rows["sat_lo"].value(), w.rows["blur"].value(),
         w.cb_keep.isChecked()), flush=True)
w.rows["hue_ref"].set_value(136.4)      # 恢复成能抠开的参数
w.cb_keep.setChecked(True)

w.log("自测：生成到临时 clip ws_test")
w.generate()
t0 = time.monotonic()
while w.gen and w.gen.isRunning():
    app.processEvents()
    time.sleep(0.05)
app.processEvents()
print("生成耗时 %.1f s，进度 %d%%" % (time.monotonic() - t0, w.prog.value()), flush=True)
print("日志尾部:", w.log_box.toPlainText().strip().splitlines()[-1], flush=True)
cj = json.load(open(os.path.join(ROOT, "clips.json"), encoding="utf-8"))
print("clips.json 里的 id:", [c["id"] for c in cj["clips"]], flush=True)
w.grab().save(os.path.join(OUT, "proof_workshop_gen.png"))
print("截图 -> build/proof_workshop_gen.png", flush=True)
print("SUMMARY fps 抠像=%.1f alpha=%.1f 原片=%.1f" % (fps2, fps_a, fps0), flush=True)
