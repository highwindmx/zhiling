"""DSH → 桌宠 最小桥：订阅 dsh-deskpet-bridge 的本地 WebSocket，转成 pet.py 的 POST /state。

前置条件：
  1. DSH 里已安装并启用 @local/dsh-deskpet-bridge（它在 ws://127.0.0.1:55471 广播事件）；
  2. 桌宠带 --state-port 8765 启动（pet.py --state-port 8765；pet.py 会自动拉起本脚本）；
  3. 也可手动常驻：.venv\\Scripts\\python.exe build\\tools\\dsh_bridge.py --verbose

事件映射（DSH 事件流 v2 → 主题映射 themes/<id>.json 的状态名）：
  status running|busy|thinking   -> busy（timeoutSec=120，且标记「轮内」）
  status idle|ready              -> idle（轮内忽略：分段间隙的 idle 不能把画面打回待机）
  status completed|done          -> done
  status failed|error            -> error
  token                          -> streaming（timeoutSec=120，句末 TTL 不掉 idle）
  stream-end                     -> busy（这一段不出字了，下一步通常是工具调用）
  tool-result                    -> busy（工具跑完，模型继续推理，重新续期）
  question-asked                 -> needs_decision（ask_user_question 待决策，常驻、不代答）
  question-cleared               -> busy（轮内）/ idle（轮外；同一 callId 的 tool/result 才认）
  approval-asked                 -> needs_decision（审批弹窗待确认，常驻、不代答）
  approval-decided               -> busy（轮内）/ idle（轮外；同一 approval id 才认）
  done / turn/end                -> done
  error                          -> error（timeoutSec=10）
  welcome / pong                 -> 忽略

排障：
  --probe     只探测 55471 与 8765 是否监听，并读插件 /health
  --selftest  连 55471，等 N 秒，打印收到的事件条数与示例（判断 DSH 到底有没有在说话）
  --verbose   把收到的原始事件（前 200 字符，token 不记正文）写进 dsh_bridge.log
只连本机回环，不监听端口，不引入第三方依赖。
"""
import argparse
import base64
import ctypes
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
sys.path.insert(0, ROOT)                       # 让 shared_constants 可从仓库根目录导入
from shared_constants import (BRIDGE_TTL_BUSY_SEC, BRIDGE_TTL_ERROR_SEC,
                         BRIDGE_STREAMING_PRIORITY, DEFAULT_DSH_WS_PORT,
                         DEFAULT_STATE_PORT)
LOG_PATH = os.path.join(ROOT, "build", "cache", "dsh_bridge.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
VERBOSE = True
MAX_LOG_LINES = 400          # 日志保留的最近行数，避免长期运行无限增长


def _trim_log(max_lines=MAX_LOG_LINES):
    """启动时把日志裁到最近 max_lines 行（保留最近 N 条）。"""
    try:
        if not os.path.exists(LOG_PATH):
            return
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        if len(lines) <= max_lines:
            return
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines[-max_lines:])
    except OSError:
        pass


_note_count = 0


def note(msg):
    global _note_count
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _note_count += 1
        if _note_count % 100 == 0:      # 长跑时定期裁掉老行，保留最近 N 条
            _trim_log()
    except OSError:
        pass
    if VERBOSE and sys.stdout is not None:
        print(line, flush=True)


def _redact(ev):
    """脱敏：token 的正文不落盘，只留字段名与长度。"""
    if not isinstance(ev, dict):
        return repr(ev)
    safe = {}
    for k, v in ev.items():
        if k in ("text", "content", "delta"):
            safe[k] = "<%d chars>" % len(v) if isinstance(v, str) else "<hidden>"
        elif isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        elif isinstance(v, dict):
            safe[k] = {kk: ("<%d chars>" % len(vv) if isinstance(vv, str) and kk in ("text", "content", "delta") else vv)
                       for kk, vv in list(v.items())[:6]}
        elif isinstance(v, list):
            safe[k] = "<list %d>" % len(v)
    return json.dumps(safe, ensure_ascii=False)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _send_frame(sock, payload, opcode=0x1):
    """客户端帧必须带 mask（RFC 6455）。"""
    key = os.urandom(4)
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    header = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += key
    masked = bytes(b ^ key[i & 3] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def _recv_frame(sock):
    b0, b1 = _recv_exact(sock, 2)
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    key = _recv_exact(sock, 4) if masked else None
    data = _recv_exact(sock, n) if n else b""
    if key:
        data = bytes(b ^ key[i & 3] for i, b in enumerate(data))
    return opcode, data


def connect_ws(host, port, timeout=5):
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET / HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (host, port, key)
    )
    sock.sendall(req.encode("latin-1"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake closed")
        resp += chunk
    head, _, rest = resp.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    if "101" not in status:
        raise ConnectionError("handshake rejected: %s" % status)
    note("ws connected %s:%d（%s）" % (host, port, status))
    return sock, rest


STATUS_MAP = {
    "running": "busy", "busy": "busy", "thinking": "busy", "working": "busy",
    "idle": "idle", "ready": "idle", "stop": "idle", "stopped": "idle",
    "completed": "done", "complete": "done", "done": "done",
    "failed": "error", "failure": "error", "error": "error",
}


def map_event(ev, ctx=None):
    """DSH 广播 payload → pet /state body；None 表示不关心。

    ctx 是可选的「一轮是否进行中」状态字典（run() 里跨事件复用）：
      轮内（in_turn=True）：status→busy、token 置位；stream-end/tool-result
      保留 in_turn 不清除（它们回 busy 或忽略，等后续 token 继续）。
      收尾（in_turn=False）：status→done/error、type=done/error、以及
      approval-decided / question-cleared 轮外结算。
    为什么需要它：工具调用期间 DSH 不再推 token，旧版只按 token 续期 8s TTL，
    工具跑超过 8 秒就会掉回 idle（实测 15:39:40）；另外 agent/status 在分段间隙
    也会推 idle，直接转发同样会把画面打回待机。现在 TTL 用 BRIDGE_TTL_BUSY_SEC
    （120s）兜底，工具跑再久也不会掉 idle。

    priority 覆盖说明：主题里 streaming=40、busy=50，而 DSH 的“开始跑”和“开始出字”
    几乎同时到达；若照规则优先级，busy 一旦占住，streaming 抢不过（仲裁要求严格大于）。
    所以这里给 streaming 抬到 BRIDGE_STREAMING_PRIORITY（52），保证“真的在出字”时切到打字动画。
    """
    ctx = ctx if ctx is not None else {}
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if t == "status":
        s = str(ev.get("status") or "").lower()
        st = STATUS_MAP.get(s)
        if st is None:
            return None
        if st == "idle":
            if ctx.get("in_turn"):
                return None                  # 轮内分段间隙的 idle，忽略
            return {"state": "idle"}
        if st == "busy":
            ctx["in_turn"] = True
            return {"state": "busy", "timeoutSec": BRIDGE_TTL_BUSY_SEC}
        if st == "done":
            ctx["in_turn"] = False
            return {"state": "done"}
        if st == "error":
            ctx["in_turn"] = False
            return {"state": "error", "timeoutSec": BRIDGE_TTL_ERROR_SEC, "why": str(ev.get("message") or "")[:80]}
        return {"state": st}
    if t == "token":
        # 轮内不再用 8s 短 TTL：工具执行期间本来就没有 token，短 TTL 必然掉 idle。
        # 真正的一轮结束由 turn/end（done）或轮外 idle 收尾，BRIDGE_TTL_BUSY_SEC 只是兜底防悬挂。
        ctx["in_turn"] = True
        return {"state": "streaming", "timeoutSec": BRIDGE_TTL_BUSY_SEC, "priority": BRIDGE_STREAMING_PRIORITY}
    if t == "stream-end":
        # 模型这一段不再吐字，但整轮没结束（下一步通常是工具调用）→ 先回思考态
        if ctx.get("in_turn"):
            return {"state": "busy", "timeoutSec": BRIDGE_TTL_BUSY_SEC}
        return None
    if t == "tool-result":
        # 工具刚跑完，模型要继续推理/继续调工具；重新续期 busy，避免 8s TTL 掉 idle
        if ctx.get("in_turn"):
            return {"state": "busy", "timeoutSec": BRIDGE_TTL_BUSY_SEC}
        return None
    if t == "question-asked":
        # DSH 正在问选择题（ask_user_question）。桌宠只切提醒态并常驻，
        # 不代答；用户看到提醒后自己回 DSH 界面选。
        ctx["pending_question"] = ev.get("callId")
        return {
            "state": "needs_decision",
            "why": "ask_user_question:%s题" % ev.get("questionCount"),
            "callId": ev.get("callId"),
            "title": str(ev.get("title") or "")[:120],
            "questionCount": ev.get("questionCount"),
            "optionCount": ev.get("optionCount"),
        }
    if t == "approval-asked":
        # 审批弹窗（工具要用户点「允许/拒绝」）。和 ask_user_question 同属“等人决策”，
        # 但事件形状不同：没有 questions[]/options[]，只有 id / toolName / reason。
        aid = ev.get("id")
        if not isinstance(aid, str) or not aid:
            return None
        ctx["pending_approval"] = aid
        return {
            "state": "needs_decision",
            "why": "approval:%s" % (ev.get("toolName") or "tool"),
            "id": aid,
            "toolName": ev.get("toolName"),
            "callId": ev.get("callId"),
            "title": str(ev.get("title") or ev.get("reason") or "")[:120],
            "source": "approval",
        }
    if t == "approval-decided":
        # 只认我们推过的那一条审批；别的审批结算不影响当前状态
        if ev.get("id") != ctx.get("pending_approval"):
            return None
        ctx["pending_approval"] = None
        if ctx.get("in_turn"):
            return {"state": "busy", "timeoutSec": BRIDGE_TTL_BUSY_SEC}
        return {"state": "idle"}
    if t == "question-cleared":
        # 只认我们推过的那一道题；别的问题结算不影响当前状态
        if ev.get("callId") != ctx.get("pending_question"):
            return None
        ctx["pending_question"] = None
        if ctx.get("in_turn"):
            return {"state": "busy", "timeoutSec": BRIDGE_TTL_BUSY_SEC}
        return {"state": "idle"}
    if t == "done":
        ctx["in_turn"] = False
        return {"state": "done"}
    if t == "error":
        ctx["in_turn"] = False
        return {"state": "error", "timeoutSec": BRIDGE_TTL_ERROR_SEC, "why": str(ev.get("message") or "")[:80]}
    return None


def health(port, host="127.0.0.1"):
    """读插件的 HTTP /health（不依赖 WS 也能确认插件活着）。"""
    conn = http.client.HTTPConnection(host, port, timeout=2)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        return resp.status, resp.read(1000).decode("utf-8", "ignore")
    finally:
        conn.close()


def post_state(port, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("POST", "/state", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read(500).decode("utf-8", "ignore")
    finally:
        conn.close()


def probe(args):
    """--probe：检查两端端口，并尽量读出插件 /health。"""
    def check(port, name):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1.5)
            s.close()
            note("probe %s 127.0.0.1:%d LISTENING" % (name, port))
            return True
        except OSError as e:
            note("probe %s 127.0.0.1:%d NOT LISTENING (%s)" % (name, port, e))
            return False

    ok_dsh = check(args.ws_port, "DSH 桥")
    ok_pet = check(args.pet_port, "桌宠 /state")
    if ok_dsh:
        try:
            code, body = health(args.ws_port)
            note("probe DSH /health %s %s" % (code, body[:400]))
        except OSError as e:
            note("probe DSH /health 读取失败: %s" % e)
    if ok_dsh and not ok_pet:
        note("结论：DSH 在广播，但桌宠没开 --state-port %d" % args.pet_port)
    elif not ok_dsh and ok_pet:
        note("结论：桌宠在听，但 DSH 插件没跑（55471 未监听），事件不会来")
    elif not ok_dsh and not ok_pet:
        note("结论：两端都没起来，先启动 DSH 与 pet.py --state-port %d" % args.pet_port)
    else:
        note("结论：两端都在，可运行本脚本做桥接")
    return 0 if (ok_dsh and ok_pet) else 1


def selftest(args):
    """--selftest：连 55471，等 N 秒，统计收到的事件并打印示例。"""
    note("selftest 开始：连 ws://127.0.0.1:%d，等 %.1fs" % (args.ws_port, args.selftest))
    try:
        code, body = health(args.ws_port)
        note("selftest 插件 /health %s %s" % (code, body[:400]))
    except OSError as e:
        note("selftest 插件 /health 失败: %s" % e)
    counts = {}
    samples = []
    total = 0
    control = 0
    deadline = time.time() + args.selftest
    try:
        sock, rest = connect_ws(args.ws_host, args.ws_port, timeout=min(5, args.selftest))
        sock.settimeout(1.0)
        while time.time() < deadline:
            try:
                opcode, data = _recv_frame(sock)
            except socket.timeout:
                continue
            if opcode == 0x9:
                _send_frame(sock, data, 0xA)
                continue
            if opcode in (0xA, 0x8):
                if opcode == 0x8:
                    break
                continue
            try:
                ev = json.loads(data.decode("utf-8"))
            except ValueError:
                counts["<non-json>"] = counts.get("<non-json>", 0) + 1
                continue
            t = str(ev.get("type") or "<no-type>")
            if t in ("welcome", "pong"):        # 握手/心跳不算“DSH 在说话”
                control += 1
                continue
            total += 1
            counts[t] = counts.get(t, 0) + 1
            if len(samples) < 6:
                samples.append(t + " " + _redact(ev)[:200])
            if args.verbose:
                note("raw %s" % _redact(ev)[:200])
        sock.close()
    except (OSError, ConnectionError) as e:
        note("selftest 连接失败: %s" % e)
        note("结论：55471 没在监听 —— DSH 插件没运行（见 build/cache/dsh_bridge_plugin.log）")
        return 2
    note("selftest 结果：%d 个事件（另有 %d 个 welcome/pong），类型分布=%s"
         % (total, control, json.dumps(counts, ensure_ascii=False)))
    for s in samples:
        note("selftest 示例 %s" % s)
    if total == 0:
        note("结论：连上了但 %.0fs 内没有任何事件。请在 DSH 里发一句消息再跑一次；"
             "若仍然为 0，说明 DSH 侧没有广播（看插件日志里的 subscribe/evt 行）" % args.selftest)
        return 1
    note("结论：DSH 确实在广播（%d 个事件）" % total)
    return 0


def _parent_alive(pid):
    if not pid:
        return True
    try:
        if sys.platform == "win32":
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not h:
                return False
            alive = ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x00000102  # WAIT_TIMEOUT
            ctypes.windll.kernel32.CloseHandle(h)
            return alive
        os.kill(int(pid), 0)
        return True
    except Exception:                                    # noqa: BLE001
        return True


def run(args):
    last_sig = None
    backoff = 1.0
    events = 0
    last_parent_check = 0.0
    turn_ctx = {}           # in_turn：跨事件复用；断线重连后清空（这段事件已丢）
    while True:
        if args.timeout and time.time() - args.started > args.timeout:
            note("timeout reached, exit")
            return 0
        if args.parent_pid and time.time() - last_parent_check > 5:
            last_parent_check = time.time()
            if not _parent_alive(args.parent_pid):
                note("父进程 %d 已退出，桥一并结束" % args.parent_pid)
                return 0
        try:
            sock, rest = connect_ws(args.ws_host, args.ws_port, timeout=args.read_timeout)
            backoff = 1.0
            if rest:
                note("handshake extra bytes=%d（忽略）" % len(rest))
            sock.settimeout(args.read_timeout)
            while True:
                if args.parent_pid and time.time() - last_parent_check > 5:
                    last_parent_check = time.time()
                    if not _parent_alive(args.parent_pid):
                        note("父进程 %d 已退出，桥一并结束" % args.parent_pid)
                        return 0
                opcode, data = _recv_frame(sock)
                if opcode == 0x8:
                    note("ws closed by peer")
                    break
                if opcode == 0x9:
                    _send_frame(sock, data, 0xA)
                    continue
                if opcode == 0xA:
                    continue
                try:
                    ev = json.loads(data.decode("utf-8"))
                except ValueError:
                    note("忽略非 JSON 帧")
                    continue
                if args.verbose:
                    note("raw %s" % _redact(ev)[:200])
                payload = map_event(ev, turn_ctx)
                if payload is None:
                    last_sig = None
                    continue
                sig = json.dumps(payload, sort_keys=True)
                if sig == last_sig:      # 连续 token 不重复打日志，但仍转发（续期 TTL）
                    if payload.get("state") != "streaming":
                        continue
                last_sig = sig
                try:
                    code, body = post_state(args.pet_port, payload)
                    note("POST /state %s -> %s %s" % (sig, code, body[:120]))
                except OSError as e:
                    note("POST /state 失败（桌宠没带 --state-port %d？）: %s" % (args.pet_port, e))
                    break
                events += 1
                if args.max_events and events >= args.max_events:
                    note("max-events reached, exit")
                    return 0
        except (OSError, ConnectionError) as e:
            note("ws error: %s（%.1fs 后重连）" % (e, backoff))
            turn_ctx.clear()                    # 断线期间事件不可见，别带着陈旧的 in_turn
            time.sleep(backoff)
            backoff = min(5.0, backoff * 2)


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="DSH → 桌宠状态桥")
    ap.add_argument("--ws-host", default="127.0.0.1")
    ap.add_argument("--ws-port", type=int, default=DEFAULT_DSH_WS_PORT)
    ap.add_argument("--pet-port", type=int, default=DEFAULT_STATE_PORT)
    ap.add_argument("--read-timeout", type=float, default=30.0)
    ap.add_argument("--timeout", type=float, default=0.0, help="总共跑多少秒后退出（0=常驻）")
    ap.add_argument("--max-events", type=int, default=0, help="转发多少个事件后退出（自测用）")
    ap.add_argument("--probe", action="store_true", help="只检查两端口是否可用")
    ap.add_argument("--selftest", type=float, default=0.0, metavar="SEC",
                    help="连 55471 等 SEC 秒，打印事件条数与示例（0 关闭）")
    ap.add_argument("--verbose", action="store_true", help="把收到的原始事件（脱敏，前 200 字符）写日志")
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="父进程 PID；父进程退出后本脚本自动结束（pet.py 自动拉起时传入）")
    args = ap.parse_args()
    VERBOSE = args.verbose            # 真正尊重 --verbose；默认 False 才走「只写日志、不打屏」
    args.started = time.time()
    _trim_log()
    if args.selftest:
        return selftest(args)
    if args.probe:
        return probe(args)
    note("bridge start v3 ws=%s:%d pet=127.0.0.1:%d verbose=%s parent=%d"
         % (args.ws_host, args.ws_port, args.pet_port, args.verbose, args.parent_pid))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
