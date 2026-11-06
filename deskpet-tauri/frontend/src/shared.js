// 三套前端页（main / settings / workshop）共用的零构建 ES 模块。
//
// 这里只放「跨页面完全一致、且不会因为页面语义而变化」的定义：
//   - 状态机状态名枚举与中文映射（与 deskpet-python 的 state_map 对齐）
//   - 素材协议基地址（deskpet:// 或 http://deskpet.localhost/）
//   - Tauri 全局 API 封装（withGlobalTauri: true 时在 window.__TAURI__）
//   - HTML 转义（防 DOM 注入）
//
// 注意：本文件**不**放各页面私有的东西——例如 main.js 的 REMINDER_STATES /
// HOLD_STATES（只主播放器用）、settings.js 的 DEFAULT_ORDER、workshop.js 的
// FIXED_VAL_LO/HI。也**不**放历史记录里那套「研究会」式的中间层，保持直白可读。

// ---- 状态机：13 个状态（顺序即优先级基线）------------------------------
export const STATES = [
  "double_click", "click", "error", "needs_decision", "done",
  "focus", "focus_break",
  "busy", "streaming", "awaiting_user", "long_idle_2", "long_idle_1", "idle",
];

export const STATE_CN = {
  idle: "待机", long_idle_1: "长时间空闲 ①", long_idle_2: "长时间空闲 ②",
  awaiting_user: "等你回复", busy: "忙碌", streaming: "输出中",
  done: "完成", needs_decision: "待决策", error: "出错",
  click: "单击", double_click: "双击",
  focus: "专注（番茄钟）", focus_break: "休息（番茄钟）",
};

// ---- 素材协议基地址 ------------------------------------------------------
// 素材在 exe 同级的 assets/ 目录里（运行时读盘，不进 exe）。用自定义协议 deskpet://
// 而不是 file://：WebView2 默认拦 file:// 请求。Windows 上 Tauri 会把它映射成
// http://deskpet.localhost/<path>；macOS/Linux 是 deskpet://localhost/<path>。
const ASSET_BASE = (location.protocol === "http:" || location.protocol === "https:")
  ? `${location.protocol}//deskpet.localhost/`
  : "deskpet://localhost/";

export function assetUrl(rel) {
  return ASSET_BASE + rel;
}

// ---- Tauri 全局 API（零构建，withGlobalTauri: true）----------------------
export function T() {
  return window.__TAURI__ || null;
}

// invoke 失败时返回 null（浏览器里预览时 Tauri 不存在也走这里），调用方自行 .catch。
export async function invoke(cmd, args = {}) {
  const t = T();
  if (t && t.core && typeof t.core.invoke === "function") return t.core.invoke(cmd, args);
  if (t && typeof t.invoke === "function") return t.invoke(cmd, args);
  return null; // 浏览器预览
}

// ---- HTML 转义（防 DOM 注入）---------------------------------------------
export function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- 默认画布（素材没给 canvas 时的兜底）---------------------------------
// 与 prepare_assets.py 的 DEFAULT_CANVAS、Rust 侧默认一致。
export const DEFAULT_CANVAS = [576, 530];

export function canvasOf(meta) {
  const c = (meta && meta.canvas) || {};
  if (Array.isArray(c)) return [Number(c[0]) || DEFAULT_CANVAS[0], Number(c[1]) || DEFAULT_CANVAS[1]];
  return [Number(c.w) || DEFAULT_CANVAS[0], Number(c.h) || DEFAULT_CANVAS[1]];
}

// ---- 端口默认值（与 Rust main.rs 的 dshPort / STATE_PORT_DEFAULT 对齐）----
export const DEFAULT_DSH_PORT = 55471;
export const DEFAULT_STATE_PORT = 8765;

// ---- 位图缓存上限 ---------------------------------------------------------
// 主播放器与设置页预览共用同一套 LRU 语义，上限分开给（主播放器要大得多）。
export const BITMAP_KEEP = 256;   // 主播放器：正常按片段整体保留，切片段时释放旧片段
export const PREVIEW_KEEP = 48;   // 设置页预览：一次只看一段，上限小

// ---- 帧位图 LRU 缓存 ------------------------------------------------------
// 两处逻辑同构：主播放器 loadBitmap / 设置页预览 pvBitmap。差异只在“怎么拿到
// 一张位图”和“URL 怎么拼”，所以把缓存壳做成类，解码函数由调用方注入。
//
// 关键点：
//   - Map 的插入顺序即 LRU 顺序，touch 把访问过的 key 移到末尾；
//   - 淘汰时调用 decoder 返回的 ImageBitmap.close()，主动释放解码内存；
//   - protected 前缀集合（如 "idle/"、"当前片段id/"）里的帧永不被淘汰 —— 这样
//     preloadClip 整段预载后，即便片段帧数超过 keep，循环也始终纹理只建一次
//     （见 P2-10），同时不会把其它片段的帧挤爆。
export class FrameCache {
  constructor({ keep, decoder }) {
    this.keep = keep;
    this.decoder = decoder; // (id, idx) => Promise<ImageBitmap>
    this.bitmaps = new Map();  // "id/idx" -> ImageBitmap
    this.pending = new Map();  // "id/idx" -> Promise<ImageBitmap>
    this.protectedPrefix = new Set();
  }

  key(id, idx) { return `${id}/${idx}`; }

  touch(key) {
    const v = this.bitmaps.get(key);
    this.bitmaps.delete(key);
    this.bitmaps.set(key, v);
    return v;
  }

  has(key) { return this.bitmaps.has(key); }
  get(key) { return this.touch(key); }

  // 把 id 加入“永不被淘汰”前缀集合（例：当前播放片段 + idle 片段）。
  protect(id) { if (id) this.protectedPrefix.add(`${id}/`); }
  unprotect(id) { if (id) this.protectedPrefix.delete(`${id}/`); }

  // 淘汰：优先丢最旧、且不在 protectedPrefix 里的帧；protected 帧永远保留。
  trim() {
    const it = this.bitmaps.keys();
    let r = it.next();
    while (this.bitmaps.size > this.keep && !r.done) {
      const k = r.value;
      r = it.next();
      if (this.protectedPrefix.size) {
        let prot = false;
        for (const p of this.protectedPrefix) { if (k.startsWith(p)) { prot = true; break; } }
        if (prot) continue; // 受保护的帧先留着
      }
      const b = this.bitmaps.get(k);
      this.bitmaps.delete(k);
      if (b && typeof b.close === "function") b.close();
    }
  }

  // 释放除 keepIds 以外所有片段的帧（切片段时调用）。
  dropOtherClips(keepIds) {
    const keep = new Set(keepIds);
    for (const key of [...this.bitmaps.keys()]) {
      const id = key.slice(0, key.indexOf("/"));
      if (!keep.has(id)) {
        const b = this.bitmaps.get(key);
        this.bitmaps.delete(key);
        if (b && typeof b.close === "function") b.close();
      }
    }
  }

  clear() {
    for (const b of this.bitmaps.values()) {
      if (b && typeof b.close === "function") b.close();
    }
    this.bitmaps.clear();
    this.pending.clear();
  }

  async load(id, idx) {
    const key = this.key(id, idx);
    if (this.bitmaps.has(key)) return this.touch(key);
    if (this.pending.has(key)) return this.pending.get(key);
    const job = Promise.resolve(this.decoder(id, idx))
      .then((bmp) => {
        this.pending.delete(key);
        this.bitmaps.set(key, bmp);
        this.trim();
        return bmp;
      })
      .catch((e) => {
        this.pending.delete(key);
        throw e;
      });
    this.pending.set(key, job);
    return job;
  }
}
