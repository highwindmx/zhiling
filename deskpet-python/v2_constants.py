"""跨端共享常量 —— 单一真相源。

DeskPet 同时存在两个实现：
  - 本仓库 Python 版（pet.py / state_map.py / build/tools/dsh_bridge.py）
  - Tauri 版（deskpet-tauri/frontend/src/shared.js）

两边各自写死阈值是危险的：改了一边、另一边悄悄漂移，行为就对不上了。
这里集中放「状态集合 + 时序阈值 + 端口」，两边 import 同一份，改一处即全端生效。

约定：
  - 端口常量大写下划线（DEFAULT_DSH_WS_PORT / DEFAULT_STATE_PORT）
  - 阈值常量语义化命名（LONG_IDLE_SEC / BRIDGE_TTL_BUSY_SEC …）
  - 状态集合与 state_map.py 历史定义保持一致（idle 结尾，13 元组）
"""

# ---- 状态集合（与 Tauri shared.js STATES 成员一致；顺序无关，优先级来自主题 JSON）----
STATES = (
    "double_click", "click", "error", "needs_decision", "done",
    "busy", "focus", "streaming", "awaiting_user", "focus_break",
    "long_idle_2", "long_idle_1", "idle",
)
REMINDER_STATES = ("awaiting_user", "needs_decision")
# 提醒型状态：完成 / 出错 / 待决策。开启 hold_reminders 后这三态播完不回 idle，
# 一直轮播直到用户点击桌宠（或继续输入）。
HOLD_STATES = ("done", "error", "needs_decision")

# ---- 状态机时序阈值（state_map.py 使用）----
LONG_IDLE_SEC = 600            # 空闲多久 → long_idle_1
LONG_IDLE_2_AFTER_SEC = 1800  # long_idle_1 再空闲多久 → long_idle_2（afterSec 兜底值）
AWAITING_USER_SEC = 30         # 完成态多久没反馈（点击/输入）→ 弹提醒
DECISION_TIMEOUT_SEC = 300     # needs_decision 多久没决策 → 自动超时回 idle

# ---- DSH 桥接时序 / 优先级（build/tools/dsh_bridge.py map_event 使用）----
BRIDGE_TTL_BUSY_SEC = 120      # 思考/打字态 TTL（工具跑再久也不掉 idle 的兜底）
BRIDGE_TTL_ERROR_SEC = 10      # 出错态 TTL
BRIDGE_STREAMING_PRIORITY = 52 # 打字态优先级（高于 busy=50，保证真的在出字时切到打字动画）

# ---- 默认端口（DSH WebSocket / 桌宠 /state HTTP）----
DEFAULT_DSH_WS_PORT = 55471
DEFAULT_STATE_PORT = 8765
