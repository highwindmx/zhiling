"""进程内实测：4 段素材 / 双击按序换动作 / 右键选动作 / 回待机 / 拖动 / 三档 / 内存。
时序改为轮询（等到 clip 变化再推进），避免动作时长抖动导致误判。
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PyQt6.QtCore import Qt, QTimer, QPoint, QPointF, QEvent
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
import pet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "build")

pet.META = json.load(open(os.path.join(ROOT, "meta.json"), encoding="utf-8"))
pet.CLIPS, pet.ACTIONS = pet.load_clips()
print("idle =", pet.IDLE_ID, "| 动作顺序 =", pet.ACTIONS, flush=True)

app = QApplication(sys.argv)
t0 = time.monotonic()
p = pet.Pet(192)
p.tier_name = "中"
p.move(1280 - p.width() - 24, 800 - p.height() - 12)
p.show()
p.repaint()
print("首帧时间 %.0f ms，窗口 %dx%d" % ((time.monotonic() - t0) * 1000, p.width(), p.height()), flush=True)
fails = []


def shot(name):
    app.primaryScreen().grabWindow(0).save(os.path.join(OUT, name))
    p.grab().save(os.path.join(OUT, "crop_" + name))   # 窗口自身截图（透明底，便于判断本体）
    print("  截图 -> build/%s" % name, flush=True)


def dbl():
    QTest.mouseDClick(p, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(60, 60))


def verdict(tag, ok):
    print("  %s %s" % (tag, "PASS" if ok else "FAIL"), flush=True)
    if not ok:
        fails.append(tag)


def drag():
    """按住左上区域往左上拖：按下用 QTest，移动直接同步调用 mouseMoveEvent（带左键 buttons），避免入队事件的生命周期问题。"""
    p._drag_from = p.pos()
    QTest.mousePress(p, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(96, 80))
    g0 = p.mapToGlobal(QPoint(96, 80))
    for i in range(1, 8):
        local = QPoint(96 - 12 * i, 80 - 10 * i)
        glob = QPointF(g0.x() - 12 * i, g0.y() - 10 * i)
        ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(local), glob,
                         Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        p.mouseMoveEvent(ev)
    QTest.mouseRelease(p, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(96 - 84, 80 - 70))
    d = p.pos() - p._drag_from
    print("  拖动位移 (%d, %d)" % (d.x(), d.y()), flush=True)
    verdict("拖动", abs(d.x()) > 50 and abs(d.y()) > 40)


menu_holder = {}


def menu_open():
    m = p._build_menu()
    m.popup(p.mapToGlobal(QPoint(80, 40)))
    menu_holder["m"] = m


def menu_pick():
    acts = [a for a in menu_holder["m"].actions() if a.text().startswith("动作：")]
    print("  菜单项:", [a.text() for a in menu_holder["m"].actions() if a.text()], flush=True)
    acts[2].trigger()
    menu_holder["m"].close()


# 每步：kind + 参数；kind=wait 纯等待，其余步骤自身决定何时算完成
steps = [
    {"kind": "wait", "sec": 1.2},
    {"kind": "shot", "name": "proof_idle.png"},
    {"kind": "do", "fn": dbl},
    {"kind": "expect_clip", "clip": "action", "tag": "双击①切到 action", "timeout": 4},
    {"kind": "wait", "sec": 0.35},
    {"kind": "shot", "name": "proof_action.png"},
    {"kind": "expect_idle", "tag": "动作①播完回待机", "timeout": 12},
    {"kind": "do", "fn": dbl},
    {"kind": "expect_clip", "clip": "action2", "tag": "双击②切到 action2（按序）", "timeout": 4},
    {"kind": "wait", "sec": 0.35},
    {"kind": "shot", "name": "proof_action2.png"},
    {"kind": "expect_idle", "tag": "动作②播完回待机", "timeout": 12},
    {"kind": "do", "fn": menu_open},
    {"kind": "wait", "sec": 0.4},
    {"kind": "shot", "name": "proof_menu.png"},
    {"kind": "do", "fn": menu_pick},
    {"kind": "expect_clip", "clip": "action3", "tag": "右键菜单直选 action3", "timeout": 4},
    {"kind": "wait", "sec": 0.35},
    {"kind": "shot", "name": "proof_action3.png"},
    {"kind": "expect_idle", "tag": "动作③播完回待机", "timeout": 14},
    {"kind": "mem"},
    {"kind": "do", "fn": drag},
    {"kind": "do", "fn": lambda: (p.set_tier("小"), print("  小档宽", p.width(), flush=True))},
    {"kind": "wait", "sec": 0.6},
    {"kind": "do", "fn": lambda: (p.set_tier("大"), print("  大档宽", p.width(), flush=True))},
    {"kind": "wait", "sec": 0.8},
    {"kind": "shot", "name": "proof_big.png"},
    {"kind": "final"},
]


SF = {"i": 0, "t": None}


def seq_tick():
    now = time.monotonic()
    while SF["i"] < len(steps):
        s = steps[SF["i"]]
        if s.get("i") != SF["i"]:
            s["i"] = SF["i"]
            s["t"] = now
        k = s["kind"]
        if k == "wait":
            if now - s["t"] < s["sec"]:
                return
        elif k == "expect_clip":
            if p.clip != s["clip"] and now - s["t"] < s["timeout"]:
                return
            verdict(s["tag"], p.clip == s["clip"])
        elif k == "expect_idle":
            if p.clip != pet.IDLE_ID and now - s["t"] < s["timeout"]:
                return
            print("  动作耗时 %.1f s" % (now - s["t"]), flush=True)
            verdict(s["tag"], p.clip == pet.IDLE_ID)
        elif k == "mem":
            ws, peak = pet.self_mem_mb()
            print("  工作集 %.1f MB 峰值 %.1f MB 缓存 %d 帧"
                  % (ws, peak, len(p.loader.cache)), flush=True)
            verdict("内存 < 200MB", ws < 200)
        elif k == "shot":
            shot(s["name"])
        elif k == "do":
            s["fn"]()
        elif k == "final":
            print("  菜单:", [a.text() for a in p._build_menu().actions() if a.text()], flush=True)
            print("失败项:", fails or "无", flush=True)
            app.quit()
            return
        SF["i"] += 1


timer = QTimer()
timer.timeout.connect(seq_tick)
timer.start(80)
_probe = QTimer()
_probe.timeout.connect(lambda: print("  beat clip=%s idx=%d mem=%.1f cache=%d"
                                     % (p.clip, p.idx, pet.self_mem_mb()[0],
                                        len(p.loader.cache)), flush=True))
_probe.start(1000)
app.exec()
print("end，总耗时 %.1f s" % (time.monotonic() - t0), flush=True)
sys.exit(1 if fails else 0)
