"""状态映射 v2：主题索引 packs/themes/<id>.json → clipIds[] 顺序轮播。

契约（与 docs/packs-v2-architecture-V1.md 一致）：
- 主题文件顶层 {"version":2,"id","name","longIdleSec","awaitingUserSec","states":{...}}
- states 共 13 行（long_idle 拆成 ①/② 两档；focus / focus_break 供番茄钟使用），每行：
    clipIds[]            0~N 段动画；空数组 = 到达该状态时保持当前画面（一等公民）
    fallbackClipIds[]    只在池里的段缺失/被删时补位，不参与轮播
    priority / minPlayMs / cooldownSec / loop / interruptible / repeat
    afterSec             仅 long_idle_1/2 用（分档门槛）
- 顺序轮播：每状态一个游标，每次成功 request_play 推进一格并循环。

刻意不依赖 Qt 与 pack.py：播放动作通过回调传入，方便脱离 GUI 单测：
    python state_map.py packs/themes/maid-dsh.json
"""
import json
import os
import sys
import time

# 跨端常量（state_map / pet / Tauri shared.js 共享，单一真相源）。
# state_map.py 位于仓库根目录，直接 import；被其它目录脚本导入时退回本地定义。
try:
    from v2_constants import (STATES, REMINDER_STATES, HOLD_STATES,
                              LONG_IDLE_SEC, AWAITING_USER_SEC,
                              LONG_IDLE_2_AFTER_SEC, DECISION_TIMEOUT_SEC)
except Exception:  # noqa: BLE001 - 极端情况下（根目录不在 sys.path）保留内联兜底
    STATES = ("double_click", "click", "error", "needs_decision", "done",
              "busy", "focus", "streaming", "awaiting_user", "focus_break",
              "long_idle_2", "long_idle_1", "idle")
    REMINDER_STATES = ("awaiting_user", "needs_decision")
    HOLD_STATES = ("done", "error", "needs_decision")
    LONG_IDLE_SEC = 600
    AWAITING_USER_SEC = 30
    LONG_IDLE_2_AFTER_SEC = 1800
    DECISION_TIMEOUT_SEC = 300

_LOG_MAX = 20


def _num(v, dflt):
    try:
        n = float(v)
        return n if n >= 0 else dflt
    except (TypeError, ValueError):
        return dflt


def _repeat(raw):
    rp = raw if isinstance(raw, dict) else None
    if rp is None:
        return None
    return {
        "mode": "interval" if rp.get("mode") == "interval" else "once",
        "intervalSec": _num(rp.get("intervalSec"), 75),
        "maxTimes": max(1, int(_num(rp.get("maxTimes"), 3))),
    }


def parse_state_map(text, clip_ids):
    """解析主题索引。坏一行只废一行；返回 (rules, long_idle_sec, awaiting_user_sec, errors)。

    clip_ids 是全局动作库（assetId 集合）。池中不存在的段会被剔除并记 errors；
    剔除后若整池为空，才依次用 fallbackClipIds 里可用的段补位。
    """
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("主题文件不是 JSON 对象")
    states = raw.get("states")
    if not isinstance(states, dict):
        raise ValueError("缺 states（v1 state-map.json 的 rules[] 格式已不再支持）")
    rules, errors = {}, []
    known = set(clip_ids)
    for st in STATES:
        r = states.get(st)
        if r is not None and not isinstance(r, dict):
            errors.append("%s 不是对象 → 按零绑定处理" % st)
            r = {}
        r = r or {}
        wanted = [c for c in (r.get("clipIds") or []) if isinstance(c, str)]
        pool = [c for c in wanted if c in known]
        missing = [c for c in wanted if c not in known]
        if missing:
            errors.append("%s 缺段 %s，已剔除" % (st, ",".join(missing)))
        if not pool and not wanted:
            fb = [c for c in (r.get("fallbackClipIds") or [])
                  if isinstance(c, str) and c in known]
            if fb:
                pool = fb[:]
                errors.append("%s 主池为空，用备用段 %s 补位" % (st, ",".join(pool)))
        rp = _repeat(r.get("repeat"))
        if rp and st not in REMINDER_STATES:
            rp["mode"] = "once"                 # repeat 只对提醒态有效
        rules[st] = {
            "state": st,
            "clipIds": pool,
            "fallbackClipIds": [c for c in (r.get("fallbackClipIds") or [])
                                if isinstance(c, str) and c in known],
            "cursor": 0,
            "clipId": pool[0] if pool else "",   # 运行时字段：本次轮到的段
            "afterSec": _num(r.get("afterSec"), 0),
            "priority": _num(r.get("priority"), 20),
            "minPlayMs": _num(r.get("minPlayMs"), 0),
            "cooldownSec": _num(r.get("cooldownSec"), 0),
            "loop": bool(r.get("loop")),
            "interruptible": r.get("interruptible", True) is not False,
            "repeat": rp,
        }
    return (rules, _num(raw.get("longIdleSec"), LONG_IDLE_SEC),
            _num(raw.get("awaitingUserSec"), AWAITING_USER_SEC), errors)


class StateMap:
    """状态机 + 仲裁。播放通过回调交给调用方（pet.py 的播放器）。"""

    def __init__(self, path, clips, play, play_idle, log, now=time.monotonic):
        self.path = path
        self.clips = clips                 # assetId -> {"fps","count","name"}
        self._play = play                  # play(asset_id, loop, on_end)
        self._play_idle = play_idle
        self._log = log
        self._now = now
        self.log_lines = []
        self.rules = {}
        self.long_idle_sec = LONG_IDLE_SEC
        self.awaiting_user_sec = AWAITING_USER_SEC
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
        self._pending = None
        self._ttl = None
        self.decision_timeout = DECISION_TIMEOUT_SEC
        self.hold_reminders = False        # 提醒态常驻开关（pet.py 按 config.json 设置）
        self.playing = None                # 当前真实在播的 assetId（由 view 层回填）
        self.load()

    # ---- 配置 --------------------------------------------------------------
    def load(self, force=False):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if force:
                self._log("主题索引缺失: %s（保持当前规则）" % self.path)
            return False
        if not force and mtime == self._mtime:
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:                       # 读盘失败：文件被占/删，保留上一版
            self._log("主题索引读取失败（保留上一版）: %s" % e)
            return False
        try:
            rules, long_idle, awaiting, errors = parse_state_map(text, set(self.clips))
        except Exception as e:                     # noqa: BLE001 - 坏文件保留上一版
            self._log("主题索引解析失败（保留上一版）: %s" % e)
            return False
        for e in errors:
            self._log(e)
        self.rules, self.long_idle_sec, self.awaiting_user_sec = rules, long_idle, awaiting
        self._mtime = mtime
        self._log("主题索引载入: %d 行 / %d 行有绑定 longIdle=%ds awaitingUser=%ds"
                  % (len(rules), sum(1 for r in rules.values() if r["clipIds"]),
                     int(long_idle), int(awaiting)))
        return True

    # ---- 仲裁 --------------------------------------------------------------
    def _dbg(self, msg):
        line = "%s | state=%s cur=%s" % (msg, self.state or "-", self.cur_clip or "-")
        self.log_lines.append(line)
        if len(self.log_lines) > _LOG_MAX:
            self.log_lines.pop(0)
        self._log(line)

    def _reject(self, rule, source, why):
        self._dbg("拒绝 %s→%s：%s" % (source, rule.get("clipId") or "∅", why))
        return False

    def _clip_ms(self, cid):
        c = self.clips.get(cid) or {}
        return (c.get("count", 1) / float(c.get("fps") or 24)) * 1000.0

    def _next_pick(self, rule):
        """顺序轮播：取游标指向的段，然后推进一格并循环。"""
        pool = rule["clipIds"]
        idx = rule.get("cursor", 0) % len(pool)
        rule["cursor"] = (idx + 1) % len(pool)
        return pool[idx]

    def _holding(self, rule=None):
        """当前（或给定规则）是否处于「提醒态常驻」：只对 done/error/needs_decision 生效。"""
        st = (rule or {}).get("state") or self.state
        return bool(self.hold_reminders) and st in HOLD_STATES

    def request_play(self, rule, source, force=False):
        """force=True 跳过冷却与占用仲裁：只用于「同一状态内的续播/轮播」和用户主动清除。"""
        pool = rule["clipIds"]
        if not pool:
            # 零绑定是一等公民：到达该状态时不换画面，也不算错误
            self._dbg("命中 %s→∅（零绑定，保持当前画面）" % source)
            return False
        pick = self._next_pick(rule)
        rule["clipId"] = pick
        now = self._now()
        last = self._last_play.get(pick, -1e12)
        if not force:
            if now < last + rule["cooldownSec"]:
                return self._reject(rule, source, "cooldown 剩余 %.1fs"
                                    % (last + rule["cooldownSec"] - now))
            if now < self.hold_until:
                if not self.cur_interruptible:
                    return self._reject(rule, source, "hold:%s" % self.cur_clip)
                if rule["priority"] <= self.cur_priority:
                    return self._reject(rule, source, "busy:%s" % self.cur_clip)
        self._last_play[pick] = now
        self.cur_clip = pick
        self.cur_priority = rule["priority"]
        self.cur_interruptible = rule["interruptible"]
        # 只有「单段池」才让播放器真的无限循环；多段池每段只播一遍，
        # 播完由 _clip_finished 推进游标 —— 否则第二段永远轮不到（loop=True 时不会有回调）。
        loop_clip = len(pool) == 1 and (rule["loop"] or self._holding(rule))
        self.hold_until = now + max(self._clip_ms(pick), rule["minPlayMs"]) / 1000.0
        self._dbg("命中 %s→%s（%s）池 %d 段 游标=%d prio=%g loop=%s"
                  % (source, pick, (self.clips.get(pick) or {}).get("name", ""),
                     len(pool), rule["cursor"], rule["priority"], loop_clip))
        self._play(pick, loop_clip, lambda: self._clip_finished(rule, source))
        return True

    def _clip_finished(self, rule, source):
        self.hold_until = 0
        if self.state != rule.get("state"):
            return                          # 播完前状态已切走，别再往回抢
        pool = rule["clipIds"]
        base = source.split(":cycle")[0].split(":hold")[0]
        # 状态级循环 / 提醒常驻：多段池按游标顺序接着播下一段，不换状态
        if len(pool) > 1 and (rule["loop"] or self._holding(rule)):
            if not self.request_play(rule, base + ":cycle", force=True):
                self._play_idle()
            return
        if self._holding(rule):
            # 单段池已在 request_play 里按 loop 播放；这里只兜底循环被打断的情况
            self.request_play(rule, base + ":hold", force=True)
            return
        rp = rule["repeat"]
        if not rp or rule["loop"]:
            if self.state != "idle":
                self.set_state("idle", "clip-end")
            return
        now = self._now()
        if rp["mode"] == "interval" and self.reminders + 1 < rp["maxTimes"] \
                and (self.deadline_at == 0 or now < self.deadline_at):
            self.reminders += 1
            self._play_idle()
            self._dbg("提醒 %s 第 %d/%d 次，%gs 后"
                      % (source, self.reminders + 1, rp["maxTimes"], rp["intervalSec"]))
            self._pending = (rule, source, now + rp["intervalSec"])
            return
        self.silenced = True
        self._dbg("%s 提醒结束（%s），转为静默待机" % (source, rp["mode"]))
        self._play_idle()

    # ---- 状态 --------------------------------------------------------------
    def set_state(self, name, why="", priority=None, timeout_sec=None, force=False):
        """切状态。priority=None 用主题里的值；显式传入只覆盖本次仲裁（不落盘）。

        force=True 只在用户主动操作（点击/输入）时使用：跳过冷却与优先级占用，
        否则「提醒态常驻」中的 done/error 会挡住清除。
        """
        if name not in STATES:
            self._dbg("未知状态 %r → 忽略" % (name,))
            return False
        if name == self.state:
            if timeout_sec:
                self._ttl = (name, self._now() + _num(timeout_sec, 0))
            return False
        prev = self.state
        prev_silenced, prev_reminders = self.silenced, self.reminders
        prev_deadline = self.deadline_at
        prev_ttl = self._ttl
        self.state = name
        self.silenced = False
        self.reminders = 0
        self.deadline_at = 0
        now = self._now()
        self._ttl = (name, now + _num(timeout_sec, 0)) if timeout_sec else None
        if name == "idle":
            self._long_idle_at = now
        if name == "done":
            self._done_at = now
        if name == "needs_decision":
            self.deadline_at = now + self.decision_timeout
        self._dbg("切换 %s（%s%s）" % (name, why, ("，来自 " + prev) if prev else ""))
        rule = self.rules.get(name)
        if not rule:
            self.state = prev
            self._dbg("%s 没有规则 → 保持 %s" % (name, prev or "-"))
            return False
        if priority is not None:
            try:
                p = float(priority)
            except (TypeError, ValueError):
                p = None
            if p is not None and p != rule["priority"]:
                rule = dict(rule)
                rule["priority"] = p
        # 已在播池里任一段就续播，避免 idle 回位时把游标白推一格
        if name == "idle" and self.playing in rule["clipIds"]:
            self.cur_clip, self.cur_priority = self.playing, rule["priority"]
            self.cur_interruptible = rule["interruptible"]
            self.hold_until = now + self._clip_ms(self.playing) / 1000.0
            self._dbg("已在播 %s，续播" % self.playing)
            return True
        ok = self.request_play(rule, name, force=force)
        if not ok and rule["clipIds"]:
            # 被冷却/高优先级占用拒掉时回滚状态名，否则会出现
            # “state=streaming 但画面还在播 handup”的错位（提醒也会被误取消）
            self.state = prev
            self.silenced = prev_silenced
            self.reminders = prev_reminders
            self.deadline_at = prev_deadline
            self._ttl = prev_ttl
            self._dbg("%s 未生效 → 保持 %s" % (name, prev or "-"))
        return ok

    def _is_reminder(self):
        """当前状态算不算「需要用户回应的提醒」（含常驻模式下的 done/error）。"""
        return self.state in REMINDER_STATES or (self.hold_reminders and self.state in HOLD_STATES)

    def user_input(self):
        if self._is_reminder():
            self._dbg("用户有输入 → 清除提醒（原状态 %s）" % (self.state,))
            self.set_state("busy", "user_input", force=True)

    def clear_reminder(self, why):
        if self._is_reminder():
            self._dbg("手动操作（%s）→ 清除提醒（原状态 %s）" % (why, self.state))
            self.set_state("idle", "manual-clear", force=True)

    def external(self, name, extra=None):
        """外部事件：done/busy/streaming/error/attention/user_input。"""
        extra = extra or {}
        if name == "attention":
            self.decision_timeout = _num(extra.get("timeoutSec"), DECISION_TIMEOUT_SEC)
            self.set_state("needs_decision", "attention:%s" % extra.get("kind", "?"))
        elif name == "user_input":
            self.user_input()
        else:
            if name not in self.rules:
                self._dbg("外部状态 %s 在主题索引里没有规则 → 忽略" % name)
                return False
            return self.set_state(name, "external", priority=extra.get("priority"),
                                  timeout_sec=extra.get("timeoutSec"))

    # ---- 每秒一次 ----------------------------------------------------------
    def tick(self):
        now = self._now()
        if self._ttl and now >= self._ttl[1] and self.state == self._ttl[0]:
            st = self._ttl[0]
            self._ttl = None
            if self.hold_reminders and st in HOLD_STATES:
                # 常驻模式下不做 TTL 回落（桥给 error 的 timeoutSec=10 会把它打回 idle）
                self._dbg("%s 超时但提醒常驻 → 保持" % st)
            else:
                self._dbg("%s 超时 → 回落 idle" % st)
                self.hold_until = 0
                self.set_state("idle", "state-timeout")
                return
        if self._pending and now >= self._pending[2] and not self.silenced:
            rule, source, _at = self._pending
            self._pending = None
            if self.state == rule["state"]:
                self.request_play(rule, source + ":reminder")

        # 长待机两档：LONG_IDLE_SEC → ①，LONG_IDLE_2_AFTER_SEC → ②（afterSec 写在各自状态行里）
        if self.state == "idle" and now - self._long_idle_at > self.long_idle_sec:
            self.set_state("long_idle_1", "空闲 %ds" % int(self.long_idle_sec))
        if self.state == "long_idle_1":
            elapsed = now - self._long_idle_at
            gate = (self.rules.get("long_idle_2") or {}).get("afterSec", LONG_IDLE_2_AFTER_SEC)
            if elapsed >= gate and (self.rules.get("long_idle_2") or {}).get("clipIds"):
                self.set_state("long_idle_2", "空闲升档 %ds" % int(gate))
        if self.state == "done" and now - self._done_at > self.awaiting_user_sec \
                and not self._holding():
            self.set_state("awaiting_user", "done 后 %ds 无输入" % int(self.awaiting_user_sec))
        if self.state == "needs_decision" and self.deadline_at and now > self.deadline_at \
                and not self._holding():
            self._dbg("needs_decision 超时 → 降级为 idle")
            self.set_state("idle", "decision-timeout")
            return
        # 画面补齐：目标状态的池里没有一段在播、且没有占用 → 重试
        # 用「是否在池内」判断，不用 rule["clipId"]，否则多段池会每 tick 白推游标
        if not self.silenced and not self._pending and self.state:
            rule = self.rules.get(self.state)
            if rule and rule["clipIds"] and self.playing not in rule["clipIds"] \
                    and now >= self.hold_until:
                self.request_play(rule, self.state + ":reconcile")


def _print(path):
    root = os.path.dirname(os.path.abspath(__file__))
    sys_path = os.path.join(root, "build", "tools")
    import sys
    sys.path.insert(0, sys_path)
    import pack
    assets = pack.list_assets()
    clips = {k: {"fps": v.get("fps", 24), "count": v.get("count", 1),
                 "name": v.get("name") or k} for k, v in assets.items()}
    sm = StateMap(path, clips, lambda *a: None, lambda: None, print)
    for st in STATES:
        r = sm.rules.get(st)
        print(("%-15s " % st) + ("(无)" if not r else
              "-> %-24s prio=%3g loop=%-5s cd=%gs repeat=%s"
              % ("/".join(r["clipIds"]) or "∅", r["priority"], r["loop"],
                 r["cooldownSec"], r["repeat"])))


if __name__ == "__main__":
    import sys
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(root, "build", "tools"))
    import pack
    dflt = pack.theme_path(pack.active_theme())   # 素材目录统一解析（环境变量 / config / Documents / 就地）
    _print(sys.argv[1] if len(sys.argv) > 1 else dflt)
