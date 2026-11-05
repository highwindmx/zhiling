// DeskPet Tauri 后端：透明窗口的显示/尺寸/位置/右键菜单/退出。
// 播放逻辑全在 frontend/src/main.js；前后端只通过少量命令通信，
// 尺寸与定位统一在这里按物理像素计算，避免 WebView 的 CSS 像素 / DPI 换算误差。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::Write;
use std::path::{Path, PathBuf};
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

// ---- 素材目录：运行时外置 --------------------------------------------------
// 帧和 mp4 不打进 exe（以后要接不同素材系列，不能每换一套就重新编译）。启动时按
// 下面的顺序找素材根目录：
//   1. 环境变量 DESKPET_ASSETS（绝对路径，调试/多 pack 并存时用）
//   2. exe 同级的 assets/（发布形态：exe + assets 一起分发）
// 前端通过自定义协议 deskpet:// 读取（Windows 上映射成 http://deskpet.localhost/）。
// 选自定义协议而不是内置 asset 协议的原因：asset 协议的 scope 必须写死在
// tauri.conf.json 里（编译期就固定，运行时改不了），而这里素材目录要能换；自定义
// 协议的路径校验放在下面的 serve_asset 里，只允许素材目录内的白名单后缀，
// 因此 tauri.conf.json 不需要开任何 scope（详见 README「素材外置」）。
static ASSET_ROOT: OnceLock<PathBuf> = OnceLock::new();

fn asset_root() -> &'static PathBuf {
    ASSET_ROOT.get_or_init(|| {
        if let Ok(p) = std::env::var("DESKPET_ASSETS") {
            if !p.trim().is_empty() {
                return PathBuf::from(p);
            }
        }
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(Path::to_path_buf))
            .unwrap_or_else(|| PathBuf::from("."));
        exe_dir.join("assets")
    })
}

fn mime_of(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| s.to_ascii_lowercase())
        .as_deref()
    {
        Some("json") => "application/json; charset=utf-8",
        Some("webp") => "image/webp",
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("svg") => "image/svg+xml",
        Some("mp4") => "video/mp4",
        _ => "application/octet-stream",
    }
}

fn asset_response(status: u16, mime: &'static str, body: Vec<u8>) -> tauri::http::Response<Vec<u8>> {
    tauri::http::Response::builder()
        .status(status)
        .header("Content-Type", mime)
        // 页面来自 http://tauri.localhost，跨到 http://deskpet.localhost 需要 CORS 头
        .header("Access-Control-Allow-Origin", "*")
        // 不允许缓存：用户改完 pack 里的帧/配置后，刷新即生效
        .header("Cache-Control", "no-cache")
        .body(body)
        .unwrap_or_else(|_| tauri::http::Response::new(Vec::new()))
}

fn percent_decode(s: &str) -> Option<String> {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            if i + 2 >= bytes.len() {
                return None;
            }
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).ok()?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// 自定义协议处理器：把 `deskpet://.../frames_x/f0000.webp` 映射成素材目录里的文件。
fn serve_asset(req: tauri::http::Request<Vec<u8>>) -> tauri::http::Response<Vec<u8>> {
    let raw = req.uri().path().trim_start_matches('/');
    let rel = match percent_decode(raw) {
        Some(p) => p,
        None => return asset_response(400, "text/plain; charset=utf-8", b"bad path".to_vec()),
    };
    // 只允许相对路径 + 白名单后缀，杜绝 ../ 跳出素材目录
    if rel.is_empty() || rel.contains("..") || rel.starts_with('/') || rel.contains('\\') {
        return asset_response(403, "text/plain; charset=utf-8", b"forbidden".to_vec());
    }
    let full = asset_root().join(&rel);
    let ok_ext = matches!(
        full.extension().and_then(|e| e.to_str()).map(|s| s.to_ascii_lowercase()),
        Some(ref e) if matches!(e.as_str(), "json" | "webp" | "png" | "jpg" | "jpeg" | "svg" | "mp4")
    );
    if !ok_ext {
        return asset_response(403, "text/plain; charset=utf-8", b"bad extension".to_vec());
    }
    match std::fs::read(&full) {
        Ok(body) => asset_response(200, mime_of(&full), body),
        Err(e) => {
            log_line(&format!("asset_404 rel={} err={}", rel, e));
            asset_response(404, "text/plain; charset=utf-8", format!("missing: {}", rel).into_bytes())
        }
    }
}

#[derive(serde::Serialize)]
struct AssetsInfo {
    root: String,
    exists: bool,
    clips: bool,
    state_map: bool,
}

/// 前端启动时先问素材目录在哪：缺素材时用它给出明确的路径提示，而不是黑窗。
#[tauri::command]
fn assets_info() -> AssetsInfo {
    let root = asset_root().clone();
    AssetsInfo {
        exists: root.is_dir(),
        clips: root.join("clips.json").is_file(),
        state_map: root.join("state-map.json").is_file(),
        root: root.display().to_string(),
    }
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
    let ws = MenuItem::with_id(&win, "workshop", "素材工坊…", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    menu.append(&ws).map_err(|e| e.to_string())?;
    let about = MenuItem::with_id(&win, "about_assets", "关于素材…", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    menu.append(&about).map_err(|e| e.to_string())?;
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

/// 素材工坊是 Python 工具（deskpet-python/workshop.bat）。Tauri 版是单 exe，不依赖 Python：
/// 能找到 workshop.bat 就拉起来，找不到就把原因回给前端提示，不静默失败、也不把工坊重写成 Rust。
fn find_workshop() -> Option<PathBuf> {
    let mut cands: Vec<PathBuf> = Vec::new();
    if let Ok(p) = std::env::var("DESKPET_WORKSHOP") {
        cands.push(PathBuf::from(p));
    }
    if let Some(root) = ASSET_ROOT.get() {
        // exe 同级 assets/ → 仓库 deskpet-python/（开发布局）
        cands.push(root.join("..").join("deskpet-python").join("workshop.bat"));
        cands.push(root.join("workshop.bat"));
    }
    if let Ok(cwd) = std::env::current_dir() {
        cands.push(cwd.join("deskpet-python").join("workshop.bat"));
        cands.push(cwd.join("workshop.bat"));
        cands.push(cwd.join("..").join("deskpet-python").join("workshop.bat"));
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            cands.push(dir.join("workshop.bat"));
            cands.push(dir.join("deskpet-python").join("workshop.bat"));
        }
    }
    cands.into_iter().find(|p| p.is_file())
}

#[tauri::command]
fn open_workshop() -> Result<String, String> {
    let p = find_workshop().ok_or_else(|| {
        "找不到 workshop.bat：素材工坊是 Python 工具，Tauri 版单 exe 不带 Python。\
把 deskpet-python/workshop.bat 放到 assets 同级，或设置环境变量 DESKPET_WORKSHOP 指向它；\
也可以直接双击 deskpet-python\\workshop.bat。"
            .to_string()
    })?;
    log_line(&format!("workshop_launch path={}", p.display()));
    let dir = p
        .parent()
        .map(|d| d.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    std::process::Command::new("cmd")
        .args(["/c", "start", "", &p.to_string_lossy()])
        .current_dir(dir)
        .spawn()
        .map_err(|e| format!("拉起 workshop.bat 失败：{}", e))?;
    Ok(p.display().to_string())
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
    let root = asset_root().clone();
    log_line(&format!(
        "assets_root={} exists={} clips={}",
        root.display(),
        root.is_dir(),
        root.join("clips.json").is_file()
    ));
    tauri::Builder::default()
        .register_uri_scheme_protocol("deskpet", |_ctx, req| serve_asset(req))
        .invoke_handler(tauri::generate_handler![
            show_window,
            set_tier,
            show_menu,
            log_event,
            quit_app,
            cursor_local,
            set_click_through,
            assets_info,
            open_workshop
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
