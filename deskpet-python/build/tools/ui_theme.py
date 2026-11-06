"""桌宠 Python 版两个界面（设置窗 / 素材工坊）共用的低对比深色配色。

背景：工坊旧版只写了 `QWidget { color:#d9dee8 }` 却没写背景，Qt 默认调色板
窗口色是 #efefef → 浅色字落在白底上，几乎看不见；设置窗则完全没有样式表，
用的是系统浅色主题 + 手写的中灰/橙色/亮蓝，两个窗口像两个软件。

这里把颜色收敛成一份：所有面板/文字/强调色都取自同一张表，主文字不用纯白、
强调色不用高饱和亮蓝，避免刺眼的高对比。QSS 用 string.Template 填值，
因为 QSS 自身也用花括号，不能用 str.format。

注意：QSS 只挂在具体的窗口上，**不要挂到 QApplication**——桌宠主窗口是
透明无边框 QWidget，app 级 `QWidget{background:...}` 会把它刷成不透明色块。
"""
from string import Template

from PyQt6.QtGui import QColor, QPalette

# 唯一真相。改配色只改这里。
P = {
    "bg":           "#1b2028",   # 窗口底
    "panel":        "#222936",   # 分组框 / 菜单
    "raised":       "#2a3241",   # 按钮、抬起面
    "raised_hover": "#333d4e",
    "surface":      "#171c24",   # 输入框 / 预览 / 日志底
    "border":       "#3a4453",
    "text":         "#cfd6e2",   # 主文字（刻意不用纯白）
    "muted":        "#97a4b6",   # 次要文字（深色底上仍有足够对比）
    "faint":        "#76839a",   # 三级说明文字
    "disabled":     "#637082",
    "accent":       "#8fb3d9",   # 柔蓝：标题、分组标题
    "accent_strong": "#6f97c4",  # 滑块 / 进度 / 选中态
    "select":       "#33465f",
    "select_text":  "#e0e7f1",
    "ok":           "#83b79a",   # 绿灯 / 成功（不用纯绿）
    "warn":         "#c9a668",   # 琥珀 / 提示
    "error":        "#c98b8b",   # 红灯 / 错误
    "preview_bg":   "#202632",   # 棋盘格底
    "preview_alt":  "#283041",   # 棋盘格另一格
}

# 指示灯四态也走同一张表（原来 #37b26b / #e05252 太跳）
LINK_COLORS = {
    "grey":  P["muted"],
    "red":   P["error"],
    "amber": P["warn"],
    "green": P["ok"],
}

_QSS_TEMPLATE = Template("""
QWidget { background-color: $bg; color: $text; }
QLabel { background: transparent; color: $text; }
QGroupBox { background-color: $panel; border: 1px solid $border; border-radius: 6px;
            margin-top: 14px; padding: 10px 8px 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px;
                   color: $accent; background: transparent; }
QPushButton { background-color: $raised; color: $text; border: 1px solid $border;
              border-radius: 4px; padding: 5px 10px; }
QPushButton:hover { background-color: $raised_hover; border-color: $accent_strong; }
QPushButton:pressed { background-color: $select; }
QPushButton:default { border-color: $accent_strong; }
QPushButton:disabled { color: $disabled; background-color: $panel; }
QToolButton { background-color: transparent; color: $text; padding: 4px 8px;
              border: 1px solid transparent; border-radius: 4px; }
QToolButton:checked { background-color: $raised; border-color: $border; }
QToolButton:hover { background-color: $raised_hover; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit,
QListWidget, QTreeWidget, QTableWidget {
    background-color: $surface; color: $text; border: 1px solid $border;
    border-radius: 4px; padding: 3px 6px; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: $disabled; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { background-color: $surface; color: $text;
    border: 1px solid $border; selection-background-color: $select;
    selection-color: $select_text; }
QAbstractItemView::item { padding: 3px 4px; }
QAbstractItemView::item:selected { background-color: $select; color: $select_text; }
QAbstractItemView::item:hover { background-color: $raised; }
QCheckBox, QRadioButton { background: transparent; color: $text; spacing: 6px; }
QCheckBox::indicator, QRadioButton::indicator { width: 14px; height: 14px;
    border: 1px solid $muted; border-radius: 3px; background-color: $surface; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: $accent_strong; border-color: $accent_strong; }
QSlider::groove:horizontal { height: 4px; background-color: $panel; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; background-color: $accent_strong;
    border-radius: 6px; margin: -4px 0; }
QProgressBar { border: 1px solid $border; border-radius: 4px;
    background-color: $surface; color: $text; text-align: center; }
QProgressBar::chunk { background-color: $accent_strong; }
QSplitter::handle { background-color: $border; }
QScrollArea { background: transparent; }
QScrollBar:vertical, QScrollBar:horizontal { background: $surface; border: none; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: $raised; border-radius: 4px; }
QToolTip { background-color: $raised; color: $text; border: 1px solid $border; }
QMenu { background-color: $panel; color: $text; border: 1px solid $border; }
QMenu::item:selected { background-color: $select; color: $select_text; }
QTabWidget::pane { border: 1px solid $border; }
QTabBar::tab { background-color: $panel; color: $muted; padding: 6px 12px;
               border: 1px solid $border; }
QTabBar::tab:selected { background-color: $raised; color: $text; }
QHeaderView::section { background-color: $panel; color: $muted;
                       border: none; padding: 4px; }
""")

QSS = _QSS_TEMPLATE.substitute(P)


def dark_palette():
    """给自定义绘制（PairDelegate / 画笔画的字）用：QSS 不会改 QPalette。"""
    pal = QPalette()
    c = lambda k: QColor(P[k])                          # noqa: E731
    pal.setColor(QPalette.ColorRole.Window, c("bg"))
    pal.setColor(QPalette.ColorRole.WindowText, c("text"))
    pal.setColor(QPalette.ColorRole.Base, c("surface"))
    pal.setColor(QPalette.ColorRole.AlternateBase, c("panel"))
    pal.setColor(QPalette.ColorRole.Text, c("text"))
    pal.setColor(QPalette.ColorRole.Button, c("raised"))
    pal.setColor(QPalette.ColorRole.ButtonText, c("text"))
    pal.setColor(QPalette.ColorRole.Highlight, c("select"))
    pal.setColor(QPalette.ColorRole.HighlightedText, c("select_text"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, c("disabled"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, c("disabled"))
    return pal
