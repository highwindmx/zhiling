//! 素材工坊（Tauri 自有实现，不再外拉 Python，也不再依赖 ffmpeg）。
//!
//! 管线与 deskpet-python/workshop 对齐：
//!   Media Foundation 抽帧（统一 fps）→ 整片裁边 → key_frame 抠像
//!   → 按首/中/末三帧合并前景框 → 缩放贴合画布（脚底居中）
//!   → libwebp 编码（q80 / method 6 / alpha_quality 80）
//!   → 写 animations/<id>/{fNNNN.webp,meta.json} → 可选绑定到主题的状态。
//!
//! 参数语义、默认值与 Python 版逐项对齐（sat_lo/sat_hi/hue_in/hue_out/floor/
//! despill/hue_ref/blur/erode/keep_frac，见 keyer.key_frame 与 ADV_DEFS）。
//! 差异：预览为省时间只用「当前帧」算贴合框，导出仍用首/中/末三帧（与 build_clip 一致）。
//!
//! 解码走 Windows Media Foundation（mp4/mov 的 H.264/HEVC）；webm/VP9/AV1 不支持，
//! 会明确提示先转 mp4 或用 Python 版。编码走内置 libwebp（与 Pillow 同一套旋钮）。

use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::media;

const WEBP_Q: f32 = 80.0;
const WEBP_METHOD: i32 = 6;
const ALPHA_Q: i32 = 80;
const MARGIN: f64 = 0.985; // 与 build_frames.MARGIN 一致
const PREVIEW_MAX: f64 = 480.0;

/// 「取消导出」开关：前端点取消后置位，导出循环每帧检查一次。
static CANCEL_EXPORT: AtomicBool = AtomicBool::new(false);

#[tauri::command]
pub fn workshop_cancel_export() {
    CANCEL_EXPORT.store(true, Ordering::SeqCst);
}

/// 打开导出目录（默认 animations/；传上次导出的 outDir 则打开那一层）。
#[tauri::command]
pub fn workshop_open_dir(path: Option<String>) -> Result<(), String> {
    let dir = path
        .filter(|p| !p.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(crate::animations_dir);
    if !dir.exists() {
        std::fs::create_dir_all(&dir).map_err(|e| format!("目录不存在，创建失败：{}", e))?;
    }
    Command::new("explorer")
        .arg(&dir)
        .spawn()
        .map_err(|e| format!("打开目录失败：{}", e))?;
    Ok(())
}

/// libwebp 的旋钮与 Pillow save(quality=80, method=6, alpha_quality=80) 完全一致。
fn encode_webp(rgba: &[u8], w: usize, h: usize) -> Result<Vec<u8>, String> {
    let enc = webp::Encoder::from_rgba(rgba, w as u32, h as u32);
    let mut cfg = libwebp_sys::WebPConfig::new()
        .map_err(|_| "WebP 配置初始化失败".to_string())?;
    cfg.lossless = 0;
    cfg.alpha_compression = 1;
    cfg.quality = WEBP_Q;
    cfg.method = WEBP_METHOD;
    cfg.alpha_quality = ALPHA_Q;
    let mem = enc
        .encode_advanced(&cfg)
        .map_err(|e| format!("WebP 编码失败：{:?}", e))?;
    Ok(mem.to_vec())
}

// ===========================================================================
// 原生对话框（rfd / IFileDialog，进程内调用）
// ===========================================================================
/// 用 rfd 的原生打开文件对话框。进程内直调 COM，不拉 PowerShell/cmd 子进程：
/// 既不会闪控制台窗口，也彻底避开 GBK/UTF-8 编码转换（路径直接是 UTF-8 PathBuf）。
fn pick_file_native(title: &str, filter: &str) -> Result<PathBuf, String> {
    let mut d = rfd::FileDialog::new().set_title(title);
    // filter 形如 "视频 (*.mp4;*.mov;*.m4v)|*.mp4;*.mov;*.m4v|所有文件 (*.*)|*.*"
    let parts: Vec<&str> = filter.split('|').collect();
    for pair in parts.chunks(2) {
        if let [name, pats] = pair {
            let exts: Vec<&str> = pats
                .split(';')
                .map(|s| s.trim().trim_start_matches("*."))
                .filter(|s| !s.is_empty() && *s != "*")
                .collect();
            if !exts.is_empty() {
                d = d.add_filter(name.trim(), &exts);
            }
        }
    }
    match d.pick_file() {
        Some(p) => Ok(p),
        None => Err("未选择文件".into()),
    }
}

// ===========================================================================
// 视频探测 / 抽帧（Media Foundation）
// ===========================================================================
#[derive(Serialize, Clone, Default)]
pub struct VideoInfo {
    width: u32,
    height: u32,
    fps: f64,
    duration: f64,
    frames: u32,
}

fn probe_video(src: &str) -> Result<VideoInfo, String> {
    let m = media::probe(src)?;
    Ok(VideoInfo {
        width: m.width,
        height: m.height,
        fps: m.fps,
        duration: m.duration,
        frames: m.frames,
    })
}

/// 单帧解码成 RGBA（裁切后），解码来源为 Media Foundation。
fn decode_frame_rgba(
    src: &str,
    at: f64,
    crop: Option<[i32; 4]>,
    cw: usize,
    ch: usize,
) -> Result<Vec<u8>, String> {
    media::frame_cropped(src, at, crop, cw, ch)
}

// ===========================================================================
// 抠像：与 deskpet-python/workshop/keyer.py 的 key_frame 对齐
// ===========================================================================
#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct KeyParams {
    pub hue_ref: f32,
    pub hue_in: f32,
    pub hue_out: f32,
    pub sat_lo: f32,
    pub sat_hi: f32,
    pub val_lo: f32,
    pub val_hi: f32,
    pub erode: u32,
    pub blur: f32,
    pub despill: f32,
    pub floor: f32,
    pub keep_frac: f32,
    pub watermarks: Vec<[i32; 4]>,
}

impl Default for KeyParams {
    /// 工坊 UI 默认值（workshop.py 的 default_params：ADV_DEFS + FIXED）。
    fn default() -> Self {
        Self {
            hue_ref: 136.0,
            hue_in: 18.0,
            hue_out: 34.0,
            sat_lo: 0.30,
            sat_hi: 0.55,
            val_lo: 0.30,
            val_hi: 0.45,
            erode: 1,
            blur: 1.0,
            despill: 0.75,
            floor: 0.16,
            keep_frac: 0.05,
            watermarks: Vec::new(),
        }
    }
}

impl KeyParams {
    /// 源帧坐标的擦除框 → 裁切后坐标（对应 workshop._wm_for_crop）。
    fn for_crop(&self, crop: Option<[i32; 4]>) -> Self {
        let mut p = self.clone();
        let (l, t) = crop.map(|c| (c[0], c[1])).unwrap_or((0, 0));
        let mut out = Vec::new();
        for w in &self.watermarks {
            let (x0, y0) = ((w[0] - l).max(0), (w[1] - t).max(0));
            let (x1, y1) = ((w[2] - l).max(0), (w[3] - t).max(0));
            if x1 > x0 && y1 > y0 {
                out.push([x0, y0, x1, y1]);
            }
        }
        p.watermarks = out;
        p
    }
}

#[inline]
fn clamp01(x: f32) -> f32 {
    x.clamp(0.0, 1.0)
}

#[derive(Clone, Copy)]
struct Hsv {
    hue: f32,
    sat: f32,
    val: f32,
}

#[inline]
fn hsv_of(r: f32, g: f32, b: f32) -> Hsv {
    let mx = r.max(g).max(b);
    let mn = r.min(g).min(b);
    let d = mx - mn + 1e-6;
    let hue = if mx == r {
        (((g - b) / d) % 6.0) * 60.0
    } else if mx == g {
        ((b - r) / d + 2.0) * 60.0
    } else {
        ((r - g) / d + 4.0) * 60.0
    };
    let sat = if mx > 0.0 { (mx - mn) / mx.max(1e-6) } else { 0.0 };
    Hsv { hue, sat, val: mx }
}

struct Keyed {
    rgba: Vec<u8>,
    bg_permille: u32,
}

/// 抠像：输入是 RGBA（解码输出），参数与 Python keyer.key_frame 一致。
fn key_frame(rgba: &[u8], w: usize, h: usize, p: &KeyParams) -> Keyed {
    let n = w * h;
    let mut alpha = vec![0u8; n];
    let mut color = vec![0u8; n * 3];

    let span_h = (p.hue_out - p.hue_in).max(1e-6);
    let span_s = (p.sat_hi - p.sat_lo).max(1e-6);
    let span_v = (p.val_hi - p.val_lo).max(1e-6);
    let denom_floor = (1.0 - p.floor).max(1e-6);

    for i in 0..n {
        let o = i * 4;
        let r = rgba[o] as f32 / 255.0;
        let g = rgba[o + 1] as f32 / 255.0;
        let b = rgba[o + 2] as f32 / 255.0;
        let Hsv { hue, sat, val } = hsv_of(r, g, b);
        // 与绿幕色相的距离（度）
        let dh = (((hue - p.hue_ref + 180.0).rem_euclid(360.0)) - 180.0).abs();
        let kh = clamp01((p.hue_out - dh) / span_h);
        let ks = clamp01((sat - p.sat_lo) / span_s);
        let kv = clamp01((val - p.val_lo) / span_v);
        let mut bg = kh.min(ks).min(kv);
        // 补刀：暗且灰的像素（压缩噪点 / 暗角）也算背景，只打中间亮度
        let kd = clamp01((0.48 - val) / 0.10) * clamp01((val - 0.22) / 0.06) * clamp01((0.08 - sat) / 0.05);
        if kd > bg {
            bg = kd;
        }
        let a = clamp01((1.0 - bg - p.floor) / denom_floor);
        // 去溢色：按比例削掉「绿比红蓝高」的部分，只改 RGB
        let excess = (g - r.max(b)).max(0.0);
        let g2 = g - p.despill * excess;
        color[i * 3] = (clamp01(r) * 255.0) as u8;
        color[i * 3 + 1] = (clamp01(g2) * 255.0) as u8;
        color[i * 3 + 2] = (clamp01(b) * 255.0) as u8;
        alpha[i] = (a * 255.0) as u8;
    }

    // 水印 / 擦除框直接置 0
    for wm in &p.watermarks {
        let x0 = wm[0].max(0) as usize;
        let y0 = wm[1].max(0) as usize;
        let x1 = (wm[2].max(0) as usize).min(w);
        let y1 = (wm[3].max(0) as usize).min(h);
        for y in y0..y1 {
            for x in x0..x1 {
                alpha[y * w + x] = 0;
            }
        }
    }

    // 连通块清理：只留主体，气泡 / 碎边丢掉（scipy.ndimage.label 默认 4 连通）
    if p.keep_frac < 1.0 {
        let mut mask: Vec<bool> = alpha.iter().map(|&a| a > 20).collect();
        let mut label = vec![0i32; n];
        let mut areas: Vec<u32> = vec![0];
        let mut stack: Vec<usize> = Vec::new();
        let mut next = 0i32;
        for start in 0..n {
            if !mask[start] || label[start] != 0 {
                continue;
            }
            next += 1;
            let mut area = 0u32;
            label[start] = next;
            stack.push(start);
            while let Some(idx) = stack.pop() {
                area += 1;
                let x = idx % w;
                let y = idx / w;
                let mut visit = |j: usize| {
                    if mask[j] && label[j] == 0 {
                        label[j] = next;
                        stack.push(j);
                    }
                };
                if x > 0 {
                    visit(idx - 1);
                }
                if x + 1 < w {
                    visit(idx + 1);
                }
                if y > 0 {
                    visit(idx - w);
                }
                if y + 1 < h {
                    visit(idx + w);
                }
            }
            areas.push(area);
        }
        if next > 1 {
            let big = *areas.iter().max().unwrap_or(&0) as f32;
            let keep_min = big * p.keep_frac;
            for i in 0..n {
                if label[i] != 0 && (areas[label[i] as usize] as f32) < keep_min {
                    alpha[i] = 0;
                    mask[i] = false;
                }
            }
        }
    }

    // MinFilter(3) × erode → 收边
    for _ in 0..p.erode {
        alpha = min_filter3(&alpha, w, h);
    }
    if p.blur > 0.0 {
        alpha = gaussian_blur(&alpha, w, h, p.blur);
    }

    let bg_permille = (alpha.iter().filter(|&&a| a < 8).count() * 1000 / n) as u32;
    let mut rgba = Vec::with_capacity(n * 4);
    for i in 0..n {
        let o = i * 3;
        rgba.push(color[o]);
        rgba.push(color[o + 1]);
        rgba.push(color[o + 2]);
        rgba.push(alpha[i]);
    }
    Keyed { rgba, bg_permille }
}

fn min_filter3(src: &[u8], w: usize, h: usize) -> Vec<u8> {
    let mut out = vec![0u8; src.len()];
    for y in 0..h {
        for x in 0..w {
            let mut m = 255u8;
            for dy in -1i32..=1 {
                for dx in -1i32..=1 {
                    let yy = (y as i32 + dy).clamp(0, h as i32 - 1) as usize;
                    let xx = (x as i32 + dx).clamp(0, w as i32 - 1) as usize;
                    let v = src[yy * w + xx];
                    if v < m {
                        m = v;
                    }
                }
            }
            out[y * w + x] = m;
        }
    }
    out
}

/// Pillow 的 GaussianBlur(radius) 里 radius 就是 sigma，这里按同一口径实现。
fn gaussian_blur(src: &[u8], w: usize, h: usize, sigma: f32) -> Vec<u8> {
    let radius = (sigma * 3.0).ceil() as i32;
    if radius < 1 {
        return src.to_vec();
    }
    let mut kernel = Vec::with_capacity((radius * 2 + 1) as usize);
    let mut sum = 0.0f32;
    for i in -radius..=radius {
        let v = (-(i as f32) * (i as f32) / (2.0 * sigma * sigma)).exp();
        kernel.push(v);
        sum += v;
    }
    for k in kernel.iter_mut() {
        *k /= sum;
    }
    let mut tmp = vec![0u8; src.len()];
    // 横向
    for y in 0..h {
        for x in 0..w {
            let mut acc = 0.0f32;
            for (k, kv) in kernel.iter().enumerate() {
                let xx = (x as i32 + k as i32 - radius).clamp(0, w as i32 - 1) as usize;
                acc += src[y * w + xx] as f32 * kv;
            }
            tmp[y * w + x] = acc.round().clamp(0.0, 255.0) as u8;
        }
    }
    // 纵向
    let mut out = vec![0u8; src.len()];
    for y in 0..h {
        for x in 0..w {
            let mut acc = 0.0f32;
            for (k, kv) in kernel.iter().enumerate() {
                let yy = (y as i32 + k as i32 - radius).clamp(0, h as i32 - 1) as usize;
                acc += tmp[yy * w + x] as f32 * kv;
            }
            out[y * w + x] = acc.round().clamp(0.0, 255.0) as u8;
        }
    }
    out
}

// ===========================================================================
// 缩放 / 画布合成（LANCZOS + 预乘 alpha，与 build_frames.compose_canvas 等价）
// ===========================================================================
fn lanczos3(x: f64) -> f64 {
    let a = 3.0f64;
    if x == 0.0 {
        return 1.0;
    }
    let xa = x.abs();
    if xa >= a {
        return 0.0;
    }
    let px = std::f64::consts::PI * x;
    a * px.sin() * (px / a).sin() / (a * px * px)
}

fn build_weights(sw: usize, dw: usize) -> Vec<Vec<(usize, f32)>> {
    let ratio = dw as f64 / sw as f64;
    let filter_scale = if ratio < 1.0 { 1.0 / ratio } else { 1.0 };
    let support = 3.0 * filter_scale;
    let mut out = Vec::with_capacity(dw);
    for x in 0..dw {
        let center = (x as f64 + 0.5) / ratio - 0.5;
        let lo = (center - support).floor() as i64;
        let hi = (center + support).ceil() as i64;
        let mut ws: Vec<(usize, f32)> = Vec::new();
        let mut sum = 0.0f64;
        for i in lo..=hi {
            let d = (i as f64 - center) / filter_scale;
            let v = lanczos3(d);
            if v.abs() < 1e-8 {
                continue;
            }
            let ii = i.clamp(0, sw as i64 - 1) as usize;
            ws.push((ii, v as f32));
            sum += v;
        }
        if sum.abs() < 1e-9 {
            ws.clear();
            ws.push((x.min(sw - 1), 1.0));
            sum = 1.0;
        }
        let inv = 1.0 / sum as f32;
        for w in ws.iter_mut() {
            w.1 *= inv;
        }
        out.push(ws);
    }
    out
}

/// 预乘 RGBA f32 的分离式 LANCZOS 缩放。
fn resize_premul(src: &[f32], sw: usize, sh: usize, dw: usize, dh: usize) -> Vec<f32> {
    if sw == dw && sh == dh {
        return src.to_vec();
    }
    let wx = build_weights(sw, dw);
    let mut tmp = vec![0f32; dw * sh * 4];
    for y in 0..sh {
        let srow = y * sw * 4;
        for (x, ws) in wx.iter().enumerate() {
            let mut acc = [0f32; 4];
            for &(i, k) in ws {
                let o = srow + i * 4;
                acc[0] += src[o] * k;
                acc[1] += src[o + 1] * k;
                acc[2] += src[o + 2] * k;
                acc[3] += src[o + 3] * k;
            }
            let d = (y * dw + x) * 4;
            tmp[d..d + 4].copy_from_slice(&acc);
        }
    }
    let wy = build_weights(sh, dh);
    let mut out = vec![0f32; dw * dh * 4];
    for (y, ws) in wy.iter().enumerate() {
        let drow = y * dw * 4;
        for x in 0..dw {
            let mut acc = [0f32; 4];
            for &(i, k) in ws {
                let o = i * dw * 4 + x * 4;
                acc[0] += tmp[o] * k;
                acc[1] += tmp[o + 1] * k;
                acc[2] += tmp[o + 2] * k;
                acc[3] += tmp[o + 3] * k;
            }
            let d = drow + x * 4;
            out[d..d + 4].copy_from_slice(&acc);
        }
    }
    out
}

fn premultiply(rgba: &[u8]) -> Vec<f32> {
    let mut out = vec![0f32; rgba.len()];
    for i in (0..rgba.len()).step_by(4) {
        let a = rgba[i + 3] as f32 / 255.0;
        out[i] = rgba[i] as f32 * a;
        out[i + 1] = rgba[i + 1] as f32 * a;
        out[i + 2] = rgba[i + 2] as f32 * a;
        out[i + 3] = rgba[i + 3] as f32;
    }
    out
}

fn unpremultiply_to_u8(src: &[f32]) -> Vec<u8> {
    let mut out = vec![0u8; src.len()];
    for i in (0..src.len()).step_by(4) {
        let a = src[i + 3];
        if a <= 0.5 {
            continue; // 全透明：RGB 归零，避免边缘出现黑边
        }
        let af = a / 255.0;
        out[i] = (src[i] / af).clamp(0.0, 255.0).round() as u8;
        out[i + 1] = (src[i + 1] / af).clamp(0.0, 255.0).round() as u8;
        out[i + 2] = (src[i + 2] / af).clamp(0.0, 255.0).round() as u8;
        out[i + 3] = a.round().clamp(0.0, 255.0) as u8;
    }
    out
}

fn round_half_even(x: f64) -> i64 {
    let f = x.floor();
    let frac = x - f;
    if (frac - 0.5).abs() < 1e-12 {
        if (f as i64) % 2 == 0 {
            f as i64
        } else {
            f as i64 + 1
        }
    } else if frac > 0.5 {
        f as i64 + 1
    } else {
        f as i64
    }
}

#[derive(Serialize, Clone, Copy)]
struct Placement {
    scale: f64,
    ox: i64,
    oy: i64,
}

/// build_frames.fit_bbox：前景框 → (缩放, 偏移)。
fn fit_bbox(l: i64, t: i64, r: i64, b: i64, out_w: usize, out_h: usize) -> Placement {
    let bw = (r - l + 1) as f64;
    let bh = (b - t + 1) as f64;
    let scale = (out_w as f64 * MARGIN / bw).min(out_h as f64 * MARGIN / bh);
    let ox = round_half_even((out_w as f64 - bw * scale) / 2.0 - l as f64 * scale);
    let oy = round_half_even(out_h as f64 - 2.0 - b as f64 * scale);
    Placement { scale, ox, oy }
}

fn alpha_bbox(rgba: &[u8], w: usize, h: usize) -> Option<(i64, i64, i64, i64)> {
    let mut l = i64::MAX;
    let mut t = i64::MAX;
    let mut r = i64::MIN;
    let mut b = i64::MIN;
    for y in 0..h {
        for x in 0..w {
            if rgba[(y * w + x) * 4 + 3] > 8 {
                if (x as i64) < l {
                    l = x as i64;
                }
                if (x as i64) > r {
                    r = x as i64;
                }
                if (y as i64) < t {
                    t = y as i64;
                }
                if (y as i64) > b {
                    b = y as i64;
                }
            }
        }
    }
    if r < l || b < t {
        None
    } else {
        Some((l, t, r, b))
    }
}

/// 抠像结果 → 成品画布（缩放 + 脚底居中 + 留白），返回 RGBA。
fn compose_canvas(
    keyed: &[u8],
    kw: usize,
    kh: usize,
    pl: Placement,
    out_w: usize,
    out_h: usize,
) -> Vec<u8> {
    let dw = ((kw as f64 * pl.scale).round() as usize).max(1);
    let dh = ((kh as f64 * pl.scale).round() as usize).max(1);
    let pre = premultiply(keyed);
    let small = resize_premul(&pre, kw, kh, dw, dh);
    let mut canvas = vec![0f32; out_w * out_h * 4];
    for y in 0..dh {
        let cy = y as i64 + pl.oy;
        if cy < 0 || cy >= out_h as i64 {
            continue;
        }
        for x in 0..dw {
            let cx = x as i64 + pl.ox;
            if cx < 0 || cx >= out_w as i64 {
                continue;
            }
            let s = (y * dw + x) * 4;
            let d = ((cy as usize) * out_w + cx as usize) * 4;
            for c in 0..4 {
                canvas[d + c] += small[s + c];
            }
        }
    }
    unpremultiply_to_u8(&canvas)
}

/// 预览用：等比缩到最长边 ≤ PREVIEW_MAX。
fn fit_preview(rgba: &[u8], w: usize, h: usize) -> (Vec<u8>, usize, usize) {
    let s = (PREVIEW_MAX / w.max(h) as f64).min(1.0);
    if s >= 0.999 {
        return (rgba.to_vec(), w, h);
    }
    let dw = ((w as f64 * s).round() as usize).max(1);
    let dh = ((h as f64 * s).round() as usize).max(1);
    let pre = premultiply(rgba);
    let out = resize_premul(&pre, w, h, dw, dh);
    (unpremultiply_to_u8(&out), dw, dh)
}

// ===========================================================================
// 主题 / 素材库
// ===========================================================================
/// 主题的画布：exportCanvas → idle 段 meta.canvas → 576×530。
fn series_canvas(theme: Option<&str>) -> (usize, usize) {
    let fallback = (576usize, 530usize);
    let Some(tid) = theme.filter(|t| !t.trim().is_empty()) else {
        return fallback;
    };
    if !crate::safe_id(tid) {
        return fallback;
    }
    let path = crate::themes_dir().join(format!("{}.json", tid));
    let Some(th) = crate::read_json(&path) else {
        return fallback;
    };
    if let Some(c) = th.get("exportCanvas") {
        let w = c.get("w").and_then(|x| x.as_u64()).unwrap_or(0) as usize;
        let h = c.get("h").and_then(|x| x.as_u64()).unwrap_or(0) as usize;
        if w > 0 && h > 0 {
            return (w, h);
        }
    }
    let first_idle = th
        .get("states")
        .and_then(|s| s.get("idle"))
        .and_then(|s| s.get("clipIds"))
        .and_then(|c| c.as_array())
        .and_then(|a| a.first())
        .and_then(|x| x.as_str())
        .unwrap_or("");
    if crate::safe_id(first_idle) {
        let mp = crate::animations_dir().join(first_idle).join("meta.json");
        if let Some(m) = crate::read_json(&mp) {
            if let Some(c) = m.get("canvas") {
                let w = c.get("w").and_then(|x| x.as_u64()).unwrap_or(0) as usize;
                let h = c.get("h").and_then(|x| x.as_u64()).unwrap_or(0) as usize;
                if w > 0 && h > 0 {
                    return (w, h);
                }
            }
        }
    }
    fallback
}

fn ascii_id(name: &str) -> String {
    let mut s = String::new();
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            s.push(ch.to_ascii_lowercase());
        } else if !s.ends_with('_') {
            s.push('_');
        }
    }
    let t = s.trim_matches('_').to_string();
    t.chars().take(24).collect()
}

fn next_free_series_id(base: &str) -> String {
    let base = if base.is_empty() { "series" } else { base };
    for n in 1..200 {
        let id = if n == 1 {
            base.to_string()
        } else {
            format!("{}_{}", base, n)
        };
        if !crate::themes_dir().join(format!("{}.json", id)).exists() {
            return id;
        }
    }
    format!("{}_x", base)
}

// ===========================================================================
// Tauri 命令
// ===========================================================================
async fn blocking<T, F>(f: F) -> Result<T, String>
where
    F: FnOnce() -> Result<T, String> + Send + 'static,
    T: Send + 'static,
{
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(|e| format!("后台任务失败：{}", e))?
}

#[tauri::command]
pub async fn workshop_pick_video() -> Result<String, String> {
    blocking(|| {
        pick_file_native(
            "选择绿幕视频",
            "视频 (*.mp4;*.mov;*.m4v)|*.mp4;*.mov;*.m4v|所有文件 (*.*)|*.*",
        )
        .map(|p| p.display().to_string())
    })
    .await
}

#[tauri::command]
pub async fn workshop_probe(path: String) -> Result<VideoInfo, String> {
    blocking(move || probe_video(&path)).await
}

/// 源帧（未抠像，缩到 ≤480）——裁切框的拖拽底图。二进制头：[w, h] 两个 u32。
#[tauri::command]
pub async fn workshop_frame_raw(path: String, at: f64) -> Result<tauri::ipc::Response, String> {
    blocking(move || {
        let info = probe_video(&path)?;
        let (w, h) = (info.width as usize, info.height as usize);
        let rgba = decode_frame_rgba(&path, at, None, w, h)?;
        let (img, dw, dh) = fit_preview(&rgba, w, h);
        let mut buf = Vec::with_capacity(8 + img.len());
        buf.extend_from_slice(&(dw as u32).to_le_bytes());
        buf.extend_from_slice(&(dh as u32).to_le_bytes());
        buf.extend_from_slice(&img);
        Ok(tauri::ipc::Response::new(buf))
    })
    .await
}

#[derive(Deserialize)]
pub struct PreviewReq {
    path: String,
    at: f64,
    #[serde(default)]
    crop: Option<[i32; 4]>,
    #[serde(default)]
    params: KeyParams,
    #[serde(default)]
    canvas: Option<[u32; 2]>,
    #[serde(default)]
    solid: bool,
}

/// 预览：头 6 个 u32（裁边宽、裁边高、画布宽、画布高、透明千分比、耗时毫秒），
/// 随后是两张 RGBA（裁边抠像结果、成品画布）。
#[tauri::command]
pub async fn workshop_preview(req: PreviewReq) -> Result<tauri::ipc::Response, String> {
    blocking(move || {
        let info = probe_video(&req.path)?;
        let sw = info.width as i32;
        let sh = info.height as i32;
        let crop = req.crop.map(|c| clamp_crop(c, sw, sh));
        let (cw, ch) = match crop {
            Some([l, t, r, b]) => ((r - l) as usize, (b - t) as usize),
            None => (sw as usize, sh as usize),
        };
        let rgba = decode_frame_rgba(&req.path, req.at, crop, cw, ch)?;
        let params = req.params.for_crop(crop);
        let keyed = key_frame(&rgba, cw, ch, &params);
        let (ow, oh) = req.canvas.map(|c| (c[0] as usize, c[1] as usize)).unwrap_or((576, 530));
        let bbox = alpha_bbox(&keyed.rgba, cw, ch);
        let canvas = match bbox {
            Some((l, t, r, b)) => {
                let pl = fit_bbox(l, t, r, b, ow, oh);
                compose_canvas(&keyed.rgba, cw, ch, pl, ow, oh)
            }
            None => vec![0u8; ow * oh * 4],
        };
        let mut canvas_show = canvas;
        if req.solid {
            // 实色底看绿边（对应工坊里的「实色底」切换）
            for px in canvas_show.chunks_exact_mut(4) {
                let a = px[3] as f32 / 255.0;
                for c in 0..3 {
                    let bg = [46f32, 52.0, 64.0][c];
                    px[c] = (px[c] as f32 * a + bg * (1.0 - a)).round() as u8;
                }
                px[3] = 255;
            }
        }
        let (img1, w1, h1) = fit_preview(&keyed.rgba, cw, ch);
        let (img2, w2, h2) = fit_preview(&canvas_show, ow, oh);
        let mut buf = Vec::with_capacity(24 + img1.len() + img2.len());
        for v in [
            w1 as u32, h1 as u32, w2 as u32, h2 as u32, keyed.bg_permille, 0,
        ] {
            buf.extend_from_slice(&v.to_le_bytes());
        }
        buf.extend_from_slice(&img1);
        buf.extend_from_slice(&img2);
        Ok(tauri::ipc::Response::new(buf))
    })
    .await
}

fn clamp_crop(c: [i32; 4], w: i32, h: i32) -> [i32; 4] {
    let l = c[0].clamp(0, w - 16);
    let t = c[1].clamp(0, h - 16);
    let r = c[2].clamp(l + 16, w);
    let b = c[3].clamp(t + 16, h);
    [l, t, r, b]
}

/// detect_crop：扫 3 帧「近黑且均匀」的边带并集（与 workshop.detect_crop 同口径）。
#[tauri::command]
pub async fn workshop_auto_crop(path: String, at: f64) -> Result<Value, String> {
    let _ = at; // 自动裁边固定扫首/中/末三帧，不需要当前帧位置
    blocking(move || {
        let info = probe_video(&path)?;
        let (w, h) = (info.width as usize, info.height as usize);
        let times = [0.0f64, info.duration / 2.0, (info.duration - 0.15).max(0.0)];
        let mut best = [0i64; 4];
        for (i, t) in times.iter().enumerate() {
            let rgba = decode_frame_rgba(&path, *t, None, w, h)?;
            let bands = detect_bands(&rgba, w, h);
            if i == 0 {
                best = bands;
            } else {
                for k in 0..4 {
                    best[k] = best[k].max(bands[k]);
                }
            }
        }
        let margin = 2i64;
        let (l, t, r, b) = (best[0] + margin, best[1] + margin, best[2] + margin, best[3] + margin);
        if l + t + r + b < 8 || l >= w as i64 - 40 || r >= w as i64 - 40 {
            return Ok(json!({ "crop": null, "detail": format!("并集 l={} t={} r={} b={}", l, t, r, b) }));
        }
        let crop = [l as i32, t as i32, (w as i64 - r) as i32, (h as i64 - b) as i32];
        Ok(json!({ "crop": crop, "detail": format!("并集 l={} t={} r={} b={}", l, t, r, b) }))
    })
    .await
}

fn band_width(rgba: &[u8], w: usize, h: usize, axis: usize, reverse: bool) -> i64 {
    let n = if axis == 0 { h } else { w };
    let mut count = 0i64;
    for k in 0..n {
        let idx = if reverse { n - 1 - k } else { k };
        let mut sum = 0f64;
        let mut mx = 0u8;
        let mut mn = 255u8;
        let len = if axis == 0 { w } else { h };
        for j in 0..len {
            let (x, y) = if axis == 0 { (j, idx) } else { (idx, j) };
            let o = (y * w + x) * 4;
            let v = ((rgba[o] as u32 + rgba[o + 1] as u32 + rgba[o + 2] as u32) / 3) as u8;
            sum += v as f64;
            if v > mx {
                mx = v;
            }
            if v < mn {
                mn = v;
            }
        }
        let m = sum / len as f64;
        let spread = (mx - mn) as f64;
        if m < 70.0 && spread < 40.0 {
            count += 1;
        } else {
            break;
        }
        if count > n as i64 / 2 {
            break;
        }
    }
    count
}

fn detect_bands(rgba: &[u8], w: usize, h: usize) -> [i64; 4] {
    [
        band_width(rgba, w, h, 1, false),
        band_width(rgba, w, h, 0, false),
        band_width(rgba, w, h, 1, true),
        band_width(rgba, w, h, 0, true),
    ]
}

/// 自动取色：四角取样（sample_hue_from）→ hue_ref + 压饱和度闸门。
#[tauri::command]
pub async fn workshop_auto_sample(
    path: String,
    at: f64,
    crop: Option<[i32; 4]>,
) -> Result<Value, String> {
    blocking(move || {
        let info = probe_video(&path)?;
        let sw = info.width as i32;
        let sh = info.height as i32;
        let c = crop.map(|x| clamp_crop(x, sw, sh));
        let (cw, ch) = match c {
            Some([l, t, r, b]) => ((r - l) as usize, (b - t) as usize),
            None => (sw as usize, sh as usize),
        };
        let rgba = decode_frame_rgba(&path, at, c, cw, ch)?;
        let mut hs: Vec<f32> = Vec::new();
        let mut ss: Vec<f32> = Vec::new();
        let k = (ch.min(cw) / 12).max(8);
        for (ox, oy) in [
            (0usize, 0usize),
            (cw.saturating_sub(k), 0),
            (0, ch.saturating_sub(k)),
            (cw.saturating_sub(k), ch.saturating_sub(k)),
        ] {
            for y in oy..(oy + k).min(ch) {
                for x in ox..(ox + k).min(cw) {
                    let o = (y * cw + x) * 4;
                    let r = rgba[o] as f32 / 255.0;
                    let g = rgba[o + 1] as f32 / 255.0;
                    let b = rgba[o + 2] as f32 / 255.0;
                    let Hsv { hue, sat, val } = hsv_of(r, g, b);
                    if val > 0.10 && val < 0.98 {
                        hs.push(hue);
                        ss.push(sat);
                    }
                }
            }
        }
        if hs.is_empty() {
            return Err("四角没有可用背景像素：拖帧滑块换一帧再试".into());
        }
        // 只取饱和度 ≥ p60 的像素求中位数（与 sample_hue_from 一致）
        let mut order: Vec<usize> = (0..ss.len()).collect();
        order.sort_by(|a, b| ss[*a].partial_cmp(&ss[*b]).unwrap_or(std::cmp::Ordering::Equal));
        let cut = ((order.len() as f32) * 0.60) as usize;
        let pick = &order[cut.min(order.len() - 1)..];
        let med = |vals: &[f32]| -> f32 {
            let mut v: Vec<f32> = vals.to_vec();
            v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            if v.is_empty() {
                return 0.0;
            }
            v[v.len() / 2]
        };
        let hue_med = med(&pick.iter().map(|&i| hs[i]).collect::<Vec<f32>>());
        let sat_med = med(&pick.iter().map(|&i| ss[i]).collect::<Vec<f32>>());
        // 闸门：背景饱和度已经低于下限才压低（对齐 workshop.sample_hue）
        let mut sat_lo = 0.30f32;
        let mut sat_hi = 0.55f32;
        if sat_med < sat_lo {
            sat_lo = ((sat_med - 0.10).max(0.02) * 100.0).round() / 100.0;
            sat_hi = (((sat_med - 0.05).max(sat_lo + 0.02)) * 100.0).round() / 100.0;
        }
        Ok(json!({
            "hue_ref": (hue_med * 10.0).round() / 10.0,
            "sat_lo": sat_lo,
            "sat_hi": sat_hi,
            "sat_med": (sat_med * 100.0).round() / 100.0,
            "samples": hs.len(),
        }))
    })
    .await
}

fn pct(vals: &[f32], q: f64) -> f32 {
    if vals.is_empty() {
        return 0.0;
    }
    let mut v: Vec<f32> = vals.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    if v.len() == 1 {
        return v[0];
    }
    let pos = q * (v.len() as f64 - 1.0);
    let lo = pos.floor() as usize;
    let hi = pos.ceil() as usize;
    let frac = (pos - lo as f64) as f32;
    v[lo] * (1.0 - frac) + v[hi] * frac
}

fn round1(x: f32) -> f32 {
    (x * 10.0).round() / 10.0
}

fn round2(x: f32) -> f32 {
    (x * 100.0).round() / 100.0
}

/// 多个用户取色框 → 一套数据驱动的闸门参数（对应 workshop.derive_range）。
///
/// 取色框坐标是「源帧像素」，取样作用于未裁切的完整帧，所以之后改裁切不会
/// 让已取的点失效（与 Python 版一致）。
#[tauri::command]
pub async fn workshop_sample_rects(
    path: String,
    at: f64,
    rects: Vec<[i32; 4]>,
) -> Result<Value, String> {
    blocking(move || {
        let info = probe_video(&path)?;
        let (w, h) = (info.width as usize, info.height as usize);
        let rgba = decode_frame_rgba(&path, at, None, w, h)?;
        let mut hues: Vec<f32> = Vec::new();
        let mut sats: Vec<f32> = Vec::new();
        let mut used = 0usize;
        for rect in &rects {
            let l = (rect[0].max(0) as usize).min(w.saturating_sub(1));
            let t = (rect[1].max(0) as usize).min(h.saturating_sub(1));
            let r = (rect[2].max(0) as usize).min(w).max(l + 1);
            let b = (rect[3].max(0) as usize).min(h).max(t + 1);
            let (rw, rh) = (r - l, b - t);
            let step = (((rw * rh) as f64 / 12000.0).sqrt().ceil() as usize).max(1);
            let mut any = false;
            let mut y = t;
            while y < b {
                let mut x = l;
                while x < r {
                    let o = (y * w + x) * 4;
                    let Hsv { hue, sat, val } = hsv_of(
                        rgba[o] as f32 / 255.0,
                        rgba[o + 1] as f32 / 255.0,
                        rgba[o + 2] as f32 / 255.0,
                    );
                    if val > 0.10 && val < 0.98 && sat > 0.03 {
                        hues.push(hue);
                        sats.push(sat);
                        any = true;
                    }
                    x += step;
                }
                y += step;
            }
            if any {
                used += 1;
            }
        }
        if hues.is_empty() {
            return Err("取色框里没有可用背景像素（近黑/近白/灰）：换个位置再拖框".into());
        }
        // 圆形色相中位数（0/360 跨界时普通中位数会算错，迭代收敛）
        let mut cur = pct(&hues, 0.5);
        for _ in 0..3 {
            let shifted: Vec<f32> = hues
                .iter()
                .map(|h| ((*h - cur + 180.0).rem_euclid(360.0)) - 180.0 + cur)
                .collect();
            cur = pct(&shifted, 0.5);
        }
        let href = cur.rem_euclid(360.0);
        let dev: Vec<f32> = hues
            .iter()
            .map(|h| (((*h - href + 180.0).rem_euclid(360.0)) - 180.0).abs())
            .collect();
        // 四角框里常混进暗角/角色像素：分位只用「确实接近背景色」的核心像素
        let core: Vec<f32> = dev.iter().copied().filter(|d| *d <= 45.0).collect();
        let core = if core.len() < ((hues.len() as f32) * 0.05).max(10.0) as usize {
            dev.clone()
        } else {
            core
        };
        let d60 = pct(&core, 0.60);
        let d90 = pct(&core, 0.90);
        let s05 = pct(&sats, 0.05);
        let s10 = pct(&sats, 0.10);
        let s50 = pct(&sats, 0.50);
        let hue_in = round1(d60 * 1.2 + 4.0).clamp(6.0, 32.0);
        let hue_out = round1((d90 * 1.4).max(hue_in + 10.0)).clamp(hue_in + 6.0, 60.0);
        let sat_lo = round2((s10 - 0.10).max(0.0)).max(0.02);
        let sat_hi = round2((s05.min(s10) - 0.01).max(0.04)).max(sat_lo + 0.02);
        Ok(json!({
            "hue_ref": round1(href),
            "hue_in": hue_in,
            "hue_out": hue_out,
            "sat_lo": sat_lo,
            "sat_hi": sat_hi,
            "boxes": used,
            "samples": hues.len(),
            "hue_dev60": round1(d60),
            "hue_dev90": round1(d90),
            "sat05": round2(s05),
            "sat10": round2(s10),
            "sat50": round2(s50),
        }))
    })
    .await
}

#[derive(Deserialize)]
pub struct ExportReq {
    path: String,
    #[serde(default)]
    crop: Option<[i32; 4]>,
    #[serde(default)]
    params: KeyParams,
    #[serde(default)]
    fps: Option<u32>,
    #[serde(default)]
    canvas: Option<[u32; 2]>,
    clip_id: String,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    theme: Option<String>,
    #[serde(default)]
    state: Option<String>,
    #[serde(default)]
    overwrite: bool,
}

fn temp_raw(pid: u32) -> PathBuf {
    std::env::temp_dir()
        .join(format!("deskpet-ws-{}", pid))
        .join("raw.rgba")
}

/// 抽帧 → 抠像 → 合成 → 编码。返回 meta 摘要。
#[tauri::command]
pub async fn workshop_export(req: ExportReq) -> Result<Value, String> {
    blocking(move || export_impl(req)).await
}

fn export_impl(req: ExportReq) -> Result<Value, String> {
    CANCEL_EXPORT.store(false, Ordering::SeqCst);
    if !crate::safe_id(&req.clip_id) {
        return Err(format!("动画 id 只能用字母/数字/下划线：{}", req.clip_id));
    }
    let info = probe_video(&req.path)?;
    let sw = info.width as i32;
    let sh = info.height as i32;
    let crop = req.crop.map(|c| clamp_crop(c, sw, sh));
    let (cw, ch) = match crop {
        Some([l, t, r, b]) => ((r - l) as usize, (b - t) as usize),
        None => (sw as usize, sh as usize),
    };
    let fps = req.fps.unwrap_or(24).clamp(1, 60);
    let (ow, oh) = req
        .canvas
        .map(|c| (c[0] as usize, c[1] as usize))
        .unwrap_or_else(|| series_canvas(req.theme.as_deref()));
    if ow == 0 || oh == 0 {
        return Err("画布尺寸不合法".into());
    }

    // 1) Media Foundation 抽帧到一个临时 raw 文件（rgba 顺序帧，逐帧读，省内存）
    let tmp = temp_raw(std::process::id());
    if let Some(dir) = tmp.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let _ = std::fs::remove_file(&tmp);
    let frame_bytes = cw * ch * 4;
    let count = media::export_raw(&req.path, fps, crop, cw, ch, &tmp).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        e
    })?;
    if count == 0 {
        let _ = std::fs::remove_file(&tmp);
        return Err("抽帧为空：检查源视频是否可读".into());
    }

    // 2) 首/中/末三帧各抠一次 → 合并前景框（build_clip 的口径）
    let params_c = req.params.for_crop(crop);
    let mut file = std::fs::File::open(&tmp).map_err(|e| e.to_string())?;
    let mut boxes: Vec<(i64, i64, i64, i64)> = Vec::new();
    for idx in [0usize, count / 2, count - 1] {
        let mut buf = vec![0u8; frame_bytes];
        file.seek(SeekFrom::Start((idx * frame_bytes) as u64))
            .map_err(|e| e.to_string())?;
        file.read_exact(&mut buf).map_err(|e| e.to_string())?;
        let keyed = key_frame(&buf, cw, ch, &params_c);
        if let Some(bb) = alpha_bbox(&keyed.rgba, cw, ch) {
            boxes.push(bb);
        }
    }
    if boxes.is_empty() {
        let _ = std::fs::remove_file(&tmp);
        return Err("抠像后没有前景：先点「自动取色」把背景色相取准".into());
    }
    let (l, t) = (
        boxes.iter().map(|b| b.0).min().unwrap_or(0),
        boxes.iter().map(|b| b.1).min().unwrap_or(0),
    );
    let (r, b) = (
        boxes.iter().map(|b| b.2).max().unwrap_or(0),
        boxes.iter().map(|b| b.3).max().unwrap_or(0),
    );
    let pl = fit_bbox(l, t, r, b, ow, oh);
    let foot = [
        round_half_even(((l + r) as f64 / 2.0) * pl.scale + pl.ox as f64) as i64,
        oh as i64 - 2,
    ];

    // 3) 输出目录 + 清旧帧
    let out_dir = crate::animations_dir().join(&req.clip_id);
    std::fs::create_dir_all(&out_dir).map_err(|e| e.to_string())?;
    if !req.overwrite && out_dir.join("meta.json").is_file() {
        let _ = std::fs::remove_file(&tmp);
        return Err(format!(
            "animations/{} 已存在；勾选「覆盖已有段」后会重写（旧 meta 备份到 packs/.backup）",
            req.clip_id
        ));
    }
    for entry in std::fs::read_dir(&out_dir).map(|d| d.flatten().collect::<Vec<_>>()).unwrap_or_default() {
        let p = entry.path();
        let ext = p.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
        if ext == "webp" || ext == "png" {
            let _ = std::fs::remove_file(&p);
        }
    }
    // 旧 meta 备份（回滚用）
    let meta_path = out_dir.join("meta.json");
    if meta_path.is_file() {
        let bak = crate::asset_root().join(".backup");
        let _ = std::fs::create_dir_all(&bak);
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let _ = std::fs::copy(
            &meta_path,
            bak.join(format!("asset.{}.{}.json", req.clip_id, stamp)),
        );
    }

    // 4) 逐帧抠像 + 合成 → 内置 libwebp 编码（q80 / method 6 / alpha_quality 80）
    file.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    let mut reader = BufReader::new(file);
    let mut wrote = 0usize;
    let mut written: Vec<PathBuf> = Vec::new();
    for i in 0..count {
        if CANCEL_EXPORT.load(Ordering::SeqCst) {
            let _ = std::fs::remove_file(&tmp);
            for p in &written {
                let _ = std::fs::remove_file(p);
            }
            crate::log_line(&format!("workshop_export cancelled clip={} at={}/{}", req.clip_id, i, count));
            return Err("已取消导出（临时帧已清理，未写入 meta.json）".into());
        }
        let mut buf = vec![0u8; frame_bytes];
        if reader.read_exact(&mut buf).is_err() {
            break;
        }
        let keyed = key_frame(&buf, cw, ch, &params_c);
        let canvas = compose_canvas(&keyed.rgba, cw, ch, pl, ow, oh);
        let webp = encode_webp(&canvas, ow, oh).map_err(|e| {
            let _ = std::fs::remove_file(&tmp);
            format!("第 {} 帧编码失败：{}", i, e)
        })?;
        let frame_path = out_dir.join(format!("f{:04}.webp", i + 1));
        std::fs::write(&frame_path, &webp).map_err(|e| {
            let _ = std::fs::remove_file(&tmp);
            format!("写 {} 失败：{}", frame_path.display(), e)
        })?;
        written.push(frame_path);
        wrote += 1;
    }
    let _ = std::fs::remove_file(&tmp);

    // 5) 统计 + 写 meta.json
    let mut bytes = 0u64;
    let mut n = 0usize;
    for entry in std::fs::read_dir(&out_dir).map(|d| d.flatten().collect::<Vec<_>>()).unwrap_or_default() {
        let p = entry.path();
        if p.extension().and_then(|e| e.to_str()) == Some("webp")
            && p.file_name().and_then(|s| s.to_str()).map(|s| s.starts_with('f')).unwrap_or(false)
        {
            n += 1;
            bytes += entry.metadata().map(|m| m.len()).unwrap_or(0);
        }
    }
    if n == 0 {
        return Err("编码完成但没有生成任何帧文件".into());
    }
    if n != wrote || n != count {
        crate::log_line(&format!("workshop_export frames mismatch wrote={} files={} decoded={}", wrote, n, count));
    }
    let name = req
        .name
        .clone()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| req.clip_id.clone());
    let src_name = Path::new(&req.path)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_string();
    let mut meta = json!({
        "assetId": req.clip_id,
        "name": name,
        "canvas": { "w": ow, "h": oh },
        "fps": fps,
        "count": n,
        "format": "webp",
        "loop": false,
        "type": "action",
        "priority": 20,
        "scale": (pl.scale * 10000.0).round() / 10000.0,
        "offset": [pl.ox, pl.oy],
        "anchor": { "foot": [0.5, (((foot[1] as f64 / oh as f64) * 10000.0).round() / 10000.0)] },
        "tags": [],
        "crop": crop.map(|c| c.to_vec()),
        "source": src_name,
        "sourcePath": req.path,
        "eraseRects": req.params.watermarks.iter().map(|w| w.to_vec()).collect::<Vec<_>>(),
        "origin": { "theme": req.theme.clone().unwrap_or_default(), "generated": true },
    });
    let keyer = json!({
        "hue_ref": req.params.hue_ref, "hue_in": req.params.hue_in, "hue_out": req.params.hue_out,
        "sat_lo": req.params.sat_lo, "sat_hi": req.params.sat_hi,
        "val_lo": req.params.val_lo, "val_hi": req.params.val_hi,
        "erode": req.params.erode, "blur": req.params.blur, "despill": req.params.despill,
        "floor": req.params.floor, "keep_frac": req.params.keep_frac,
        "watermarks": req.params.watermarks.iter().map(|w| w.to_vec()).collect::<Vec<_>>(),
    });
    if let Value::Object(ref mut o) = meta {
        o.insert("keyer".into(), keyer);
    }
    // 已有段的其它字段（license 等）保留
    if let Some(Value::Object(old)) = crate::read_json(&meta_path) {
        for k in ["license", "legacyDir", "displayName", "tags"] {
            if let (Some(v), false) = (old.get(k), meta.get(k).map(|x| !x.is_null()).unwrap_or(false)) {
                if let Value::Object(ref mut o) = meta {
                    o.insert(k.to_string(), v.clone());
                }
            }
        }
    }
    crate::write_json(&meta_path, &meta)?;

    // 6) 绑定到主题状态（可选）
    let mut bound = false;
    if let (Some(tid), Some(state)) = (
        req.theme.clone().filter(|s| crate::safe_id(s)),
        req.state.clone().filter(|s| !s.trim().is_empty()),
    ) {
        let path = crate::themes_dir().join(format!("{}.json", tid));
        if let Some(mut th) = crate::read_json(&path) {
            let mut states = th
                .get("states")
                .and_then(|s| s.as_object())
                .cloned()
                .unwrap_or_default();
            let mut entry = states.get(&state).cloned().unwrap_or_else(|| json!({}));
            let mut ids: Vec<Value> = entry
                .get("clipIds")
                .and_then(|c| c.as_array())
                .cloned()
                .unwrap_or_default();
            if !ids.iter().any(|x| x.as_str() == Some(req.clip_id.as_str())) {
                ids.push(json!(req.clip_id));
            }
            if let Value::Object(ref mut e) = entry {
                e.insert("clipIds".into(), json!(ids));
                e.insert("enabled".into(), json!(true));
            }
            states.insert(state.clone(), entry);
            if let Value::Object(ref mut o) = th {
                o.insert("states".into(), Value::Object(states));
            }
            crate::write_json(&path, &th)?;
            bound = true;
            crate::log_line(&format!("workshop_bind theme={} state={} clip={}", tid, state, req.clip_id));
        }
    }
    crate::log_line(&format!(
        "workshop_export clip={} n={} {}x{} scale={:.3} offset=({},{})",
        req.clip_id, n, ow, oh, pl.scale, pl.ox, pl.oy
    ));
    Ok(json!({
        "clipId": req.clip_id,
        "outDir": out_dir.display().to_string(),
        "count": n,
        "bytes": bytes,
        "width": ow,
        "height": oh,
        "scale": (pl.scale * 10000.0).round() / 10000.0,
        "offset": [pl.ox, pl.oy],
        "foot": foot,
        "bound": bound,
        "theme": req.theme.clone().unwrap_or_default(),
        "webp": {
            "q": WEBP_Q,
            "method": WEBP_METHOD,
            "alpha_quality": ALPHA_Q,
        },
    }))
}

#[tauri::command]
pub fn workshop_series() -> Vec<Value> {
    let active = crate::active_theme_id();
    crate::theme_files()
        .into_iter()
        .filter_map(|p| {
            let v = crate::read_json(&p)?;
            let id = crate::theme_id_of(&p, &v);
            let canvas = series_canvas(Some(&id));
            Some(json!({
                "id": id,
                "name": crate::theme_name_of(&p, &v),
                "active": id == active,
                "bound": crate::count_bound(&v),
                "canvas": [canvas.0, canvas.1],
            }))
        })
        .collect()
}

/// 新建主题：只建一张绑定索引，画布默认 576×530。
#[tauri::command]
pub async fn workshop_new_series(app: tauri::AppHandle, name: String) -> Result<String, String> {
    let display = name.trim().to_string();
    if display.is_empty() {
        return Err("主题名不能为空".into());
    }
    let base = ascii_id(&display);
    let id = next_free_series_id(&base);
    let path = crate::themes_dir().join(format!("{}.json", id));
    let data = json!({
        "version": 2,
        "id": id,
        "name": display,
        "longIdleSec": 600,
        "awaitingUserSec": 30,
        "states": {},
        "active": false,
        "exportCanvas": { "w": 576, "h": 530 },
    });
    crate::write_json(&path, &data)?;
    crate::log_line(&format!("workshop_new_series id={}", id));
    crate::refresh_tray(&app);
    Ok(id)
}

/// 动作库列表（给工坊「插入已有段」用）。
#[tauri::command]
pub fn workshop_assets() -> Vec<Value> {
    crate::list_assets()
}
