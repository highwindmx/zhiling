"""托盘最小探针：只做 QApplication + QSystemTrayIcon，用来区分「系统/Qt 问题」与 pet.py 逻辑问题。

运行（从 deskpet-python 目录）：
    .venv\\Scripts\\python.exe build\\tools\\tray_probe.py
或（无控制台，直接看日志）：
    .venv\\Scripts\\pythonw.exe build\\tools\\tray_probe.py

10 秒后自动退出，结果同时写 build\\cache\\tray_probe.log 和控制台。
"""
import os
import sys
import time

from PyQt6.QtCore import QSize, QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

BASE = os.path.dirname(os.path.abspath(__file__))          # build\tools
ROOT = os.path.dirname(os.path.dirname(BASE))              # deskpet-python
LOG_PATH = os.path.join(ROOT, "build", "cache", "tray_probe.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

_lines = []


def note(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    _lines.append(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None:                 # pythonw 下 stdout 为 None
        print(line, flush=True)


def main():
    note("probe start cwd=%s" % os.getcwd())
    app = QApplication(sys.argv)

    icon_path_ico = os.path.join(ROOT, "assets", "icon.ico")
    icon_path_png = os.path.join(ROOT, "assets", "icon.png")
    path = icon_path_ico if os.path.exists(icon_path_ico) else icon_path_png
    icon = QIcon(path)
    note("probe icon path=%s exists=%s isNull=%s sizes=%s"
         % (path, os.path.exists(path), icon.isNull(),
            ["%dx%d" % (s.width(), s.height()) for s in icon.availableSizes()]))

    screen = app.primaryScreen()
    note("probe platform=%s screens=%d primary=%s dpr=%s"
         % (app.platformName(), len(QApplication.screens()), screen.name(),
            screen.devicePixelRatio()))
    note("probe tray available=%s supports_messages=%s"
         % (QSystemTrayIcon.isSystemTrayAvailable(), QSystemTrayIcon.supportsMessages()))

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("知灵桌宠 tray probe")
    menu = QMenu()
    act_show = QAction("显示", menu)
    act_show.triggered.connect(lambda: on_show(tray))
    menu.addAction(act_show)
    menu.addAction(QAction("关于", menu))
    act_quit = QAction("退出", menu)
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.setVisible(True)

    def snap(tag):
        try:
            actual = tray.icon().actualSize(QSize(16, 16))
            note("probe[%s] visible=%s available=%s geometry=%s iconNull=%s actual16=%dx%d sizes=%s"
                 " menu_items=%d"
                 % (tag, tray.isVisible(), QSystemTrayIcon.isSystemTrayAvailable(), tray.geometry(),
                    tray.icon().isNull(), actual.width(), actual.height(),
                    ["%dx%d" % (s.width(), s.height()) for s in tray.icon().availableSizes()],
                    len(menu.actions())))
        except Exception as e:                 # noqa: BLE001
            note("probe[%s] exception=%r" % (tag, e))

    def on_show(t):
        note("probe menu 显示 clicked, visible=%s" % t.isVisible())
        t.show()

    QTimer.singleShot(0, lambda: snap("t+0s"))
    QTimer.singleShot(1000, lambda: snap("t+1s"))
    QTimer.singleShot(5000, lambda: snap("t+5s"))
    QTimer.singleShot(9000, lambda: snap("t+9s"))
    QTimer.singleShot(10000, app.quit)
    note("probe run 10s: 若这 10 秒里任务栏右下角也看不到，就是 Qt/系统层不可用，与 pet.py 无关")
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        note("probe CRASH %s" % traceback.format_exc())
        raise
