"""一条进程内实测：首帧时间 / 内存 / 双击触动作 / 播完回待机 / 拖动 / 三档尺寸 / 菜单。"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PyQt6.QtCore import Qt, QTimer, QPointF, QEvent
from PyQt6.QtGui import QMouseEvent, QAction
from PyQt6.QtWidgets import QApplication, QMenu
from PIL import ImageGrab
import pet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "build")

pet.META = json.load(open(os.path.join(ROOT, "meta.json"), encoding="utf-8"))
pet.CLIPS = pet.META["clips"]

app = QApplication(sys.argv)
t0 = time.monotonic()
p = pet.Pet(192)                       # 中档 = 1280 逻辑宽的 15%
p.tier_name = "中"
p.move(1280 - p.width() - 24, 800 - p.height() - 12)
p.show()
p.repaint()
print("首帧时间(构造->repaint): %.0f ms" % ((time.monotonic() - t0) * 1000))


def dbl_click(widget, local=(60, 60)):
    g = QPointF(widget.x() + local[0], widget.y() + local[1])
    for _ in range(2):
        app.postEvent(widget, QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(*local), g,
                                          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                          Qt.KeyboardModifier.NoModifier))
        app.postEvent(widget, QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(*local), g,
                                          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                          Qt.KeyboardModifier.NoModifier))
    app.postEvent(widget, QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(*local), g,
                                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                                      Qt.KeyboardModifier.NoModifier))


state = {}


def s1():
    ImageGrab.grab().convert("RGB").save(os.path.join(OUT, "proof_idle.png"))
    print("待机截图 -> build/proof_idle.png  窗口尺寸", p.width(), "x", p.height())
    dbl_click(p)


def s2():
    print("双击后 clip =", p.clip, " idx =", p.idx)
    ImageGrab.grab().convert("RGB").save(os.path.join(OUT, "proof_action.png"))


def s3():
    print("约 6.2s 后 clip =", p.clip, "（应为 idle）idx =", p.idx)
    ws, peak = pet.self_mem_mb()
    print("工作集 %.1f MB  峰值 %.1f MB  缓存 %d 帧" % (ws, peak, len(p.loader.cache)))
    state["from"] = p.pos()
    gp = QPointF(p.x() + 96, p.y() + 80)
    app.postEvent(p, QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(96, 80), gp,
                                 Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier))
    for i in range(1, 8):
        app.postEvent(p, QMouseEvent(QEvent.Type.MouseMove,
                                     QPointF(96 - 12 * i, 80 - 10 * i),
                                     QPointF(gp.x() - 12 * i, gp.y() - 10 * i),
                                     Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                     Qt.KeyboardModifier.NoModifier))
    app.postEvent(p, QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(96, 80), gp,
                                 Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                                 Qt.KeyboardModifier.NoModifier))


def s4():
    d = p.pos() - state["from"]
    print("拖动位移", (d.x(), d.y()), "->", "PASS" if abs(d.x()) > 50 and abs(d.y()) > 40 else "FAIL")
    p.set_tier("小")
    print("小档宽", p.width())
    QTimer.singleShot(500, s5)


def s5():
    p.set_tier("大")
    QTimer.singleShot(600, s6)


def s6():
    print("大档宽", p.width())
    ws, _ = pet.self_mem_mb()
    print("大档工作集 %.1f MB  缓存 %d 帧" % (ws, len(p.loader.cache)))
    ImageGrab.grab().convert("RGB").save(os.path.join(OUT, "proof_big.png"))
    m = QMenu(p)
    for t in ("尺寸：小", "尺寸：中", "尺寸：大", "退出"):
        m.addAction(QAction(t, p))
    print("右键菜单:", [a.text() for a in m.actions()])
    app.quit()


QTimer.singleShot(1200, s1)
QTimer.singleShot(2200, s2)
QTimer.singleShot(7400, s3)
QTimer.singleShot(7800, s4)
app.exec()
print("end，总耗时 %.1f s" % (time.monotonic() - t0))
