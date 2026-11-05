"""状态映射：把可观测状态映射到播放哪个 clip。

与 deskpet-tauri/frontend/src/main.js 的「状态映射」一节同一套语义，只换语言：
同一份 packs/<packId>/state-map.json，10 个状态、priority 0~100、同样的三道闸
（disabled / cooldown / busy-hold）和两个提醒态的 repeat 机制。

设计上刻意不依赖 Qt：播放动作通过回调传进来，方便脱离 GUI 单测：
    python state_map.py packs/dsh/state-map.json          # 解析并打印每条规则
"""
import json
import os
import time

STATES = ("idle", "long_idle", "awaiting_user", "busy", "streaming",
          "done", "needs_decision", "error", "click", "double_click")
REMINDER_STATES = ("awaiting_user", "needs_decision")
_LOG_MAX = 20


def _num(v, dflt):
    try:
        n = float(v)
        return n if n >= 0 else dflt
    except (TypeError, ValueError):
        return dflt


def parse_state_map(text, clip_ids):
    """逐条校验：坏一条只废一条。返回 (rules, long_idle_sec, awaiting_user_sec, errors)。"""
    raw = json.loads(text)
    rules, errors = {}, []
    for i, r in enumerate(raw.get("rules") or []):
        try:
            if not isinstance(r, dict):
                raise ValueError("不是对象")
            st = r.get("state")
            if st not in STATES:
                raise ValueError("未知状态 %r" % (st,))
            cid = r.get("clipId")
            if cid not in clip_ids:
                raise ValueError("clipId 不存在: %r" % (cid,))
            if st in rules:
                raise ValueError("状态 %s 重复（只保留第一条）" % st)
            rp = r.get("repeat") if isinstance(r.get("repeat"), dict) else None
            rule = {
                "state": st,
                "clipId": cid,
                "priority": _num(r.get("priority"), 20),
                "minPlayMs": _num(r.get("minPlayMs"), 0),
                "cooldownSec": _num(r.get("cooldownSec"), 0),
                "loop": bool(r.get("loop")),
                "interruptible": r.get("interruptible", True) is not False,
                "enabled": r.get("enabled", True) is not False,
                "repeat": {
                    "mode": "interval" if (rp or {}).get("mode") == "interval" else "once",
                    "intervalSec": _num((rp or {}).get("intervalSec"), 75),
                    "maxTimes": max(1, int(_num((rp or {}).get("maxTimes"), 3))),
                } if rp else None,
            }
            # repeat 只对提醒态有效，其它状态写了 interval 也按 once 处理
            if rule["repeat"] and st not in REMINDER_STATES:
                rule["repeat"]["mode"] = "once"
            rules[st] = rule
        except Exception as e:                     # noqa: BLE001 - 单条规则容错
            errors.append("rule#%d 无效: %s" % (i, e))
    return rules, _num(raw.get("longIdleSec"), 600), _num(raw.get("awaitingUserSec"), 30), errors


class StateMap:
    """状态机 + 仲裁。播放通过回调交给调用方（pet.py 的播放器）。"""

    def __init__(self, path, clips, play, play_idle, log, now=time.monotonic):
        self.path = path
        self.clips = clips                 # clipId -> {"fps","count","name"}
        self._play = play                  # play(clip_id, loop, on_end)
        self._play_idle = play_idle        # 回到待机画面
        self._log = log                    # log(str)
        self._now = now
        self.log_lines = []
        self.rules = {}
        self.long_idle_sec = 600
        self.awaiting_user_sec = 30
        self.state = None
        self.silenced = False
        self.reminders = 0
        self.deadline_at = 0
        self.hold_until = 0
        self.cur_clip = None
        self.cur_priority = 0
        self.cur_interruptible = True
        self._last_play = {}
        self._long_idle_at = 0
        self._done_at = 0
        self._mtime = None
        self._pending = None               # (rule, source, at) 提醒态的下一次
        self.decision_timeout = 300        # needs_decision 的默认超时（DSH 可覆盖）
        self.playing = None                # 当前真实在播的 clipId（由 view 层回填）
        self.load()

    # ---- 配置 --------------------------------------------------------------
    def load(self, force=False):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if force:
                self._log("state-map 缺失: %s（保持当前规则）" % self.path)
            return False
        if not force and mtime == self._mtime:
            return False
        try:
            text = open(self.path, encoding="utf-8").read()
            rules, long_idle, awaiting, errors = parse_state_map(text, set(self.clips))
        except Exception as e:                     # noqa: BLE001 - 坏文件保留上一版
            self._log("state-map 解析失败（保留上一版）: %s" % e)
            return False
        for e in errors:
            self._log(e)
        self.rules, self.long_idle_sec, self.awaiting_user_sec = rules, long_idle, awaiting
        self._mtime = mtime
        self._log("state-map 载入: %d 条 longIdle=%ds awaitingUser=%ds"
                  % (len(rules), int(long_idle), int(awaiting)))
        return True

    # ---- 仲裁 --------------------------------------------------------------
    def _dbg(self, msg):
        line = "%s | state=%s cur=%s" % (msg, self.state or "-", self.cur_clip or "-")
        self.log_lines.append(line)
        if len(self.log_lines) > _LOG_MAX:
            self.log_lines.pop(0)
        self._log(line)

    def _reject(self, rule, source, why):
        self._dbg("拒绝 %s→%s：%s" % (source, rule["clipId"], why))
        return False

    def _clip_ms(self, cid):
        c = self.clips.get(cid) or {}
        return (c.get("count", 1) / float(c.get("fps") or 24)) * 1000.0

    def request_play(self, rule, source):
        if not rule["enabled"]:
            return self._reject(rule, source, "disabled")
        now = self._now()
        last = self._last_play.get(rule["clipId"], -1e12)
        if now < last + rule["cooldownSec"]:
            return self._reject(rule, source, "cooldown 剩余 %.1fs"
                                % (last + rule["cooldownSec"] - now))
        if now < self.hold_until:
            if not self.cur_interruptible:
                return self._reject(rule, source, "hold:%s" % self.cur_clip)
            if rule["priority"] <= self.cur_priority:
                return self._reject(rule, source, "busy:%s" % self.cur_clip)
        self._last_play[rule["clipId"]] = now
        self.cur_clip = rule["clipId"]
        self.cur_priority = rule["priority"]
        self.cur_interruptible = rule["interruptible"]
        self.hold_until = now + max(self._clip_ms(rule["clipId"]), rule["minPlayMs"]) / 1000.0
        self._dbg("命中 %s→%s（%s）prio=%g loop=%s"
                  % (source, rule["clipId"], (self.clips.get(rule["clipId"]) or {}).get("name", ""),
                     rule["priority"], rule["loop"]))
        self._play(rule["clipId"], rule["loop"], lambda: self._clip_finished(rule, source))
        return True

    def _clip_finished(self, rule, source):
        self.hold_until = 0
        rp = rule["repeat"]
        if not rp or rule["loop"]:
            if self.state != "idle":
                self.set_state("idle", "clip-end")
            return
        now = self._now()
        if rp["mode"] == "interval" and self.reminders + 1 < rp["maxTimes"] \
                and (self.deadline_at == 0 or now < self.deadline_at):
            self.reminders += 1
            self._play_idle()                      # 间隔期回到低干扰待机
            self._dbg("提醒 %s 第 %d/%d 次，%gs 后"
                      % (source, self.reminders + 1, rp["maxTimes"], rp["intervalSec"]))
            self._pending = (rule, source, now + rp["intervalSec"])
            return
        self.silenced = True
        self._dbg("%s 提醒结束（%s），转为静默待机" % (source, rp["mode"]))
        self._play_idle()

    # ---- 状态 --------------------------------------------------------------
    def set_state(self, name, why=""):
        if name not in STATES or name == self.state:
            return
        prev = self.state
        self.state = name
        self.silenced = False
        self.reminders = 0
        self.deadline_at = 0
        now = self._now()
        if name == "idle":
            self._long_idle_at = now
        if name == "done":
            self._done_at = now
        if name == "needs_decision":
            self.deadline_at = now + self.decision_timeout
        self._dbg("切换 %s（%s%s）" % (name, why, ("，来自 " + prev) if prev else ""))
        rule = self.rules.get(name)
        if not rule:
            self._dbg("%s 没有规则 → 保持当前画面" % name)
            return
        if name == "idle" and self.playing == rule["clipId"]:
            self.cur_clip, self.cur_priority = rule["clipId"], rule["priority"]
            self.cur_interruptible = rule["interruptible"]
            self.hold_until = now + self._clip_ms(rule["clipId"]) / 1000.0
            self._dbg("已在播 %s，续播" % rule["clipId"])
            return
        self.request_play(rule, name)

    def user_input(self):
        if self.state in REMINDER_STATES:
            self._dbg("用户有输入 → 清除提醒（原状态 %s）" % self.state)
            self.set_state("busy", "user_input")

    def clear_reminder(self, why):
        if self.state in REMINDER_STATES:
            self._dbg("手动操作（%s）→ 清除提醒（原状态 %s）" % (why, self.state))
            self.set_state("idle", "manual-clear")

    def external(self, name, extra=None):
        """外部事件：done/busy/streaming/error/attention/user_input。"""
        extra = extra or {}
        if name == "attention":
            self.decision_timeout = _num(extra.get("timeoutSec"), 300)
            self.set_state("needs_decision", "attention:%s" % extra.get("kind", "?"))
        elif name == "user_input":
            self.user_input()
        else:
            self.set_state(name, "external")

    # ---- 每秒一次 ----------------------------------------------------------
    def tick(self):
        now = self._now()
        if self._pending and now >= self._pending[2] and not self.silenced:
            rule, source, _at = self._pending
            self._pending = None
            if self.state == rule["state"]:
                self.request_play(rule, source + ":reminder")

        if self.state == "idle" and now - self._long_idle_at > self.long_idle_sec:
            self.set_state("long_idle", "空闲 %ds" % int(self.long_idle_sec))
        if self.state == "done" and now - self._done_at > self.awaiting_user_sec:
            self.set_state("awaiting_user", "done 后 %ds 无输入" % int(self.awaiting_user_sec))
        if self.state == "needs_decision" and self.deadline_at and now > self.deadline_at:
            self._dbg("needs_decision 超时 → 降级为 idle")
            self.set_state("idle", "decision-timeout")
            return
        # 画面补齐：目标状态的 clip 没在播、且没有占用 → 重试（被冷却/占用拒过一次的情况）
        # 两个提醒态在"等下一次提醒"的间隙里不要补齐，否则会每 tick 重播一遍完整动作
        if not self.silenced and not self._pending and self.state:
            rule = self.rules.get(self.state)
            if rule and self.playing != rule["clipId"] and now >= self.hold_until:
                self.request_play(rule, self.state + ":reconcile")


def _print(path):
    clips_path = os.path.join(os.path.dirname(path), "clips.json")
    if os.path.exists(clips_path):
        arr = json.load(open(clips_path, encoding="utf-8"))["clips"]
    else:
        # pack 里可能只放了 state-map.json（并且 frames 由 pack 侧解析），这时用空集合
        arr = json.load(open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "deskpet-mini", "clips.json"),
            encoding="utf-8"))["clips"]
    clips = {c["id"]: {"fps": 24, "count": 1,
                       "name": c.get("displayName") or c.get("name", "")} for c in arr}
    sm = StateMap(path, clips, lambda *a: None, lambda: None, print)
    for st in STATES:
        r = sm.rules.get(st)
        print(("%-15s " % st) + ("(无)" if not r else
              "-> %-13s prio=%3g loop=%-5s cd=%gs repeat=%s"
              % (r["clipId"], r["priority"], r["loop"], r["cooldownSec"], r["repeat"])))


if __name__ == "__main__":
    import sys
    root = os.path.dirname(os.path.abspath(__file__))          # deskpet-mini/
    dflt = os.path.join(os.path.dirname(root), "packs", "dsh", "state-map.json")
    _print(sys.argv[1] if len(sys.argv) > 1 else dflt)
