//! Windows Media Foundation 解码：替代 ffmpeg CLI 抽帧。
//!
//! Tauri 版不再依赖 ffmpeg：
//!   - 解码：IMFSourceReader + MFVideoFormat_RGB32（H.264 / HEVC 的 mp4/mov 交给系统解码器）
//!   - 编码：libwebp（静态编进 exe，q80 / method 6 / alpha_quality 80，与 Python 版同一套旋钮）
//!
//! 不支持 webm / VP9 / AV1 等系统没有解码器的容器，open 失败时给出明确中文提示。

use std::path::Path;

use windows::core::{GUID, HSTRING};
use windows::Win32::Media::MediaFoundation::{
    IMFSourceReader, MFCreateAttributes, MFCreateMediaType, MFCreateSourceReaderFromURL,
    MFMediaType_Video, MFShutdown, MFStartup, MFVideoFormat_RGB32, MF_MT_DEFAULT_STRIDE,
    MF_MT_FRAME_RATE, MF_MT_FRAME_SIZE, MF_MT_MAJOR_TYPE, MF_MT_SUBTYPE, MF_PD_DURATION,
    MF_SOURCE_READERF_ENDOFSTREAM, MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING,
    MF_SOURCE_READER_FIRST_VIDEO_STREAM,
    MF_SOURCE_READER_MEDIASOURCE, MFSTARTUP_NOSOCKET, MF_VERSION,
};
use windows::Win32::System::Com::StructuredStorage::{
    PROPVARIANT, PROPVARIANT_0_0, PROPVARIANT_0_0_0,
};
use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_MULTITHREADED};
use windows::Win32::System::Variant::VT_I8;

const TICKS: f64 = 10_000_000.0; // 100ns

/// 视频元信息（Tauri 版固定用内置 libwebp，不再有编码器能力探测）。
#[derive(Clone, Debug, Default)]
pub struct Meta {
    pub width: u32,
    pub height: u32,
    pub fps: f64,
    pub duration: f64,
    pub frames: u32,
}

fn hr(e: &windows::core::Error) -> String {
    format!("0x{:08X} {}", e.code().0 as u32, e.message())
}

/// 统一的「解不了」提示：不静默失败，也不产出坏帧。
fn unsupported(src: &str, detail: &str) -> String {
    let name = Path::new(src)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or(src);
    format!(
        "无法解码这个视频（{name}）：{detail}\n\
         Tauri 版用系统自带的 Media Foundation 解码，只支持 mp4/mov 里的 H.264 / HEVC。\n\
         如果它是 webm / VP9 / AV1 等格式，请先转成 mp4（H.264），\
         或用 Python 版素材工坊处理。"
    )
}

// ===========================================================================
// COM / MF 生命周期
// ===========================================================================
struct ComGuard {
    uninit: bool,
}

impl ComGuard {
    fn new() -> Self {
        // 同线程可能已初始化过（S_FALSE / RPC_E_CHANGED_MODE 都不算失败）
        let uninit = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }.is_ok();
        let _ = unsafe { MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET) };
        Self { uninit }
    }
}

impl Drop for ComGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = MFShutdown();
            if self.uninit {
                CoUninitialize();
            }
        }
    }
}

// ===========================================================================
// PROPVARIANT（只需要整数读写）
// ===========================================================================
fn pv_i64_from(v: i64) -> PROPVARIANT {
    let mut pv = PROPVARIANT::default();
    pv.Anonymous.Anonymous = std::mem::ManuallyDrop::new(PROPVARIANT_0_0 {
        vt: VT_I8,
        wReserved1: 0,
        wReserved2: 0,
        wReserved3: 0,
        Anonymous: PROPVARIANT_0_0_0 { hVal: v },
    });
    pv
}

fn pv_as_i64(pv: &PROPVARIANT) -> Option<i64> {
    unsafe {
        let inner: &PROPVARIANT_0_0 = &*pv.Anonymous.Anonymous;
        match inner.vt.0 {
            20 => Some(inner.Anonymous.hVal),         // VT_I8
            21 => Some(inner.Anonymous.uhVal as i64), // VT_UI8
            3 => Some(inner.Anonymous.lVal as i64),   // VT_I4
            19 => Some(inner.Anonymous.ulVal as i64), // VT_UI4
            _ => None,
        }
    }
}

// ===========================================================================
// 读取器
// ===========================================================================
pub struct Reader {
    // Option 是为了在 drop 时先释放 COM 对象，再 MFShutdown / CoUninitialize
    inner: Option<IMFSourceReader>,
    _guard: ComGuard,
    pub meta: Meta,
    stride: i32, // 负值 = 自下而上（DIB 习惯）
}

impl Reader {
    pub fn open(src: &str) -> Result<Self, String> {
        if !Path::new(src).is_file() {
            return Err(format!("源视频不存在：{}", src));
        }
        let guard = ComGuard::new();
        let reader = unsafe { Self::open_inner(src) }?;
        Ok(Self {
            inner: Some(reader),
            _guard: guard,
            meta: Meta::default(),
            stride: 0,
        })
    }

    unsafe fn open_inner(src: &str) -> Result<IMFSourceReader, String> {
        let mut attrs = None;
        MFCreateAttributes(&mut attrs, 2).map_err(|e| format!("初始化 MF 属性失败：{}", hr(&e)))?;
        let attrs = attrs.ok_or_else(|| "初始化 MF 属性失败：空句柄".to_string())?;
        // 让 MF 插入 Video Processor MFT，负责 YUV → RGB32 的色彩转换。
        // 注意：MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING 在部分系统上会让
        // MFCreateSourceReaderFromURL 直接返回 E_INVALIDARG，所以只用基础开关。
        attrs
            .SetUINT32(&MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING, 1)
            .map_err(|e| format!("初始化 MF 属性失败：{}", hr(&e)))?;

        let url = HSTRING::from(src);
        let reader = MFCreateSourceReaderFromURL(&url, &attrs)
            .map_err(|e| unsupported(src, &format!("打开失败 {}", hr(&e))))?;
        reader
            .SetStreamSelection(MF_SOURCE_READER_FIRST_VIDEO_STREAM.0 as u32, true)
            .map_err(|e| unsupported(src, &format!("没有视频流 {}", hr(&e))))?;

        let mt = MFCreateMediaType().map_err(|e| format!("创建媒体类型失败：{}", hr(&e)))?;
        mt.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)
            .map_err(|e| format!("设置媒体类型失败：{}", hr(&e)))?;
        mt.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_RGB32)
            .map_err(|e| format!("设置媒体类型失败：{}", hr(&e)))?;
        reader
            .SetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM.0 as u32, None, &mt)
            .map_err(|e| {
                unsupported(src, &format!("转成 RGB32 失败（颜色格式不支持 {}）", hr(&e)))
            })?;
        Ok(reader)
    }

    /// 打开后补齐尺寸 / 帧率 / 时长。（放在这里是因为要读输出类型和原生类型）
    fn read_meta(&mut self, src: &str) -> Result<(), String> {
        let reader = self.inner.as_ref().ok_or("读取器未打开")?;
        unsafe {
            let cur = reader
                .GetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM.0 as u32)
                .map_err(|e| unsupported(src, &format!("读取输出格式失败 {}", hr(&e))))?;
            let size = cur.GetUINT64(&MF_MT_FRAME_SIZE).unwrap_or(0);
            let width = (size >> 32) as u32;
            let height = (size & 0xFFFF_FFFF) as u32;
            if width == 0 || height == 0 {
                return Err(unsupported(src, "读不出视频分辨率"));
            }
            let stride = cur
                .GetUINT32(&MF_MT_DEFAULT_STRIDE)
                .map(|v| v as i32)
                .unwrap_or(-(width as i32 * 4));

            let fps = reader
                .GetNativeMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM.0 as u32, 0)
                .ok()
                .and_then(|t| t.GetUINT64(&MF_MT_FRAME_RATE).ok())
                .map(|packed| {
                    let num = (packed >> 32) as f64;
                    let den = (packed & 0xFFFF_FFFF) as f64;
                    if den > 0.0 {
                        num / den
                    } else {
                        0.0
                    }
                })
                .unwrap_or(0.0);

            let duration = reader
                .GetPresentationAttribute(MF_SOURCE_READER_MEDIASOURCE.0 as u32, &MF_PD_DURATION)
                .ok()
                .and_then(|pv| pv_as_i64(&pv))
                .map(|t| (t as f64 / TICKS).max(0.0))
                .unwrap_or(0.0);

            let fps = if fps > 0.1 && fps < 240.0 { fps } else { 24.0 };
            self.meta = Meta {
                width,
                height,
                fps,
                duration,
                frames: ((duration * fps).round() as u32).max(1),
            };
            self.stride = stride;
        }
        Ok(())
    }

    fn reader(&self) -> Result<&IMFSourceReader, String> {
        self.inner.as_ref().ok_or_else(|| "读取器未打开".into())
    }

    /// 跳到 at 秒（时间格式 GUID_NULL = 100ns 单位；MF 会退到前一个关键帧）。
    pub fn seek(&self, at: f64) -> Result<(), String> {
        let pv = pv_i64_from((at.max(0.0) * TICKS).round() as i64);
        let fmt: GUID = GUID::zeroed();
        unsafe { self.reader()?.SetCurrentPosition(&fmt, &pv) }
            .map_err(|e| format!("跳转到 {:.2}s 失败：{}", at, hr(&e)))
    }

    /// 读下一帧整帧 RGBA（自上而下）。返回 (时间戳 100ns, RGBA)。
    pub fn next(&mut self) -> Result<Option<(i64, Vec<u8>)>, String> {
        loop {
            let mut flags = 0u32;
            let mut ts = 0i64;
            let mut sample: Option<windows::Win32::Media::MediaFoundation::IMFSample> = None;
            unsafe {
                self.reader()?.ReadSample(
                    MF_SOURCE_READER_FIRST_VIDEO_STREAM.0 as u32,
                    0,
                    None,
                    Some(&mut flags),
                    Some(&mut ts),
                    Some(&mut sample),
                )
            }
            .map_err(|e| format!("读帧失败：{}", hr(&e)))?;

            if flags & MF_SOURCE_READERF_ENDOFSTREAM.0 as u32 != 0 {
                return Ok(None);
            }
            let Some(sample) = sample else {
                continue; // 流间隙 / 非视频样本
            };
            let buf = unsafe { sample.ConvertToContiguousBuffer() }
                .map_err(|e| format!("取帧缓冲失败：{}", hr(&e)))?;
            let mut ptr = std::ptr::null_mut();
            let mut len = 0u32;
            unsafe { buf.Lock(&mut ptr, None, Some(&mut len)) }
                .map_err(|e| format!("锁定帧缓冲失败：{}", hr(&e)))?;
            let (w, h) = (self.meta.width as usize, self.meta.height as usize);
            let abs = self.stride.unsigned_abs() as usize;
            let usable = (len as usize / abs.max(1)).min(h);
            let mut out = vec![255u8; w * h * 4];
            if abs >= 4 && usable > 0 {
                let src = unsafe { std::slice::from_raw_parts(ptr, len as usize) };
                for y in 0..usable {
                    let sy = if self.stride < 0 {
                        usable - 1 - y
                    } else {
                        y
                    };
                    let s0 = sy * abs;
                    let d0 = y * w * 4;
                    let n = w.min(abs / 4);
                    for x in 0..n {
                        let s = s0 + x * 4;
                        let d = d0 + x * 4;
                        // RGB32 内存序是 BGRA；视频是不透明的，alpha 统一给 255
                        // （MF 输出的 RGB32 alpha 位恒为 0，保留会让预乘缩放把画面算成黑）
                        out[d] = src[s + 2];
                        out[d + 1] = src[s + 1];
                        out[d + 2] = src[s];
                        out[d + 3] = 255;
                    }
                }
            }
            unsafe {
                let _ = buf.Unlock();
            }
            return Ok(Some((ts, out)));
        }
    }
}

impl Drop for Reader {
    fn drop(&mut self) {
        self.inner = None; // 先放 COM 对象，再让 _guard 收尾
    }
}

// ===========================================================================
// 便捷入口
// ===========================================================================
fn open_with_meta(src: &str) -> Result<Reader, String> {
    let mut r = Reader::open(src)?;
    r.read_meta(src)?;
    Ok(r)
}

pub fn probe(src: &str) -> Result<Meta, String> {
    Ok(open_with_meta(src)?.meta.clone())
}

/// 整帧 → 裁切后（源坐标 → 裁切尺寸）。
fn crop_rgba(
    rgba: &[u8],
    w: usize,
    h: usize,
    crop: Option<[i32; 4]>,
    cw: usize,
    ch: usize,
) -> Vec<u8> {
    let Some([l, t, _, _]) = crop else {
        return rgba.to_vec();
    };
    let (l, t) = (l.max(0) as usize, t.max(0) as usize);
    let mut out = Vec::with_capacity(cw * ch * 4);
    for y in 0..ch {
        let sy = t + y;
        if sy >= h || l >= w {
            out.resize((out.len() / 4 + cw) * 4, 0);
            continue;
        }
        let s0 = (sy * w + l) * 4;
        let n = cw.min(w - l);
        out.extend_from_slice(&rgba[s0..s0 + n * 4]);
        if n < cw {
            out.resize((out.len() / 4 + (cw - n)) * 4, 0);
        }
    }
    out
}

/// 取 at 秒附近的一帧（裁切后）。
pub fn frame_cropped(
    src: &str,
    at: f64,
    crop: Option<[i32; 4]>,
    cw: usize,
    ch: usize,
) -> Result<Vec<u8>, String> {
    let mut r = open_with_meta(src)?;
    let (w, h) = (r.meta.width as usize, r.meta.height as usize);
    if at > 0.0 {
        r.seek(at)?;
    }
    let target = (at.max(0.0) * TICKS).round() as i64;
    let mut picked: Option<Vec<u8>> = None;
    while let Some((ts, rgba)) = r.next()? {
        picked = Some(rgba);
        if ts >= target {
            break;
        }
    }
    let Some(rgba) = picked else {
        return Err("没读到任何视频帧：文件可能损坏或不是视频".into());
    };
    Ok(crop_rgba(&rgba, w, h, crop, cw, ch))
}

/// 按目标帧率抽帧到临时 raw 文件（裁切后，rgba 顺序），返回帧数。
pub fn export_raw(
    src: &str,
    fps: u32,
    crop: Option<[i32; 4]>,
    cw: usize,
    ch: usize,
    dest: &Path,
) -> Result<usize, String> {
    use std::io::Write;
    let mut r = open_with_meta(src)?;
    let (w, h) = (r.meta.width as usize, r.meta.height as usize);
    let mut f = std::fs::File::create(dest).map_err(|e| format!("创建临时帧文件失败：{}", e))?;
    let fps = fps.max(1);
    let mut last_bucket: Option<i64> = None;
    let mut count = 0usize;
    while let Some((ts, rgba)) = r.next()? {
        let bucket = ((ts as f64) * fps as f64 / TICKS) as i64;
        let bucket = bucket.max(0);
        if last_bucket.map(|b| bucket <= b).unwrap_or(false) {
            continue; // 目标帧率的同一格，丢掉
        }
        last_bucket = Some(bucket);
        let frame = crop_rgba(&rgba, w, h, crop, cw, ch);
        f.write_all(&frame)
            .map_err(|e| format!("写临时帧文件失败：{}", e))?;
        count += 1;
    }
    Ok(count)
}
