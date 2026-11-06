//! DSH → 桌宠：本机 WebSocket 客户端。
//!
//! 只订阅 `ws://127.0.0.1:<dshPort>` 上 dsh-deskpet-bridge 插件的广播，把事件
//! 映射成状态机 payload 后用 `state-in` 事件交给前端。映射与 Python 版
//! `dsh_bridge.py` 的 `map_event` 逐条对齐：不回写、不改变插件行为，
//! 也不影响 Python 桥（两者可以同时连同一个端口）。
//!
//! 断线后指数退避（1→5s）重连；端口变化（设置里改 `dshPort`）也会立刻重连。

use serde_json::{json, Value};
use std::io::ErrorKind;
use std::net::{SocketAddr, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::Message;

static CONNECTED: AtomicBool = AtomicBool::new(false);
static RESTART: AtomicBool = AtomicBool::new(false);
static STARTED: AtomicBool = AtomicBool::new(false);

static NOTE: Mutex<String> = Mutex::new(String::new());
static LAST_EVENT: Mutex<String> = Mutex::new(String::new());
static LAST_ERROR: Mutex<String> = Mutex::new(String::new());

fn set_note(s: &str) {
    if let Ok(mut g) = NOTE.lock() {
        *g = s.to_string();
    }
}

fn set_error(s: &str) {
    if let Ok(mut g) = LAST_ERROR.lock() {
        *g = s.to_string();
    }
}

fn set_last_event(s: &str) {
    if let Ok(mut g) = LAST_EVENT.lock() {
        *g = s.to_string();
    }
}

fn get(m: &Mutex<String>) -> String {
    m.lock().map(|s| s.clone()).unwrap_or_else(|e| e.into_inner().clone())
}

/// 端口上是否有进程在监听（不建立 WS，只做 TCP 探测）。
fn listening(port: u16) -> bool {
    if port == 0 {
        return false;
    }
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_ok()
}

/// 设置页状态灯：端口、URL、是否已连上、端口是否在监听、最近事件/错误。
pub fn info() -> Value {
    let port = crate::dsh_port();
    json!({
        "port": port,
        "url": format!("ws://127.0.0.1:{}", port),
        "connected": CONNECTED.load(Ordering::SeqCst),
        "listening": listening(port),
        "note": get(&NOTE),
        "lastEvent": get(&LAST_EVENT),
        "lastError": get(&LAST_ERROR),
    })
}

/// 强制重连（设置页「重新检测」）：只置标志，线程在下一次读超时/循环时重连。
/// 不阻塞：调用方稍后再查 `info()` 即可看到新状态。
pub fn recheck() {
    RESTART.store(true, Ordering::SeqCst);
    set_note("reconnecting…");
    crate::log_line("dsh recheck requested");
}

/// 保存配置后端口可能变了：让线程按新端口重建连接。
pub fn restart() {
    RESTART.store(true, Ordering::SeqCst);
}

/// 常驻线程：连接 → 读事件 → 映射 → emit；断线退避重连。
pub fn start(app: AppHandle) {
    if STARTED.swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(move || {
        let mut backoff: u64 = 1;
        loop {
            let port = crate::dsh_port();
            if port == 0 {
                set_note("dshPort=0：已关闭");
                std::thread::sleep(Duration::from_secs(2));
                continue;
            }
            let url = format!("ws://127.0.0.1:{}", port);
            set_note(&format!("connecting {}", url));
            crate::log_line(&format!("dsh connect {}", url));

            match tungstenite::connect(&url) {
                Ok((mut ws, _resp)) => {
                    backoff = 1;
                    RESTART.store(false, Ordering::SeqCst);
                    CONNECTED.store(true, Ordering::SeqCst);
                    set_error("");
                    set_note(&format!("connected {}", url));
                    crate::log_line(&format!("dsh connected {}", url));
                    // 2s 读超时：让线程周期性检查 RESTART / 端口变化，也能给对端发 ping。
                    if let MaybeTlsStream::Plain(t) = ws.get_mut() {
                        let _ = t.set_read_timeout(Some(Duration::from_secs(2)));
                    }
                    pump(&app, &mut ws, port);
                    CONNECTED.store(false, Ordering::SeqCst);
                    set_note(&format!("disconnected {}", url));
                    crate::log_line(&format!("dsh disconnected {}", url));
                }
                Err(e) => {
                    CONNECTED.store(false, Ordering::SeqCst);
                    set_error(&format!("{}", e));
                    set_note(&format!("connect failed {}（{}s 后重试）", url, backoff));
                    crate::log_line(&format!("dsh connect_failed {} err={}", url, e));
                }
            }

            // 断线等待：可被 RESTART 打断，避免改端口后还要等满退避。
            let deadline = Instant::now() + Duration::from_secs(backoff);
            while Instant::now() < deadline {
                if RESTART.swap(false, Ordering::SeqCst) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            backoff = (backoff * 2).min(5);
        }
    });
}

/// 一轮连接内的事件循环：只在读超时/重连标志/端口变化时退出。
fn pump(app: &AppHandle, ws: &mut tungstenite::WebSocket<MaybeTlsStream<TcpStream>>, port: u16) {
    let mut ctx = Ctx::default();
    let mut last_sig = String::new();
    loop {
        if RESTART.load(Ordering::SeqCst) {
            return;
        }
        if crate::dsh_port() != port {
            return;
        }
        match ws.read() {
            Ok(Message::Text(txt)) => {
                let ev: Value = match serde_json::from_str(&txt) {
                    Ok(v) => v,
                    Err(_) => {
                        crate::log_line("dsh ignore non-json frame");
                        continue;
                    }
                };
                let ev_type = ev.get("type").and_then(|x| x.as_str()).unwrap_or("<no-type>");
                if let Some(payload) = map_event(&ev, &mut ctx) {
                    let sig = payload.to_string();
                    let state = payload.get("state").and_then(|x| x.as_str()).unwrap_or("");
                    // 连续相同的 token 事件不重复刷日志，但仍转发（前端靠它续期 TTL）。
                    if sig == last_sig && state != "streaming" {
                        continue;
                    }
                    last_sig = sig.clone();
                    set_last_event(&sig);
                    crate::log_line(&format!("dsh_state type={} {}", ev_type, sig));
                    let _ = app.emit("state-in", payload);
                }
            }
            Ok(Message::Ping(d)) => {
                let _ = ws.send(Message::Pong(d));
            }
            Ok(Message::Close(_)) => {
                crate::log_line("dsh peer closed");
                return;
            }
            Ok(_) => {}
            Err(tungstenite::Error::Io(e))
                if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) =>
            {
                // 读超时 = 空闲：ping 保活并回到循环顶部检查标志。
                let _ = ws.send(Message::Ping(b".".to_vec()));
            }
            Err(tungstenite::Error::ConnectionClosed) | Err(tungstenite::Error::AlreadyClosed) => {
                return;
            }
            Err(e) => {
                set_error(&format!("{}", e));
                crate::log_line(&format!("dsh read_error {}", e));
                return;
            }
        }
    }
}

#[derive(Default)]
struct Ctx {
    /// 是否在一轮对话中；轮内分段间隙的 idle 不能把画面打回待机。
    in_turn: bool,
    pending_question: Option<String>,
    pending_approval: Option<String>,
}

fn text_of(ev: &Value, key: &str) -> String {
    ev.get(key)
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .chars()
        .take(80)
        .collect()
}

fn status_state(s: &str) -> Option<&'static str> {
    match s {
        "running" | "busy" | "thinking" | "working" => Some("busy"),
        "idle" | "ready" | "stop" | "stopped" => Some("idle"),
        "completed" | "complete" | "done" => Some("done"),
        "failed" | "failure" | "error" => Some("error"),
        _ => None,
    }
}

/// DSH 广播 payload → 状态机 body；None 表示不关心（与 dsh_bridge.py 一致）。
fn map_event(ev: &Value, ctx: &mut Ctx) -> Option<Value> {
    let t = ev.get("type").and_then(|x| x.as_str())?;
    match t {
        "status" => {
            let s = ev
                .get("status")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_lowercase();
            let st = status_state(&s)?;
            match st {
                "idle" => {
                    if ctx.in_turn {
                        None
                    } else {
                        Some(json!({"state": "idle", "source": "dsh_state"}))
                    }
                }
                "busy" => {
                    ctx.in_turn = true;
                    Some(json!({"state": "busy", "timeoutSec": 120, "source": "dsh_state"}))
                }
                "done" => {
                    ctx.in_turn = false;
                    Some(json!({"state": "done", "source": "dsh_state"}))
                }
                "error" => {
                    ctx.in_turn = false;
                    Some(json!({
                        "state": "error", "timeoutSec": 10,
                        "why": text_of(ev, "message"), "source": "dsh_state"
                    }))
                }
                other => Some(json!({"state": other, "source": "dsh_state"})),
            }
        }
        // 轮内不用短 TTL：工具执行期间本来就没有 token，短 TTL 必然掉 idle。
        "token" => {
            ctx.in_turn = true;
            Some(json!({
                "state": "streaming", "timeoutSec": 120, "priority": 52, "source": "dsh_state"
            }))
        }
        // 这一段不再吐字，但整轮没结束（下一步通常是工具调用）→ 回思考态。
        "stream-end" | "tool-result" => {
            if ctx.in_turn {
                Some(json!({"state": "busy", "timeoutSec": 120, "source": "dsh_state"}))
            } else {
                None
            }
        }
        // DSH 正在问选择题：桌宠只切提醒态并常驻，不代答。
        "question-asked" => {
            ctx.pending_question = ev
                .get("callId")
                .and_then(|x| x.as_str())
                .map(|x| x.to_string());
            let why = match ev.get("questionCount").and_then(|x| x.as_u64()) {
                Some(n) => format!("ask_user_question:{}题", n),
                None => "ask_user_question".to_string(),
            };
            Some(json!({
                "state": "needs_decision",
                "why": why,
                "callId": ev.get("callId").cloned().unwrap_or(Value::Null),
                "title": text_of(ev, "title"),
                "questionCount": ev.get("questionCount").cloned().unwrap_or(Value::Null),
                "optionCount": ev.get("optionCount").cloned().unwrap_or(Value::Null),
                "source": "dsh_state"
            }))
        }
        // 只认我们推过的那一道题；别的问题结算不影响当前状态。
        "question-cleared" => {
            let call_id = ev.get("callId").and_then(|x| x.as_str());
            if call_id != ctx.pending_question.as_deref() {
                return None;
            }
            ctx.pending_question = None;
            if ctx.in_turn {
                Some(json!({"state": "busy", "timeoutSec": 120, "source": "dsh_state"}))
            } else {
                Some(json!({"state": "idle", "source": "dsh_state"}))
            }
        }
        // 审批弹窗（工具要用户点「允许/拒绝」）：也是等人决策，但没有 questions/options。
        "approval-asked" => {
            let id = ev.get("id").and_then(|x| x.as_str())?;
            ctx.pending_approval = Some(id.to_string());
            let tool = text_of(ev, "toolName");
            let why = if tool.is_empty() {
                "approval".to_string()
            } else {
                format!("approval:{}", tool)
            };
            Some(json!({
                "state": "needs_decision",
                "why": why,
                "id": id,
                "toolName": ev.get("toolName").cloned().unwrap_or(Value::Null),
                "callId": ev.get("callId").cloned().unwrap_or(Value::Null),
                "title": text_of(ev, "title"),
                "source": "dsh_state"
            }))
        }
        // 只认我们推过的那一条审批；别的审批结算不影响当前状态。
        "approval-decided" => {
            let id = ev.get("id").and_then(|x| x.as_str());
            if id != ctx.pending_approval.as_deref() {
                return None;
            }
            ctx.pending_approval = None;
            if ctx.in_turn {
                Some(json!({"state": "busy", "timeoutSec": 120, "source": "dsh_state"}))
            } else {
                Some(json!({"state": "idle", "source": "dsh_state"}))
            }
        }
        "done" => {
            ctx.in_turn = false;
            Some(json!({"state": "done", "source": "dsh_state"}))
        }
        "error" => {
            ctx.in_turn = false;
            Some(json!({
                "state": "error", "timeoutSec": 10,
                "why": text_of(ev, "message"), "source": "dsh_state"
            }))
        }
        _ => None,
    }
}
