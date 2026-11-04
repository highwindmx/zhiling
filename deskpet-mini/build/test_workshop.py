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
    w.render()
    t = []
    for i in range(n):
        w.rows["blur"].set_value(0.6 + (i % 8) * 0.2)
        t0 = time.monotonic()
        w.render()
        t.append(time.monotonic() - t0)
    avg = sum(t) / len(t)
    print("视图=%s 单帧 %.0f ms → %.1f fps" % (w.view_mode.currentText(), avg * 1000,
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
w.sp_w.setValue(384)
w.log("自测：生成到临时 clip ws_test")
w.generate()
t0 = time.monotonic()
while w.gen and w.gen.isRunning():
    app.processEvents()
    time.sleep(0.05)
app.processEvents()
print("生成耗时 %.1f s，进度 %d%%" % (time.monotonic() - t0, w.prog.value()), flush=True)
print("日志尾部:", w.log_box.toPlainText().strip().splitlines()[-1], flush=True)
w.grab().save(os.path.join(OUT, "proof_workshop_gen.png"))
print("截图 -> build/proof_workshop_gen.png", flush=True)
print("SUMMARY fps 抠像=%.1f alpha=%.1f 原片=%.1f" % (fps2, fps_a, fps0), flush=True)
