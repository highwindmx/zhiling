// DeskPet Tauri 后端：透明窗口 + 托盘 + 设置窗 + 外部状态入口。
// 播放与状态机全在 frontend/src/main.js；前后端只通过少量命令通信，
// 尺寸与定位统一在这里按物理像素计算，避免 WebView 的 CSS 像素 / DPI 换算误差。
//
// 与 deskpet-python 对齐的入口：
//   - 托盘：显示/隐藏、尺寸、主题、素材工坊、设置、退出（双击图标切换显隐）
//   - POST/GET http://127.0.0.1:8765/state（DESKPET_STATE_PORT 可改，0=关闭）
//   - DESKPET_STATE_FILE 状态文件轮询（500ms，内容变化才投递）
//   - v2 主题读写：assets/themes/*.json + active 标记 + active.json 镜像
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod dsh;
mod media;
mod workshop;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant, UNIX_EPOCH};

use serde_json::{json, Value};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, LogicalSize, Manager, PhysicalPosition, WebviewUrl, WebviewWindowBuilder};

static SHOWN: AtomicBool = AtomicBool::new(false);
static START: OnceLock<Instant> = OnceLock::new();
static CUR_TIER: Mutex<String> = Mutex::new(String::new()); // 空 = 用 config
static LAST_STATE: Mutex<String> = Mutex::new(String::new());
static LAST_PLAYING: Mutex<String> = Mutex::new(String::new());

const STATE_PORT_DEFAULT: u16 = 8765;

fn log_path() -> &'static PathBuf {
    static P: OnceLock<PathBuf> = OnceLock::new();
    P.get_or_init(|| {
        if let Ok(p) = std::env::var("DESKPET_LOG") {
            if !p.trim().is_empty() {
                return PathBuf::from(p);
            }
        }
        // release 版没有控制台，默认也要留痕：%LOCALAPPDATA%\DeskPet\deskpet.log
        let base = std::env::var("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|_| std::env::temp_dir());
        base.join("DeskPet").join("deskpet.log")
    })
}

pub(crate) fn log_line(msg: &str) {
    let path = log_path();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(f, "{}", msg);
    }
}

fn elapsed_ms() -> u128 {
    START.get_or_init(Instant::now).elapsed().as_millis()
}

/// 原生错误提示框：release 版没有控制台，启动失败必须让用户看得见。
fn message_box(title: &str, text: &str) {
    #[cfg(windows)]
    unsafe {
        #[link(name = "user32")]
        extern "system" {
            fn MessageBoxW(
                hwnd: *mut std::os::raw::c_void,
                lp_text: *const u16,
                lp_caption: *const u16,
                u_type: u32,
            ) -> i32;
        }
        let wide = |s: &str| -> Vec<u16> {
            s.encode_utf16().chain(std::iter::once(0)).collect()
        };
        MessageBoxW(
            std::ptr::null_mut(),
            wide(text).as_ptr(),
            wide(title).as_ptr(),
            0x10, // MB_ICONERROR
        );
    }
    #[cfg(not(windows))]
    {
        let _ = (title, text);
    }
}

fn fatal(msg: &str) -> ! {
    log_line(&format!("fatal {}", msg));
    message_box("知灵桌宠 · 启动失败", msg);
    std::process::exit(1);
}

/// 启动前置检查：素材缺失时窗口是全透明的，用户会以为「双击没反应」。
fn assets_problem(root: &Path) -> Option<String> {
    if !root.is_dir() {
        return Some(format!(
            "找不到素材（packs）目录：{}\n\n解析顺序：\n  1. 环境变量 DESKPET_PACKS\n  2. 设置里的「素材目录」\n  3. %LOCALAPPDATA%\\DeskPet\\packs\n  4. exe 同级的 assets\\（旧布局兼容）\n\n生成素材：python build\\prepare_assets.py --packs <目录>\n也可以在设置窗里浏览选择一个已生成的素材目录。\n\n素材应包含：\n  themes\\*.json\n  animations\\<id>\\meta.json + fNNNN.webp\n  active.json",
            root.display()
        ));
    }
    if !root.join("themes").is_dir() {
        return Some(format!("素材目录缺少 themes\\：{}", root.display()));
    }
    let theme_count = std::fs::read_dir(root.join("themes"))
        .map(|rd| {
            rd.flatten()
                .filter(|e| e.path().extension().and_then(|x| x.to_str()) == Some("json"))
                .count()
        })
        .unwrap_or(0);
    if theme_count == 0 {
        return Some(format!(
            "素材目录里没有任何主题 JSON：{}\n请重新运行 python build\\prepare_assets.py --assets",
            root.join("themes").display()
        ));
    }
    if !root.join("animations").is_dir() {
        return Some(format!("素材目录缺少 animations\\：{}", root.display()));
    }
    None
}

// ---- 素材目录：运行时外置 --------------------------------------------------
// 帧不进 exe（以后要接不同素材包，不能每换一套就重新编译）。启动时按下面的顺序
// 找素材根目录：
//   1. 环境变量 DESKPET_ASSETS（绝对路径，调试/多套素材并存时用）
//   2. exe 同级的 assets/（发布形态：exe + assets 一起分发）
// 前端通过自定义协议 deskpet:// 读取（Windows 上映射成 http://deskpet.localhost/）。
// 选自定义协议而不是内置 asset 协议的原因：asset 协议的 scope 必须写死在
// tauri.conf.json 里（编译期就固定，运行时改不了），而这里素材目录要能换；自定义
// 协议的路径校验放在下面的 serve_asset 里，只允许素材目录内的白名单后缀，
// 因此 tauri.conf.json 不需要开任何 scope（详见 README「素材外置」）。
static PACK_ROOT: Mutex<Option<PathBuf>> = Mutex::new(None);

/// 默认素材目录：%LOCALAPPDATA%\DeskPet\packs（与 exe 位置无关）。
fn default_packs_dir() -> Option<PathBuf> {
    let base = std::env::var("LOCALAPPDATA")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var("USERPROFILE")
                .ok()
                .filter(|s| !s.trim().is_empty())
                .map(|h| PathBuf::from(h).join("AppData").join("Local"))
        })?;
    Some(base.join("DeskPet").join("packs"))
}

fn exe_assets_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
        .join("assets")
}

/// 素材根解析：DESKPET_PACKS → 设置里的 packsDir → %LOCALAPPDATA%\DeskPet\packs → exe 同级 assets。
fn resolve_pack_root() -> PathBuf {
    let mut cands: Vec<PathBuf> = Vec::new();
    if let Ok(p) = std::env::var("DESKPET_PACKS") {
        if !p.trim().is_empty() {
            cands.push(PathBuf::from(p.trim()));
        }
    }
    if let Some(p) = config_packs_dir() {
        cands.push(p);
    }
    if let Some(p) = default_packs_dir() {
        cands.push(p);
    }
    cands.push(exe_assets_dir());
    cands
        .iter()
        .find(|p| p.is_dir())
        .cloned()
        .unwrap_or_else(|| cands[0].clone())
}

fn asset_root() -> PathBuf {
    if let Ok(g) = PACK_ROOT.lock() {
        if let Some(p) = g.as_ref() {
            return p.clone();
        }
    }
    let p = resolve_pack_root();
    if let Ok(mut g) = PACK_ROOT.lock() {
        *g = Some(p.clone());
    }
    p
}

fn set_pack_root(p: PathBuf) {
    if let Ok(mut g) = PACK_ROOT.lock() {
        *g = Some(p);
    }
}

fn themes_dir() -> PathBuf {
    asset_root().join("themes")
}

pub(crate) fn animations_dir() -> PathBuf {
    asset_root().join("animations")
}

/// 配置独立于素材目录：%LOCALAPPDATA%\DeskPet\config.json（换素材不丢设置）。
/// `DESKPET_CONFIG` 可覆盖（调试/多套配置并存时用），默认与该 Python 版共用同一份。
fn config_path() -> PathBuf {
    if let Ok(p) = std::env::var("DESKPET_CONFIG") {
        if !p.trim().is_empty() {
            return PathBuf::from(p.trim());
        }
    }
    let base = std::env::var("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir());
    base.join("DeskPet").join("config.json")
}

// 配置与 deskpet-python 共用 %LOCALAPPDATA%\DeskPet\config.json。
// 旧 deskpet-python\config.json 的迁移由 Python 侧负责，这里只读共用文件、不做迁移。

/// 设置里选的素材目录（config.json 的 packsDir）；空/缺失返回 None。
fn config_packs_dir() -> Option<PathBuf> {
    let v = read_json(&config_path())?;
    let p = v.get("packsDir")?.as_str()?.trim().to_string();
    if p.is_empty() {
        None
    } else {
        Some(PathBuf::from(p))
    }
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
        // 不允许缓存：用户改完主题/帧后，刷新即生效
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

/// 自定义协议处理器：把 `deskpet://localhost/themes/x.json` 映射成素材目录里的文件。
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

// ---- 主题 / 动画 / 配置 ----------------------------------------------------
#[derive(serde::Serialize)]
struct AssetsInfo {
    root: String,
    exists: bool,
    themes: bool,
    animations: bool,
    source: String,
    candidates: Vec<String>,
}

#[tauri::command]
fn assets_info() -> AssetsInfo {
    let root = asset_root();
    let mut candidates: Vec<String> = Vec::new();
    if let Some(p) = config_packs_dir() {
        candidates.push(p.display().to_string());
    }
    if let Some(p) = default_packs_dir() {
        candidates.push(p.display().to_string());
    }
    candidates.push(exe_assets_dir().display().to_string());
    let source = if std::env::var("DESKPET_PACKS")
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false)
    {
        "env"
    } else if config_packs_dir().is_some() {
        "config"
    } else if default_packs_dir().map(|p| p.is_dir()).unwrap_or(false) {
        "local"
    } else {
        "exe"
    };
    AssetsInfo {
        exists: root.is_dir(),
        themes: root.join("themes").is_dir(),
        animations: root.join("animations").is_dir(),
        root: root.display().to_string(),
        source: source.to_string(),
        candidates,
    }
}

/// 原生「浏览文件夹」对话框（rfd，进程内直调 COM）：不拉 PowerShell 子进程，
/// 既无控制台闪窗，也没有 GBK/UTF-8 转换问题。
fn pick_folder_native(desc: &str) -> Result<PathBuf, String> {
    match rfd::FileDialog::new().set_title(desc).pick_folder() {
        Some(p) => Ok(p),
        None => Err("未选择目录".into()),
    }
}

#[tauri::command]
async fn pick_packs_dir() -> Result<String, String> {
    pick_folder_native("选择素材（packs）目录").map(|p| p.display().to_string())
}

/// 清掉 packsDir 后按 DESKPET_PACKS → Documents\DeskPet\packs → exe 同级 assets 解析。
fn resolve_default_pack_root() -> PathBuf {
    let mut cands: Vec<PathBuf> = Vec::new();
    if let Ok(p) = std::env::var("DESKPET_PACKS") {
        if !p.trim().is_empty() {
            cands.push(PathBuf::from(p.trim()));
        }
    }
    if let Some(p) = default_packs_dir() {
        cands.push(p);
    }
    cands.push(exe_assets_dir());
    cands
        .iter()
        .find(|p| p.is_dir())
        .cloned()
        .unwrap_or_else(|| cands[0].clone())
}

/// 把素材目录写进共享 config.json 的 packsDir（空串 = 恢复默认解析）。
fn store_packs_dir(target: &Path, raw: &str) -> Result<(), String> {
    let mut cfg = load_config();
    if let Value::Object(ref mut o) = cfg {
        let stored = if raw.trim().is_empty() {
            String::new()
        } else {
            target.display().to_string()
        };
        o.insert("packsDir".into(), json!(stored));
    }
    write_json(&config_path(), &cfg)
}

/// 设置里选「素材目录」：校验 → 存进 config.json 的 packsDir → 立即切换。
/// 空字符串表示清掉自定义值，回到默认解析顺序。
#[tauri::command]
async fn set_packs_dir(app: AppHandle, path: String) -> Result<String, String> {
    let raw = path.trim().to_string();
    let target = if raw.is_empty() {
        resolve_default_pack_root()
    } else {
        PathBuf::from(&raw)
    };
    if let Some(problem) = assets_problem(&target) {
        return Err(problem);
    }
    store_packs_dir(&target, &raw)?;
    set_pack_root(target.clone());
    log_line(&format!("packs_dir set {} source=config", target.display()));
    refresh_tray(&app);
    // 桌宠窗口按新目录重新加载（尺寸/位置不变）
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval("window.__deskpetReload && window.__deskpetReload()");
    }
    Ok(target.display().to_string())
}

#[derive(serde::Serialize, Clone)]
struct ThemeInfo {
    id: String,
    name: String,
    active: bool,
    n_states: usize,
    n_bound: usize,
}

fn read_json(path: &Path) -> Option<Value> {
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn mtime_ns(path: &Path) -> u128 {
    std::fs::metadata(path)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}

fn theme_files() -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = std::fs::read_dir(themes_dir())
        .map(|rd| {
            rd.filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out
}

fn theme_id_of(path: &Path, v: &Value) -> String {
    v.get("id")
        .and_then(|x| x.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            path.file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("theme")
                .to_string()
        })
}

fn theme_name_of(path: &Path, v: &Value) -> String {
    v.get("name")
        .and_then(|x| x.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .unwrap_or_else(|| theme_id_of(path, v))
}

fn count_bound(v: &Value) -> usize {
    v.get("states")
        .and_then(|s| s.as_object())
        .map(|states| {
            states
                .values()
                .filter(|st| {
                    st.get("clipIds")
                        .and_then(|c| c.as_array())
                        .map(|a| !a.is_empty())
                        .unwrap_or(false)
                })
                .count()
        })
        .unwrap_or(0)
}

/// 与 pack.py 一致：themes/*.json 的 active 标记；多个为真取最近写入。
fn active_theme_id() -> String {
    let mut best = String::new();
    let mut best_m: u128 = 0;
    for p in theme_files() {
        let Some(v) = read_json(&p) else { continue };
        if v.get("active").and_then(|x| x.as_bool()) == Some(true) {
            let m = mtime_ns(&p);
            if m >= best_m || best.is_empty() {
                best = theme_id_of(&p, &v);
                best_m = m;
            }
        }
    }
    best
}

#[tauri::command]
fn list_themes() -> Vec<ThemeInfo> {
    let active = active_theme_id();
    theme_files()
        .into_iter()
        .filter_map(|p| {
            let v = read_json(&p)?;
            let id = theme_id_of(&p, &v);
            let n_states = v
                .get("states")
                .and_then(|s| s.as_object())
                .map(|o| o.len())
                .unwrap_or(0);
            Some(ThemeInfo {
                active: id == active,
                name: theme_name_of(&p, &v),
                n_states,
                n_bound: count_bound(&v),
                id,
            })
        })
        .collect()
}

#[tauri::command]
fn active_theme() -> String {
    active_theme_id()
}

fn safe_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && !id.contains('/')
        && !id.contains('\\')
        && !id.contains("..")
        && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
}

#[tauri::command]
fn read_theme(id: String) -> Result<Value, String> {
    if !safe_id(&id) {
        return Err(format!("非法的主题 id：{}", id));
    }
    let path = themes_dir().join(format!("{}.json", id));
    Ok(read_json(&path).unwrap_or_else(|| {
        json!({
            "version": 2, "id": id, "name": id,
            "longIdleSec": 600, "awaitingUserSec": 30,
            "states": {},
            "active": true,
        })
    }))
}

fn write_json(path: &Path, value: &Value) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let text = serde_json::to_string_pretty(value).map_err(|e| e.to_string())?;
    // 先写临时文件并落盘，再原子替换，避免读-改-写过程中被 Python 侧读到半截文件。
    let tmp = path.with_extension("json.tmp");
    {
        let mut f = std::fs::File::create(&tmp).map_err(|e| e.to_string())?;
        f.write_all(text.as_bytes()).map_err(|e| e.to_string())?;
        f.sync_all().map_err(|e| e.to_string())?;
    }
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())?;
    Ok(())
}

/// 深度合并：对象逐键递归合并，其余类型直接覆盖。
/// 用于 save_config 的读-改-写，保证 config.json 里 Python 侧写入的未知键（含嵌套）原样保留。
fn merge_json(dst: &mut Value, src: &Value) {
    match (dst, src) {
        (Value::Object(d), Value::Object(s)) => {
            for (k, v) in s {
                match d.get_mut(k) {
                    Some(existing) => merge_json(existing, v),
                    None => {
                        d.insert(k.clone(), v.clone());
                    }
                }
            }
        }
        (d, s) => *d = s.clone(),
    }
}

/// 保存前备份：每个主题只保留**一份** `<id>.json.bak`，每次覆盖，不再按时间戳累积。
fn backup_theme(id: &str, path: &Path) {
    if !path.is_file() {
        return;
    }
    let dir = asset_root().join(".backup");
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let _ = std::fs::copy(path, dir.join(format!("{}.json.bak", id)));
}

fn theme_backup_path(id: &str) -> std::path::PathBuf {
    asset_root().join(".backup").join(format!("{}.json.bak", id))
}

/// 读取「上次保存」的备份，供设置页「恢复上次保存」回填。
#[tauri::command]
fn read_theme_backup(id: String) -> Result<Option<Value>, String> {
    if !safe_id(&id) {
        return Err(format!("非法的主题 id：{}", id));
    }
    Ok(read_json(&theme_backup_path(&id)))
}

/// 设置面板保存：写回 themes/<id>.json，保留未知字段，写前备份。
#[tauri::command]
fn write_theme(app: AppHandle, id: String, mut data: Value) -> Result<String, String> {
    if !safe_id(&id) {
        return Err(format!("非法的主题 id：{}", id));
    }
    let path = themes_dir().join(format!("{}.json", id));
    backup_theme(&id, &path);
    if let Value::Object(ref mut o) = data {
        o.insert("version".into(), json!(2));
        o.insert("id".into(), json!(id));
    }
    write_json(&path, &data)?;
    log_line(&format!("theme_write id={}", id));
    let _ = app.emit("theme-saved", &id);
    Ok(path.display().to_string())
}

/// 切主题：逐个把 active 改成唯一 true，再写 active.json 镜像。
#[tauri::command]
fn set_active_theme(app: AppHandle, id: String) -> Result<(), String> {
    if !safe_id(&id) {
        return Err(format!("非法的主题 id：{}", id));
    }
    if !themes_dir().join(format!("{}.json", id)).is_file() {
        return Err(format!("主题不存在：{}", id));
    }
    for p in theme_files() {
        let Some(mut v) = read_json(&p) else { continue };
        let tid = theme_id_of(&p, &v);
        let want = tid == id;
        let cur = v.get("active").and_then(|x| x.as_bool()).unwrap_or(false);
        if cur == want {
            continue;
        }
        if let Value::Object(ref mut o) = v {
            o.insert("active".into(), json!(want));
        }
        write_json(&p, &v)?;
    }
    // 镜像与 Python 的 pack.py 对齐：写 theme / pack 两个键，并保留已有未知字段。
    let mirror_path = asset_root().join("active.json");
    let mut mirror = read_json(&mirror_path).unwrap_or_else(|| json!({ "version": 2 }));
    if let Value::Object(ref mut o) = mirror {
        o.insert("theme".into(), json!(id));
        o.insert("pack".into(), json!(id));
    }
    write_json(&mirror_path, &mirror)?;
    log_line(&format!("theme_set_active id={}", id));
    refresh_tray(&app);
    let _ = app.emit("theme-changed", &id);
    Ok(())
}

/// 扫描 animations/<assetId>/meta.json，返回前端建 CLIPS 所需的字段。
#[tauri::command]
fn list_assets() -> Vec<Value> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir(animations_dir()) else { return out };
    for entry in rd.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let id = dir
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_string();
        if id.is_empty() || id.starts_with('.') {
            continue;
        }
        let Some(mut meta) = read_json(&dir.join("meta.json")) else { continue };
        if let Value::Object(ref mut o) = meta {
            o.insert("assetId".into(), json!(id));
            o.insert("dir".into(), json!(format!("animations/{}", id)));
        }
        out.push(meta);
    }
    out.sort_by(|a, b| {
        a.get("assetId")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .cmp(b.get("assetId").and_then(|x| x.as_str()).unwrap_or(""))
    });
    out
}

fn default_config() -> Value {
    json!({ "tier": "中", "remindHold": true, "dshPort": 55471 })
}

fn load_config() -> Value {
    let mut cfg = default_config();
    if let Some(v) = read_json(&config_path()) {
        merge_json(&mut cfg, &v);
    }
    cfg
}

#[tauri::command]
fn get_config() -> Value {
    let cfg = load_config();
    if let Some(t) = cfg.get("tier").and_then(|x| x.as_str()) {
        if let Ok(mut cur) = CUR_TIER.lock() {
            if cur.is_empty() {
                *cur = t.to_string();
            }
        }
    }
    cfg
}

/// 前端只提交变化的字段：读旧文件 → merge → 写回 → 通知主窗口。
#[tauri::command]
fn save_config(app: AppHandle, patch: Value) -> Result<Value, String> {
    // 与 Python 版共用同一个 config.json：读旧文件 → 深度合并 patch → 原子写回。
    let mut cfg = load_config();
    merge_json(&mut cfg, &patch);
    write_json(&config_path(), &cfg)?;
    if let Some(t) = cfg.get("tier").and_then(|x| x.as_str()) {
        if let Ok(mut cur) = CUR_TIER.lock() {
            *cur = t.to_string();
        }
        refresh_tray(&app);
    }
    log_line(&format!("config_save {}", cfg));
    // dshPort 改了要立刻按新端口重连；其余配置不影响 DSH 连接。
    if patch.get("dshPort").is_some() {
        dsh::restart();
    }
    // 总开关关掉时，正在跑的番茄钟立即停掉（不影响其它配置）
    let (enabled, _, _) = pomo_settings();
    if !enabled {
        let running = {
            let p = POMO.lock().unwrap_or_else(|e| e.into_inner());
            p.deadline.is_some() || p.remain.is_some() || p.pending.is_some()
        };
        if running {
            pomo_stop(&app);
        }
    }
    refresh_tray(&app);
    let _ = app.emit("config-changed", &cfg);
    Ok(cfg)
}

// ---- 窗口：显示 / 尺寸 / 隐藏 -------------------------------------------------
#[tauri::command]
fn show_window(app: tauri::AppHandle) -> Result<(), String> {
    if SHOWN.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
    let win = app.get_webview_window("main").ok_or("main window missing")?;
    win.show().map_err(|e| e.to_string())?;
    log_line(&format!(
        "first_show_ms={} pid={}",
        elapsed_ms(),
        std::process::id()
    ));
    Ok(())
}

/// 三档尺寸 + 定位。reposition=true 时贴当前屏幕工作区右下角；
/// 否则保持左上角（keepAnchor=true 时保持右下角）不动，只做边界夹取。
#[tauri::command]
fn set_tier(
    app: tauri::AppHandle,
    width: f64,
    height: f64,
    reposition: bool,
    keep_anchor: Option<bool>,
    tier: Option<String>,
) -> Result<(), String> {
    let win = app.get_webview_window("main").ok_or("main window missing")?;
    let old_pos = win.outer_position().ok();
    let old_size = win.outer_size().ok();
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
        } else if let (Some(p), Some(sz), Some(true)) = (old_pos, old_size, keep_anchor) {
            // 换画布比例时保持右下角（pet.py 的 resize 也是锚右下）
            (
                p.x + sz.width as i32 - pw,
                p.y + sz.height as i32 - ph,
            )
        } else {
            old_pos.map(|p| (p.x, p.y)).unwrap_or((0, 0))
        };
        let max_x = wa.position.x + wa.size.width as i32 - pw;
        let max_y = wa.position.y + wa.size.height as i32 - ph;
        x = x.clamp(wa.position.x, max_x.max(wa.position.x));
        y = y.clamp(wa.position.y, max_y.max(wa.position.y));
        win.set_position(PhysicalPosition::new(x, y))
            .map_err(|e| e.to_string())?;
    }
    if let Some(t) = tier {
        let changed = CUR_TIER.lock().map(|mut c| {
            let ch = *c != t;
            *c = t;
            ch
        }).unwrap_or(false);
        if changed {
            refresh_tray(&app);
        }
    }
    Ok(())
}

#[tauri::command]
fn hide_main(app: tauri::AppHandle) -> Result<(), String> {
    let win = app.get_webview_window("main").ok_or("main window missing")?;
    let _ = win.eval("window.__deskpetPause && window.__deskpetPause()");
    win.hide().map_err(|e| e.to_string())
}

fn toggle_main(app: &AppHandle) {
    let Some(win) = app.get_webview_window("main") else { return };
    let visible = win.is_visible().unwrap_or(false);
    if visible {
        let _ = win.eval("window.__deskpetPause && window.__deskpetPause()");
        let _ = win.hide();
        log_line("tray_toggle visible=0");
    } else {
        let _ = win.show();
        let _ = win.set_focus();
        let _ = win.eval("window.__deskpetResume && window.__deskpetResume()");
        log_line("tray_toggle visible=1");
    }
}

#[derive(serde::Deserialize, Clone, Copy)]
struct MenuAt {
    x: i32,
    y: i32,
}

/// 原生右键菜单（HTML 菜单会被无边框窗口裁切，小档尤其明显）。
/// 与 pet.py 一致：只留「尺寸 ▸」和「隐藏」，动作/设置/工坊都收进托盘；
/// 打开菜单算一次用户操作，会清掉提醒态常驻。
#[tauri::command]
fn show_menu(app: tauri::AppHandle, tier: String, at: Option<MenuAt>) -> Result<(), String> {
    log_line(&format!("menu_open tier={}", tier));
    let win = app.get_webview_window("main").ok_or("main window missing")?;
    let menu = Menu::new(&win).map_err(|e| e.to_string())?;
    let size_menu = Submenu::new(&win, "尺寸", true).map_err(|e| e.to_string())?;
    for name in ["小", "中", "大"] {
        let item = CheckMenuItem::with_id(
            &win,
            format!("tier:{}", name),
            name,
            true,
            name == tier,
            None::<&str>,
        )
        .map_err(|e| e.to_string())?;
        size_menu.append(&item).map_err(|e| e.to_string())?;
    }
    menu.append(&size_menu).map_err(|e| e.to_string())?;
    menu.append(&PredefinedMenuItem::separator(&win).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let hide = MenuItem::with_id(&win, "hide", "隐藏", true, None::<&str>)
        .map_err(|e| e.to_string())?;
    menu.append(&hide).map_err(|e| e.to_string())?;

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

#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    log_line(&format!("quit_ms={} pid={}", elapsed_ms(), std::process::id()));
    app.exit(0);
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

#[tauri::command]
fn cursor_local(app: tauri::AppHandle) -> Result<CursorLocal, String> {
    let win = app.get_webview_window("main").ok_or("main window missing")?;
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
    let win = app.get_webview_window("main").ok_or("main window missing")?;
    win.set_ignore_cursor_events(ignore).map_err(|e| e.to_string())
}

// ---- 外部状态入口 ----------------------------------------------------------
/// 前端每次切状态/切段回填，供 GET /state 查询。
#[tauri::command]
fn report_state(state: String, playing: String) {
    if let Ok(mut s) = LAST_STATE.lock() {
        *s = state;
    }
    if let Ok(mut s) = LAST_PLAYING.lock() {
        *s = playing;
    }
}

fn state_payload(raw: &str, source: &str) -> Option<Value> {
    let text = raw.trim().trim_start_matches('\u{feff}').trim();
    if text.is_empty() {
        return None;
    }
    let mut v = match serde_json::from_str::<Value>(text) {
        Ok(Value::Object(mut o)) => {
            if !o.contains_key("state") {
                return None;
            }
            Value::Object(std::mem::take(&mut o))
        }
        Ok(Value::String(s)) => json!({ "state": s }),
        _ => json!({ "state": text }),
    };
    if let Value::Object(ref mut o) = v {
        o.entry("source").or_insert_with(|| json!(source));
        if let Some(st) = o.get("state").and_then(|x| x.as_str()) {
            if st.trim().is_empty() {
                return None;
            }
        }
    }
    Some(v)
}

fn http_reply(status: &str, body: &str) -> Vec<u8> {
    format!(
        "HTTP/1.1 {}\r\nContent-Type: application/json; charset=utf-8\r\n\
Content-Length: {}\r\nConnection: close\r\n\
Access-Control-Allow-Origin: *\r\n\r\n{}",
        status,
        body.as_bytes().len(),
        body
    )
    .into_bytes()
}

fn handle_state_conn(app: AppHandle, mut stream: std::net::TcpStream) {
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let mut buf = [0u8; 16 * 1024];
    let n = stream.read(&mut buf).unwrap_or(0);
    if n == 0 {
        return;
    }
    let text = String::from_utf8_lossy(&buf[..n]).to_string();
    let line = text.lines().next().unwrap_or("");
    let mut parts = line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("").split('?').next().unwrap_or("");
    let body = text.split("\r\n\r\n").nth(1).unwrap_or("").trim();

    if method == "POST" && (path == "/state" || path == "/state/") {
        match state_payload(body, "http") {
            Some(payload) => {
                let state = payload.get("state").and_then(|x| x.as_str()).unwrap_or("");
                if let Ok(mut s) = LAST_STATE.lock() {
                    *s = state.to_string();
                }
                log_line(&format!("state_http recv {}", payload));
                let _ = app.emit("state-in", payload);
                let _ = stream.write_all(&http_reply("200 OK", "{\"ok\":true}"));
            }
            None => {
                let _ = stream.write_all(&http_reply(
                    "400 Bad Request",
                    "{\"ok\":false,\"error\":\"body 必须是 {\\\"state\\\":\\\"done\\\"} 或状态名\"}",
                ));
            }
        }
        return;
    }
    if method == "GET" && (path == "/state" || path == "/state/") {
        let state = LAST_STATE.lock().map(|s| s.clone()).unwrap_or_default();
        let playing = LAST_PLAYING.lock().map(|s| s.clone()).unwrap_or_default();
        let body = json!({ "ok": true, "state": state, "playing": playing }).to_string();
        let _ = stream.write_all(&http_reply("200 OK", &body));
        return;
    }
    let _ = stream.write_all(&http_reply("404 Not Found", "{\"ok\":false}"));
}

/// 状态文件轮询：内容变化才投递（start 时先读一次作为基线，不触发）。
fn spawn_state_file(app: AppHandle, path: PathBuf) {
    std::thread::spawn(move || {
        let mut last_sig: Option<(u64, u128)> = None;
        loop {
            std::thread::sleep(Duration::from_millis(500));
            let sig = std::fs::metadata(&path)
                .ok()
                .map(|m| {
                    let modified = m
                        .modified()
                        .ok()
                        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                        .map(|d| d.as_nanos())
                        .unwrap_or(0);
                    (m.len(), modified)
                });
            if sig == last_sig {
                continue;
            }
            let first = last_sig.is_none();
            last_sig = sig;
            if first {
                continue; // 启动时读到的内容只作为基线
            }
            let Ok(text) = std::fs::read_to_string(&path) else { continue };
            if let Some(payload) = state_payload(&text, "file") {
                log_line(&format!("state_file recv {}", payload));
                let _ = app.emit("state-in", payload);
            }
        }
    });
}

fn spawn_state_server(app: AppHandle) {
    let port: u16 = state_port();
    if port == 0 {
        log_line("state_http disabled port=0");
    } else {
        let handle = app.clone();
        std::thread::spawn(move || {
            match TcpListener::bind(("127.0.0.1", port)) {
                Ok(listener) => {
                    log_line(&format!("state_http listen 127.0.0.1:{}", port));
                    for stream in listener.incoming() {
                        match stream {
                            Ok(s) => {
                                let h = handle.clone();
                                std::thread::spawn(move || handle_state_conn(h, s));
                            }
                            Err(e) => log_line(&format!("state_http accept err={}", e)),
                        }
                    }
                }
                Err(e) => log_line(&format!("state_http bind {} 失败：{}", port, e)),
            }
        });
    }
    if let Ok(file) = std::env::var("DESKPET_STATE_FILE") {
        if !file.trim().is_empty() {
            log_line(&format!("state_file watch {}", file));
            spawn_state_file(app, PathBuf::from(file));
        }
    }
}

// ---- 番茄钟 ----------------------------------------------------------------
// 计时全部用单调时钟差（Instant），不靠定时器累加：隐藏窗口/前端暂停都不影响。
// 阶段结束 → 推提醒态 done（复用提醒常驻），点击桌宠后前端回调 pomodoro_next 才开始下一阶段。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum PomoPhase {
    Idle,
    Work,
    Break,
}

struct Pomo {
    phase: PomoPhase,
    /// 运行中：阶段结束的单调时刻
    deadline: Option<Instant>,
    /// 暂停时的剩余
    remain: Option<Duration>,
    /// 阶段完成、等用户点击后再进入的下一阶段
    pending: Option<PomoPhase>,
}

static POMO: Mutex<Pomo> = Mutex::new(Pomo {
    phase: PomoPhase::Idle,
    deadline: None,
    remain: None,
    pending: None,
});

/// 配置：pomodoro.enabled / pomodoro.workMin / pomodoro.breakMin（分钟，默认 25 / 5）。
fn pomo_settings() -> (bool, u64, u64) {
    let cfg = load_config();
    let p = cfg.get("pomodoro");
    let enabled = p
        .and_then(|x| x.get("enabled"))
        .and_then(|x| x.as_bool())
        .unwrap_or(true);
    let work = p
        .and_then(|x| x.get("workMin"))
        .and_then(|x| x.as_u64())
        .unwrap_or(25)
        .clamp(1, 180);
    let brk = p
        .and_then(|x| x.get("breakMin"))
        .and_then(|x| x.as_u64())
        .unwrap_or(5)
        .clamp(1, 60);
    (enabled, work * 60, brk * 60)
}

fn fmt_mmss(d: Duration) -> String {
    let secs = d.as_secs();
    format!("{:02}:{:02}", secs / 60, secs % 60)
}

#[derive(serde::Serialize)]
struct PomoStatus {
    phase: &'static str,
    running: bool,
    paused: bool,
    /// 秒；运行/暂停时为剩余时间
    remaining: u64,
    /// 阶段完成后待点击进入的下一阶段（空表示没有）
    pending: &'static str,
    text: String,
}

fn pomo_status() -> PomoStatus {
    let (enabled, work_sec, break_sec) = pomo_settings();
    let (phase, running, paused, remaining, pending) = {
        let p = POMO.lock().unwrap_or_else(|e| e.into_inner());
        let remaining = if let Some(dl) = p.deadline {
            dl.saturating_duration_since(Instant::now()).as_secs()
        } else {
            p.remain.map(|d| d.as_secs()).unwrap_or(0)
        };
        (
            p.phase,
            p.deadline.is_some(),
            p.remain.is_some(),
            remaining,
            p.pending,
        )
    };
    let phase_s = match phase {
        PomoPhase::Idle => "idle",
        PomoPhase::Work => "work",
        PomoPhase::Break => "break",
    };
    let pending_s = match pending {
        None => "",
        Some(PomoPhase::Work) => "work",
        Some(PomoPhase::Break) => "break",
        Some(PomoPhase::Idle) => "idle",
    };
    let text = if !enabled {
        "未启用（设置里开启）".to_string()
    } else if let Some(next) = pending {
        if next == PomoPhase::Break {
            format!("专注结束 · 点击开始休息 {}分钟", break_sec / 60)
        } else {
            format!("休息结束 · 点击开始专注 {}分钟", work_sec / 60)
        }
    } else {
        match phase {
            PomoPhase::Idle => "未启动".to_string(),
            PomoPhase::Work | PomoPhase::Break => {
                let name = if phase == PomoPhase::Work { "专注" } else { "休息" };
                format!(
                    "{}{} · 剩余 {}",
                    name,
                    if paused { "已暂停" } else { "中" },
                    fmt_mmss(Duration::from_secs(remaining))
                )
            }
        }
    };
    PomoStatus {
        phase: phase_s,
        running,
        paused,
        remaining,
        pending: pending_s,
        text,
    }
}

// ---- 托盘 / 设置窗 / 工坊 ------------------------------------------------------
/// 番茄钟对外推状态：同时回填 LAST_STATE，前端 external() 当外部信号处理。
fn push_state(app: &AppHandle, payload: Value) {
    if let Some(st) = payload.get("state").and_then(|x| x.as_str()) {
        if let Ok(mut s) = LAST_STATE.lock() {
            *s = st.to_string();
        }
    }
    log_line(&format!("pomo_emit {}", payload));
    let _ = app.emit("state-in", payload);
}

/// 进入某个阶段：deadline 用单调时钟，剩余时间永远是算出来的（不累加）。
fn pomo_begin(app: &AppHandle, phase: PomoPhase) {
    if phase == PomoPhase::Idle {
        return;
    }
    let (enabled, work_sec, break_sec) = pomo_settings();
    if !enabled {
        return;
    }
    let secs = if phase == PomoPhase::Work { work_sec } else { break_sec };
    {
        let mut p = POMO.lock().unwrap_or_else(|e| e.into_inner());
        p.phase = phase;
        p.deadline = Some(Instant::now() + Duration::from_secs(secs));
        p.remain = None;
        p.pending = None;
    }
    let (state, label) = if phase == PomoPhase::Work {
        ("focus", "专注")
    } else {
        ("focus_break", "休息")
    };
    push_state(
        app,
        json!({
            "state": state,
            "source": "pomodoro",
            "message": format!("{}开始（{} 分钟）", label, secs / 60),
        }),
    );
    refresh_tray(app);
}

/// 暂停 / 继续（保持剩余时间，恢复时把 deadline 重新设成 现在+剩余）。
fn pomo_pause(app: &AppHandle) {
    let mut changed = false;
    {
        let mut p = POMO.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(dl) = p.deadline {
            p.remain = Some(dl.saturating_duration_since(Instant::now()));
            p.deadline = None;
            changed = true;
        } else if let Some(rest) = p.remain {
            p.deadline = Some(Instant::now() + rest);
            p.remain = None;
            changed = true;
        }
    }
    if changed {
        refresh_tray(app);
    }
}

fn pomo_stop(app: &AppHandle) {
    {
        let mut p = POMO.lock().unwrap_or_else(|e| e.into_inner());
        p.phase = PomoPhase::Idle;
        p.deadline = None;
        p.remain = None;
        p.pending = None;
    }
    push_state(
        app,
        json!({ "state": "idle", "source": "pomodoro", "message": "番茄钟已停止" }),
    );
    refresh_tray(app);
}

/// 用户点击解除「阶段结束」提醒后，前端回调这里进入下一阶段。
fn pomo_next(app: &AppHandle) {
    let next = {
        let p = POMO.lock().unwrap_or_else(|e| e.into_inner());
        p.pending
    };
    if let Some(phase) = next {
        pomo_begin(app, phase);
    }
}

static POMO_ITEM: Mutex<Option<MenuItem<tauri::Wry>>> = Mutex::new(None);

/// 托盘菜单里「番茄钟」的剩余时间文案 + 托盘 tooltip（托盘 id 见 TrayIconBuilder）。
fn update_pomo_item(app: &AppHandle, status: &PomoStatus) {
    if let Ok(g) = POMO_ITEM.lock() {
        if let Some(item) = g.as_ref() {
            if item.text().ok().as_deref() != Some(status.text.as_str()) {
                let _ = item.set_text(&status.text);
            }
        }
    }
    if let Some(tray) = app.tray_by_id("tray") {
        let _ = tray.set_tooltip(Some(format!("知灵桌宠 · {}", status.text)));
    }
}

/// 500ms 心跳：只做「到点判定 + 文案刷新」，剩余时间始终由 deadline - now 算出来。
fn pomo_tick(app: &AppHandle) {
    let finished = {
        let mut p = POMO.lock().unwrap_or_else(|e| e.into_inner());
        match p.deadline {
            Some(dl) if Instant::now() >= dl => {
                let ph = p.phase;
                p.deadline = None;
                p.remain = None;
                p.pending = Some(if ph == PomoPhase::Work {
                    PomoPhase::Break
                } else {
                    PomoPhase::Work
                });
                Some(ph)
            }
            _ => None,
        }
    };
    if let Some(ph) = finished {
        let msg = if ph == PomoPhase::Work {
            "专注结束 · 点击桌宠开始休息"
        } else {
            "休息结束 · 点击桌宠开始下一轮专注"
        };
        push_state(app, json!({ "state": "done", "source": "pomodoro", "message": msg }));
        refresh_tray(app);
    }
    update_pomo_item(app, &pomo_status());
}

fn spawn_pomo_ticker(app: AppHandle) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(500));
        pomo_tick(&app);
    });
}

#[tauri::command]
async fn pomodoro_start(app: AppHandle) -> Result<String, String> {
    let (enabled, _, _) = pomo_settings();
    if !enabled {
        return Err("番茄钟未启用：请先在设置里打开总开关".into());
    }
    pomo_begin(&app, PomoPhase::Work);
    Ok(pomo_status().text)
}

#[tauri::command]
async fn pomodoro_pause(app: AppHandle) -> Result<String, String> {
    pomo_pause(&app);
    Ok(pomo_status().text)
}

#[tauri::command]
async fn pomodoro_stop(app: AppHandle) -> Result<String, String> {
    pomo_stop(&app);
    Ok(pomo_status().text)
}

/// 前端点击解除提醒后调用：有待进入的阶段就切过去（返回 true）。
#[tauri::command]
async fn pomodoro_next(app: AppHandle) -> Result<bool, String> {
    let had = {
        let p = POMO.lock().unwrap_or_else(|e| e.into_inner());
        p.pending.is_some()
    };
    pomo_next(&app);
    Ok(had)
}

#[tauri::command]
fn pomodoro_status() -> PomoStatus {
    pomo_status()
}

/// 设置页「连接」用：/state 端口、URL、最近一次状态。
#[tauri::command]
fn state_info() -> Value {
    let port: u16 = state_port();
    let last = LAST_STATE
        .lock()
        .map(|s| s.clone())
        .unwrap_or_else(|e| e.into_inner().clone());
    json!({
        "port": port,
        "url": if port == 0 { String::new() } else { format!("http://127.0.0.1:{}/state", port) },
        "lastState": last,
    })
}

/// HTTP /state 端口：DESKPET_STATE_PORT > config 里的 `statePort`（与 Python 共用 config.json）> 8765。
/// 监听在启动时绑定，设置里改端口后需重启桌宠生效。
fn state_port() -> u16 {
    if let Ok(v) = std::env::var("DESKPET_STATE_PORT") {
        if let Ok(p) = v.trim().parse::<u16>() {
            return p;
        }
    }
    load_config()
        .get("statePort")
        .and_then(|v| v.as_u64())
        .map(|n| n.min(65535) as u16)
        .unwrap_or(STATE_PORT_DEFAULT)
}

/// DSH 广播端口：config 里的 `dshPort`（与 Python 版共用同一份 config.json），默认 55471。
pub(crate) fn dsh_port() -> u16 {
    load_config()
        .get("dshPort")
        .and_then(|v| v.as_u64())
        .map(|n| n.min(65535) as u16)
        .unwrap_or(55471)
}

/// 设置页「DSH 链路」状态灯：真实连接态、端口监听、最近事件/错误。
#[tauri::command]
fn dsh_info() -> Value {
    dsh::info()
}

/// 「重新检测」：让后台线程立即重连，返回当前快照（稍后再查一次看到新状态）。
#[tauri::command]
fn dsh_recheck() -> Value {
    dsh::recheck();
    dsh::info()
}

fn read_tier() -> String {
    if let Ok(cur) = CUR_TIER.lock() {
        if !cur.is_empty() {
            return cur.clone();
        }
    }
    load_config()
        .get("tier")
        .and_then(|x| x.as_str())
        .unwrap_or("中")
        .to_string()
}

fn build_tray_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::new(app)?;
    let toggle = MenuItem::with_id(app, "tray_toggle", "显示 / 隐藏", true, None::<&str>)?;
    menu.append(&toggle)?;
    menu.append(&PredefinedMenuItem::separator(app)?)?;

    let tier = read_tier();
    let size_menu = Submenu::new(app, "尺寸", true)?;
    for name in ["小", "中", "大"] {
        let item = CheckMenuItem::with_id(
            app,
            format!("tier:{}", name),
            name,
            true,
            name == tier,
            None::<&str>,
        )?;
        size_menu.append(&item)?;
    }
    menu.append(&size_menu)?;

    let active = active_theme_id();
    let theme_menu = Submenu::new(app, "主题", true)?;
    for t in list_themes() {
        let item = CheckMenuItem::with_id(
            app,
            format!("theme:{}", t.id),
            format!("{}（{} 项）", t.name, t.n_bound),
            true,
            t.id == active,
            None::<&str>,
        )?;
        theme_menu.append(&item)?;
    }
    menu.append(&theme_menu)?;
    menu.append(&PredefinedMenuItem::separator(app)?)?;

    let pomo = Submenu::new(app, "番茄钟", true)?;
    let st = pomo_status();
    let status_item = MenuItem::with_id(app, "pomo:status", st.text.clone(), false, None::<&str>)?;
    if let Ok(mut g) = POMO_ITEM.lock() {
        *g = Some(status_item.clone());
    }
    pomo.append(&status_item)?;
    let idle = st.phase == "idle";
    let start = MenuItem::with_id(app, "pomo:start", "开始专注", idle && !st.paused, None::<&str>)?;
    let pause = MenuItem::with_id(
        app,
        "pomo:pause",
        if st.paused { "继续" } else { "暂停" },
        st.running || st.paused,
        None::<&str>,
    )?;
    let stop = MenuItem::with_id(app, "pomo:stop", "停止", !idle, None::<&str>)?;
    pomo.append(&start)?;
    pomo.append(&pause)?;
    pomo.append(&stop)?;
    menu.append(&pomo)?;
    menu.append(&PredefinedMenuItem::separator(app)?)?;

    let workshop = MenuItem::with_id(app, "workshop", "素材工坊…", true, None::<&str>)?;
    menu.append(&workshop)?;
    let settings = MenuItem::with_id(app, "settings", "设置…", true, None::<&str>)?;
    menu.append(&settings)?;
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    menu.append(&quit)?;
    Ok(menu)
}

fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let menu = build_tray_menu(app)?;
    let mut builder = TrayIconBuilder::with_id("tray")
        .tooltip("知灵桌宠")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::DoubleClick {
                button: MouseButton::Left, ..
            } = event
            {
                toggle_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    log_line("tray_ready");
    Ok(())
}

fn refresh_tray(app: &AppHandle) {
    let Some(tray) = app.tray_by_id("tray") else { return };
    if let Ok(menu) = build_tray_menu(app) {
        let _ = tray.set_menu(Some(menu));
    }
}

fn open_settings_window(app: &AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("settings") {
        w.show().map_err(|e| e.to_string())?;
        let _ = w.set_focus();
        return Ok(());
    }
    let w = WebviewWindowBuilder::new(app, "settings", WebviewUrl::App("settings.html".into()))
        .title("知灵桌宠 · 设置")
        .inner_size(1040.0, 760.0)
        .min_inner_size(880.0, 600.0)
        .resizable(true)
        .center()
        .build()
        .map_err(|e| format!("创建设置窗失败：{}", e))?;
    let _ = w.set_focus();
    log_line("settings_open");
    Ok(())
}

#[tauri::command]
async fn open_settings(app: AppHandle) -> Result<(), String> {
    // 必须 async：同步命令跑在主线程，而 WebviewWindowBuilder::build 会向事件循环投递
    // 建窗消息并阻塞等待，在主线程里再等主线程会死锁（Windows 上表现为窗口停在
    // about:blank、尺寸/位置未应用）。
    open_settings_window(&app)
}

/// 素材工坊已移植进 Tauri（src/workshop.rs）：托盘/设置都开自有窗口，
/// 不再外拉 deskpet-python/workshop.bat。来路参数（状态/主题/段）存在三个 static 里，
/// 由工坊页启动时调 workshop_origin() 取。
static WS_STATE: Mutex<Option<String>> = Mutex::new(None);
static WS_THEME: Mutex<Option<String>> = Mutex::new(None);
static WS_CLIP: Mutex<Option<String>> = Mutex::new(None);

fn set_workshop_origin(state: Option<String>, theme: Option<String>, clip: Option<String>) {
    if let Ok(mut g) = WS_STATE.lock() {
        *g = state.filter(|s| !s.trim().is_empty());
    }
    if let Ok(mut g) = WS_THEME.lock() {
        *g = theme.filter(|s| !s.trim().is_empty());
    }
    if let Ok(mut g) = WS_CLIP.lock() {
        *g = clip.filter(|s| !s.trim().is_empty());
    }
}

#[tauri::command]
fn workshop_origin() -> Value {
    let state = WS_STATE.lock().ok().and_then(|g| g.clone()).unwrap_or_default();
    let theme = WS_THEME.lock().ok().and_then(|g| g.clone()).unwrap_or_default();
    let clip = WS_CLIP.lock().ok().and_then(|g| g.clone()).unwrap_or_default();
    json!({ "state": state, "theme": theme, "clip": clip })
}

fn open_workshop_window(app: &AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("workshop") {
        let _ = w.show();
        let _ = w.set_focus();
        let _ = w.eval("window.__wsOriginChanged && window.__wsOriginChanged()");
        return Ok(());
    }
    // 初始尺寸按当前显示器可用区（扣掉任务栏）计算：88%，上限 1320×880，下限 720×520；
    // 可用区原点可能不在 (0,0)（多屏 / 任务栏在左），按逻辑坐标居中到可用区。
    let (mut ww, mut wh, mut at) = (1200.0_f64, 840.0_f64, None);
    if let Ok(Some(mon)) = app.primary_monitor() {
        let sf = mon.scale_factor().max(1.0);
        let area = mon.work_area();
        let aw = area.size.width as f64 / sf;
        let ah = area.size.height as f64 / sf;
        ww = 1320.0_f64.min(aw * 0.88).max(720.0).min(aw);
        wh = 880.0_f64.min(ah * 0.88).max(520.0).min(ah);
        at = Some((
            area.position.x as f64 / sf + (aw - ww) / 2.0,
            area.position.y as f64 / sf + (ah - wh) / 2.0,
        ));
    }
    let mut b = WebviewWindowBuilder::new(app, "workshop", WebviewUrl::App("workshop.html".into()))
        .title("知灵桌宠 · 素材工坊")
        .inner_size(ww, wh)
        .min_inner_size(720.0_f64.min(ww), 520.0_f64.min(wh))
        .resizable(true)
        .maximizable(true);
    b = match at {
        Some((x, y)) => b.position(x, y),
        None => b.center(),
    };
    let w = b.build().map_err(|e| format!("创建工坊窗口失败：{}", e))?;
    // inner_size 是客户区，外面还有边框/标题栏。按 outer_size 在可用区内重新居中
    // （全部用物理像素），保证窗口真正居中且四边都不越界。
    if let (Ok(Some(mon)), Ok(sz)) = (w.primary_monitor(), w.outer_size()) {
        let area = mon.work_area();
        let x = area.position.x + (area.size.width as i32 - sz.width as i32) / 2;
        let y = area.position.y + (area.size.height as i32 - sz.height as i32) / 2;
        let _ = w.set_position(PhysicalPosition::new(
            x.max(area.position.x),
            y.max(area.position.y),
        ));
        log_line(&format!(
            "workshop_center area={}x{}@{},{} outer={}x{} pos={},{}",
            area.size.width, area.size.height, area.position.x, area.position.y,
            sz.width, sz.height, x, y
        ));
    }
    let _ = w.set_focus();
    log_line("workshop_open");
    Ok(())
}

#[tauri::command]
async fn open_workshop(
    app: AppHandle,
    state: Option<String>,
    theme: Option<String>,
    clip: Option<String>,
) -> Result<(), String> {
    set_workshop_origin(state, theme, clip);
    open_workshop_window(&app)
}

fn dispatch_menu(app: &AppHandle, id: &str) {
    log_line(&format!("menu_pick id={}", id));
    match id {
        "tray_toggle" => toggle_main(app),
        "pomo:start" => pomo_begin(app, PomoPhase::Work),
        "pomo:pause" => pomo_pause(app),
        "pomo:stop" => pomo_stop(app),
        "pomo:status" => {}
        "settings" => {
            if let Err(e) = open_settings_window(app) {
                log_line(&format!("settings_error {}", e));
            }
        }
        "quit" => {
            log_line(&format!("quit_ms={} pid={}", elapsed_ms(), std::process::id()));
            app.exit(0);
        }
        _ => {
            if let Some(win) = app.get_webview_window("main") {
                let safe = id.replace('\\', "").replace('\'', "");
                let _ = win.eval(&format!(
                    "window.__deskpetPick && window.__deskpetPick('{}')",
                    safe
                ));
            }
        }
    }
}

fn main() {
    START.get_or_init(Instant::now);
    std::panic::set_hook(Box::new(|info| {
        let msg = format!("panic: {}", info);
        log_line(&msg);
        message_box("知灵桌宠 · 崩溃", &msg);
    }));
    log_line(&format!(
        "app_start pid={} version={}",
        std::process::id(),
        env!("CARGO_PKG_VERSION")
    ));
    log_line(&format!("log_path={}", log_path().display()));
    log_line(&format!("config_path={}", config_path().display()));
    let root = asset_root();
    let source = if std::env::var("DESKPET_PACKS")
        .map(|s| !s.trim().is_empty())
        .unwrap_or(false)
    {
        "env"
    } else if config_packs_dir().is_some() {
        "config"
    } else if default_packs_dir().map(|p| p.is_dir()).unwrap_or(false) {
        "local"
    } else {
        "exe"
    };
    log_line(&format!(
        "packs_root={} source={} exists={} themes={} animations={}",
        root.display(),
        source,
        root.is_dir(),
        root.join("themes").is_dir(),
        root.join("animations").is_dir()
    ));
    // 素材缺失时窗口画不出任何东西（全透明），看起来就像「双击没反应」，所以直接拦下并提示。
    if let Some(problem) = assets_problem(&root) {
        fatal(&problem);
    }
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
            list_themes,
            active_theme,
            read_theme,
            write_theme,
            read_theme_backup,
            set_active_theme,
            list_assets,
            get_config,
            save_config,
            open_settings,
            open_workshop,
            hide_main,
            report_state,
            set_packs_dir,
            pick_packs_dir,
            pomodoro_start,
            pomodoro_pause,
            pomodoro_stop,
            pomodoro_next,
            pomodoro_status,
            state_info,
            dsh_info,
            dsh_recheck,
            workshop_origin,
            // 素材工坊（src/workshop.rs）
            workshop::workshop_pick_video,
            workshop::workshop_probe,
            workshop::workshop_frame_raw,
            workshop::workshop_preview,
            workshop::workshop_auto_crop,
            workshop::workshop_auto_sample,
            workshop::workshop_sample_rects,
            workshop::workshop_export,
            workshop::workshop_cancel_export,
            workshop::workshop_open_dir,
            workshop::workshop_series,
            workshop::workshop_new_series,
            workshop::workshop_assets
        ])
        .on_menu_event(|app, event| dispatch_menu(app, event.id().0.as_str()))
        .setup(|app| {
            let handle = app.handle().clone();
            if let Err(e) = setup_tray(&handle) {
                log_line(&format!("tray_error {}", e));
            }
            spawn_state_server(handle.clone());
            spawn_pomo_ticker(handle.clone());
            // DSH 广播订阅（只读，断线自动重连；不影响 Python 版桥）
            dsh::start(handle.clone());
            // 兜底：前端脚本万一加载失败，2.5s 后强制显示窗口，避免「进程在、窗口不在」。
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
        .unwrap_or_else(|e| {
            let msg = format!(
                "Tauri 启动失败：{}\n\n常见原因：缺少 WebView2 运行时（安装 Microsoft Edge WebView2 Runtime 后重试）。",
                e
            );
            log_line(&format!("fatal {}", msg));
            message_box("知灵桌宠 · 启动失败", &msg);
            std::process::exit(1);
        });
}
