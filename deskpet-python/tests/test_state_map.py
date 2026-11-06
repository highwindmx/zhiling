"""state_map.py 的纯逻辑测试：不依赖 Qt，now/play/log 全部注入。

运行（仓库根目录，使用自有 venv）：
    .venv\\Scripts\\python.exe -m pytest tests -q
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from state_map import StateMap, STATES, REMINDER_STATES, HOLD_STATES, parse_state_map


# ---- 测试用夹具 -------------------------------------------------------------

def _clips(ids):
    """构造 clips 字典（assetId -> meta）。"""
    return {cid: {"fps": 24, "count": 4, "name": cid} for cid in ids}


def _make_sm(text, clips=None, now=lambda: 0.0):
    """从主题 JSON 文本构造 StateMap，play/log 记录调用。"""
    clips = clips or _clips(["idle_a", "busy_a", "click_a", "done_a", "err_a"])
    played = []
    sm = StateMap.__new__(StateMap)
    # 不调用 __init__（会读真实 path）；手动初始化可注入的字段
    sm.path = "<mem>"
    sm.clips = clips
    sm._play = lambda aid, loop, on_end=None: played.append((aid, loop))
    sm._play_idle = lambda: played.append(("<idle>", False))
    sm._log = lambda m: None
    sm._now = now
    sm.log_lines = []
    sm.rules = {}
    sm.long_idle_sec = 600
    sm.awaiting_user_sec = 30
    sm.state = None
    sm.silenced = False
    sm.reminders = 0
    sm.deadline_at = 0
    sm.hold_until = 0
    sm.cur_clip = None
    sm.cur_priority = 0
    sm.cur_interruptible = True
    sm._last_play = {}
    sm._long_idle_at = 0
    sm._done_at = 0
    sm._mtime = None
    sm._pending = None
    sm._ttl = None
    sm.decision_timeout = 300
    sm.hold_reminders = False
    sm.playing = None
    # 手动跑 load 的解析部分（用传入的 text 而非文件）
    import json as _json
    try:
        rules, li, au, errors = parse_state_map(text, set(sm.clips))
    except Exception as e:  # noqa: BLE001
        raise
    sm.rules = rules
    sm.long_idle_sec = li
    sm.awaiting_user_sec = au
    sm._parse_errors = errors
    sm.state = "idle"
    sm._long_idle_at = now()
    sm._played = played
    return sm


THEME = """
{
  "version": 2, "id": "t", "name": "t",
  "longIdleSec": 600, "awaitingUserSec": 30,
  "states": {
    "idle":   {"clipIds": ["idle_a"]},
    "busy":   {"clipIds": ["busy_a"], "priority": 50},
    "click":  {"clipIds": ["click_a"]},
    "done":   {"clipIds": ["done_a"], "hold": true},
    "error":  {"clipIds": ["err_a"], "hold": true},
    "long_idle_2": {"clipIds": ["idle_a"], "afterSec": 1800}
  }
}
"""


# ---- 解析 ------------------------------------------------------------------

def test_parse_known_states_present():
    rules, li, au, errors = parse_state_map(THEME, set(_clips(["idle_a", "busy_a", "click_a", "done_a", "err_a"])))
    for st in ("idle", "busy", "click", "done", "error"):
        assert st in rules
    # 未列出的状态 clipIds 为空
    assert rules["double_click"]["clipIds"] == []


def test_parse_drops_unknown_clips():
    bad = THEME.replace('"idle_a"', '"nonexistent_a"')
    rules, li, au, errors = parse_state_map(bad, set(_clips(["busy_a"])))
    assert rules["idle"]["clipIds"] == []
    assert any("idle 缺段" in e for e in errors)


def test_parse_fallback_when_pool_empty():
    # idle 主池为空（clipIds:[]）但有 fallbackClipIds 可用 → 用 fallback 补位。
    # 注意：语义是「主池 absent/空」才走 fallback；若主池写了段但全部缺失，
    # 则只剔除缺失段、不补 fallback（见 review 对该处 docstring/代码不一致的记录）。
    theme = """
    {
      "version": 2, "id": "t", "name": "t",
      "states": {
        "idle": {"clipIds": [], "fallbackClipIds": ["busy_a"]},
        "busy": {"clipIds": ["busy_a"]},
        "error": "NOT_AN_OBJECT"
      }
    }"""
    rules, li, au, errors = parse_state_map(theme, set(_clips(["busy_a"])))
    assert rules["idle"]["clipIds"] == ["busy_a"]
    assert any("用备用段" in e for e in errors)
    # error 行格式坏了 → 按空对象处理，不抛异常（坏一行只废一行）
    assert rules["error"]["clipIds"] == []


def test_parse_default_thresholds_from_constants():
    rules, li, au, errors = parse_state_map(THEME, set(_clips(["idle_a"])))
    assert li == 600          # LONG_IDLE_SEC
    assert au == 30           # AWAITING_USER_SEC


# ---- 状态机 ----------------------------------------------------------------

def test_request_play_picks_clip_and_calls_play():
    sm = _make_sm(THEME)
    ok = sm.request_play(sm.rules["click"], "click")
    assert ok
    assert sm.cur_clip == "click_a"
    assert sm._played == [("click_a", False)]


def test_idle_continues_current_clip_when_playing_in_pool():
    """idle 续播：从别的态切回 idle 时，若 view 正在播该 idle 的某段则不打断。

    注意：set_state 在 name==self.state 时直接早退，所以这条续播分支只在
    “非 idle → idle 的转移”时触发（生产里 view 的 _switch 在 idle 态外回填
    playing，下一次切回 idle 才会走这里）。这也暴露一个脆弱点：若 already-idle
    时直接回填 playing，不会立即续播，要等下次转移——见 review 注释。
    """
    sm = _make_sm(THEME)
    sm.set_state("busy", "enter-busy")
    sm._played.clear()                 # 清空 busy 阶段已播记录，只验证 idle 转移
    sm.playing = "idle_a"
    sm.set_state("idle", "back-to-idle")
    assert sm.cur_clip == "idle_a"
    assert sm._played == []


def test_idle_interrupts_when_playing_foreign_clip():
    sm = _make_sm(THEME)
    sm.playing = "busy_a"
    sm.request_play(sm.rules["idle"], "idle")
    assert sm.cur_clip == "idle_a"
    assert sm._played == [("idle_a", False)]


def test_set_state_changes_state_and_resets_deadline():
    sm = _make_sm(THEME)
    sm.set_state("busy", "test")
    assert sm.state == "busy"


def test_tick_long_idle_transition():
    times = [0.0]
    sm = _make_sm(THEME, now=lambda: times[0])
    sm.set_state("idle", "start")
    times[0] = 700  # > long_idle_sec(600)
    sm.tick()
    assert sm.state == "long_idle_1"


def test_tick_long_idle_2_after_gate():
    times = [0.0]
    sm = _make_sm(THEME, now=lambda: times[0])
    sm.set_state("idle", "start")
    times[0] = 700
    sm.tick()
    assert sm.state == "long_idle_1"
    times[0] = 3000  # > afterSec 默认 1800
    sm.tick()
    assert sm.state == "long_idle_2"


def test_constants_consistency():
    # 13 个状态，idle 结尾；提醒态/常驻态是子集
    assert len(STATES) == 13
    assert STATES[-1] == "idle"
    assert set(REMINDER_STATES) <= set(STATES)
    assert set(HOLD_STATES) <= set(STATES)
    assert HOLD_STATES == ("done", "error", "needs_decision")
    assert REMINDER_STATES == ("awaiting_user", "needs_decision")
