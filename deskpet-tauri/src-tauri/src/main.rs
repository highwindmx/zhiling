// DeskPet Tauri 后端：透明窗口的显示/尺寸/位置/右键菜单/退出。
// 播放逻辑全在 frontend/src/main.js；前后端只通过少量命令通信，
// 尺寸与定位统一在这里按物理像素计算，避免 WebView 的 CSS 像素 / DPI 换算误差。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::{LogicalSize, Manager, PhysicalPosition};

static SHOWN: AtomicBool = AtomicBool::new(false);
static START: OnceLock<Instant> = OnceLock::new();

fn log_line(msg: &str) {
    if let Ok(path) = std::env::var("DESKPET_LOG") {
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
        {
            let _ = writeln!(f, "{}", msg);
        }
    }
}

fn elapsed_ms() -> u128 {
    START.get_or_init(Instant::now).elapsed().as_millis()
}

/// 第一帧画好后由前端调用；配置里 visible=false 是为了避免透明窗口白闪。
#[tauri::command]
fn show_window(app: tauri::AppHandle) -> Result<(), String> {
    if SHOWN.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
    let win = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    win.show().map_err(|e| e.to_string())?;
    log_line(&format!(
        "first_show_ms={} pid={}",
        elapsed_ms(),
        std::process::id()
    ));
    Ok(())
}

/// 三档尺寸 + 定位。reposition=true 时贴当前屏幕工作区右下角，
/// 否则保持左上角不动，只做边界夹取（与 pet.py 的切档行为一致）。
#[tauri::command]
fn set_tier(
    app: tauri::AppHandle,
    width: f64,
    height: f64,
    reposition: bool,
) -> Result<(), String> {
    let win = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    win.set_size(LogicalSize::new(width, height))
        .map_err(|e| e.to_string())?;

    let sf = win.scale_factor().map_err(|e| e.to_string())?;
    let pw = (width * sf).round() as i32;
    let ph = (height * sf).round() as i32;

    let monitor = if reposition {
        win.primary_monitor()
    } else {
        win.current_monitor()
    }
    .map_err(|e| e.to_string())?
    .or(win.primary_monitor().map_err(|e| e.to_string())?);

    if let Some(m) = monitor {
        let wa = m.work_area();
        let (margin_x, margin_y) = ((24.0 * sf).round() as i32, (12.0 * sf).round() as i32);
        let (mut x, mut y) = if reposition {
            (
                wa.position.x + wa.size.width as i32 - pw - margin_x,
                wa.position.y + wa.size.height as i32 - ph - margin_y,
            )
        } else {
            let p = win.outer_position().map_err(|e| e.to_string())?;
            (p.x, p.y)
        };
        let max_x = wa.position.x + wa.size.width as i32 - pw;
        let max_y = wa.position.y + wa.size.height as i32 - ph;
        x = x.clamp(wa.position.x, max_x.max(wa.position.x));
        y = y.clamp(wa.position.y, max_y.max(wa.position.y));
        win.set_position(PhysicalPosition::new(x, y))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[derive(serde::Deserialize)]
struct ActionItem {
    id: String,
    name: String,
}

#[derive(serde::Deserialize)]
struct MenuAt {
    x: i32,
    y: i32,
}

/// 原生右键菜单（HTML 菜单会被无边框窗口裁切，小档尤其明显）。
#[tauri::command]
fn show_menu(
    app: tauri::AppHandle,
    tier: String,
    actions: Vec<ActionItem>,
    at: Option<MenuAt>,
) -> Result<(), String> {
    log_line(&format!(
        "menu_open tier={} actions={}",
        tier,
        actions.len()
    ));
    let win = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    let menu = Menu::new(&win).map_err(|e| e.to_string())?;

    for name in ["小", "中", "大"] {
        let item = CheckMenuItem::with_id(
            &win,
            format!("tier:{}", name),
            format!("尺寸：{}", name),
            true,
            name == tier,
            None::<&str>,
        )
        .map_err(|e| e.to_string())?;
        menu.append(&item).map_err(|e| e.to_string())?;
    }

    menu.append(&PredefinedMenuItem::separator(&win).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;

    for a in actions {
        let item = MenuItem::with_id(
            &win,
            format!("action:{}", a.id),
            format!("动作：{}", a.name),
            true,
            None::<&str>,
        )
        .map_err(|e| e.to_string())?;
        menu.append(&item).map_err(|e| e.to_string())?;
    }

    menu.append(&PredefinedMenuItem::separator(&win).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let quit = MenuItem::with_id(&win, "quit", "退出", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    menu.append(&quit).map_err(|e| e.to_string())?;

    if let Some(p) = at {
        win.popup_menu_at(&menu, PhysicalPosition::new(p.x, p.y))
            .map_err(|e| e.to_string())
    } else {
        win.popup_menu(&menu).map_err(|e| e.to_string())
    }
}

#[tauri::command]
fn log_event(msg: String) {
    log_line(&msg);
}

// ---- 点击穿透 --------------------------------------------------------------
// 方案：窗口级 set_ignore_cursor_events（Rust）+ 前端降采样 alpha 掩码判断指针是否在
// 宠物身上。没选子类化 WM_NCHITTEST：Tauri 2 没有暴露窗口过程，得用
// SetWindowSubclass + unsafe 回调，还要把掩码每帧通过 IPC 推给 Rust；而
// set_ignore_cursor_events 是官方 API，只要 50ms 轮询一次指针位置就能做到同样的
// 效果。代价是轮询有 0~50ms 延迟（快速"移入即点击"可能漏一拍），已在 README 记录。

#[repr(C)]
struct WinPoint {
    x: i32,
    y: i32,
}

#[link(name = "user32")]
extern "system" {
    fn GetCursorPos(p: *mut WinPoint) -> i32;
}

#[derive(serde::Serialize)]
struct CursorLocal {
    x: i32,
    y: i32,
    inside: bool,
}

/// 指针相对窗口左上角的物理像素坐标（窗口无边框，outer 即内容区原点）。
#[tauri::command]
fn cursor_local(app: tauri::AppHandle) -> Result<CursorLocal, String> {
    let win = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    let mut p = WinPoint { x: 0, y: 0 };
    let ok = unsafe { GetCursorPos(&mut p) };
    let origin = win.outer_position().map_err(|e| e.to_string())?;
    let size = win.inner_size().map_err(|e| e.to_string())?;
    let (x, y) = (p.x - origin.x, p.y - origin.y);
    let inside = ok != 0
        && x >= 0
        && y >= 0
        && x < size.width as i32
        && y < size.height as i32;
    Ok(CursorLocal { x, y, inside })
}

#[tauri::command]
fn set_click_through(app: tauri::AppHandle, ignore: bool) -> Result<(), String> {
    let win = app
        .get_webview_window("main")
        .ok_or("main window missing")?;
    win.set_ignore_cursor_events(ignore)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    log_line(&format!("quit_ms={} pid={}", elapsed_ms(), std::process::id()));
    app.exit(0);
}

fn on_menu_event(app: &tauri::AppHandle, id: &str) {
    log_line(&format!("menu_pick id={}", id));
    if let Some(win) = app.get_webview_window("main") {
        let id = id.replace('\\', "").replace('\'', "");
        let _ = win.eval(&format!(
            "window.__deskpetPick && window.__deskpetPick('{}')",
            id
        ));
    }
}

fn main() {
    START.get_or_init(Instant::now);
    log_line(&format!("app_start pid={}", std::process::id()));
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            show_window,
            set_tier,
            show_menu,
            log_event,
            quit_app,
            cursor_local,
            set_click_through
        ])
        .on_menu_event(|app, event| on_menu_event(app, event.id().0.as_str()))
        .setup(|app| {
            // 兜底：前端脚本万一加载失败，2.5s 后强制显示窗口，避免「进程在、窗口不在」。
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(2500));
                if !SHOWN.swap(true, Ordering::SeqCst) {
                    if let Some(win) = handle.get_webview_window("main") {
                        let _ = win.show();
                        log_line(&format!(
                            "first_show_ms={} pid={} fallback=1",
                            elapsed_ms(),
                            std::process::id()
                        ));
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
