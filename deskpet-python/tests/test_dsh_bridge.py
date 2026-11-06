"""dsh_bridge.map_event 的纯逻辑测试（不连 WS / 不连 pet）。

运行（仓库根目录，使用自有 venv）：
    .venv\\Scripts\\python.exe -m pytest tests -q
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build", "tools"))

import v2_constants as C
from dsh_bridge import map_event


def test_status_busy_maps_to_busy_with_ttl():
    out = map_event({"type": "status", "status": "running"})
    assert out == {"state": "busy", "timeoutSec": C.BRIDGE_TTL_BUSY_SEC}
    assert out["timeoutSec"] == 120


def test_status_idle_outside_turn_is_idle():
    out = map_event({"type": "status", "status": "idle"}, ctx={})
    assert out == {"state": "idle"}


def test_status_idle_inside_turn_is_ignored():
    ctx = {"in_turn": True}
    out = map_event({"type": "status", "status": "idle"}, ctx=ctx)
    assert out is None


def test_status_error_maps_to_error_with_short_ttl():
    out = map_event({"type": "status", "status": "error", "message": "boom"})
    assert out["state"] == "error"
    assert out["timeoutSec"] == C.BRIDGE_TTL_ERROR_SEC   # 10
    assert out["why"] == "boom"


def test_token_maps_to_streaming_with_priority():
    ctx = {}
    out = map_event({"type": "token"}, ctx=ctx)
    assert out == {"state": "streaming", "timeoutSec": C.BRIDGE_TTL_BUSY_SEC,
                   "priority": C.BRIDGE_STREAMING_PRIORITY}
    assert ctx["in_turn"] is True


def test_stream_end_inside_turn_returns_busy():
    ctx = {"in_turn": True}
    out = map_event({"type": "stream-end"}, ctx=ctx)
    assert out == {"state": "busy", "timeoutSec": C.BRIDGE_TTL_BUSY_SEC}


def test_stream_end_outside_turn_ignored():
    out = map_event({"type": "stream-end"}, ctx={})
    assert out is None


def test_tool_result_inside_turn_renews_busy():
    ctx = {"in_turn": True}
    out = map_event({"type": "tool-result"}, ctx=ctx)
    assert out == {"state": "busy", "timeoutSec": C.BRIDGE_TTL_BUSY_SEC}


def test_tool_result_outside_turn_ignored():
    out = map_event({"type": "tool-result"}, ctx={})
    assert out is None


def test_question_asked_returns_needs_decision():
    out = map_event({"type": "question-asked", "callId": "q1",
                     "questionCount": 2, "title": "选哪个"})
    assert out["state"] == "needs_decision"
    assert out["callId"] == "q1"
    assert out["questionCount"] == 2


def test_question_cleared_only_for_our_call():
    ctx = {"pending_question": "q1", "in_turn": True}
    # 别人的问题结算不理会
    assert map_event({"type": "question-cleared", "callId": "other"}, ctx=ctx) is None
    # 我们推过的那道 → 轮内回 busy
    out = map_event({"type": "question-cleared", "callId": "q1"}, ctx=ctx)
    assert out == {"state": "busy", "timeoutSec": C.BRIDGE_TTL_BUSY_SEC}
    assert ctx["pending_question"] is None


def test_approval_asked_returns_needs_decision_with_id():
    out = map_event({"type": "approval-asked", "id": "a1", "toolName": "bash",
                     "title": "run?"})
    assert out["state"] == "needs_decision"
    assert out["id"] == "a1"
    assert out["source"] == "approval"


def test_approval_asked_without_id_ignored():
    assert map_event({"type": "approval-asked", "toolName": "bash"}, ctx={}) is None


def test_approval_decided_only_for_our_id():
    ctx = {"pending_approval": "a1", "in_turn": False}
    assert map_event({"type": "approval-decided", "id": "other"}, ctx=ctx) is None
    out = map_event({"type": "approval-decided", "id": "a1"}, ctx=ctx)
    assert out == {"state": "idle"}
    assert ctx["pending_approval"] is None


def test_done_resets_turn():
    ctx = {"in_turn": True}
    out = map_event({"type": "done"}, ctx=ctx)
    assert out == {"state": "done"}
    assert ctx["in_turn"] is False


def test_error_type_resets_turn():
    ctx = {"in_turn": True}
    out = map_event({"type": "error", "message": "x"}, ctx=ctx)
    assert out["state"] == "error"
    assert out["timeoutSec"] == C.BRIDGE_TTL_ERROR_SEC
    assert ctx["in_turn"] is False


def test_unknown_event_returns_none():
    assert map_event({"type": "weird"}) is None
    assert map_event(None) is None
    assert map_event("not-a-dict") is None


def test_status_unknown_status_returns_none():
    assert map_event({"type": "status", "status": "frobnicate"}) is None


def test_constants_used_in_map_event_match_module():
    # map_event 里用到的 TTL/priority 必须来自 v2_constants，避免再次硬编码漂移
    assert C.BRIDGE_TTL_BUSY_SEC == 120
    assert C.BRIDGE_TTL_ERROR_SEC == 10
    assert C.BRIDGE_STREAMING_PRIORITY == 52
