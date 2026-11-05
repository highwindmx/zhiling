// DeskPet Tauri 前端：透明桌宠播放器。
// 与 deskpet-mini/pet.py 行为对齐：待机循环、动作段播完回待机、交叉淡化、
// 拖动、双击轮播动作、右键三档尺寸 / 动作 / 退出、Esc 退出。
//
// 内存要点：帧用 fetch + createImageBitmap 解码，位图由 LRU 自己管，
// 淘汰时 ImageBitmap.close() 主动释放。不能用 <img src> 循环切帧——
// Chromium 的 MemoryCache 会留住每一帧的解码位图。

const FADE_MS = 250;     // 与 pet.py 的 FADE_MS 一致
const PREFETCH = 8;      // 向前预取帧数（pet.py 同值）
const KEEP = 256;        // 上限；正常情况下按片段整体保留，切换片段时释放旧片段
const PRELOAD_CONCURRENCY = 4;
// 素材在 exe 同级的 assets/ 目录里（运行时读盘，不进 exe）。用自定义协议 deskpet://
// 而不是 file://：WebView2 默认拦 file:// 请求。Windows 上 Tauri 会把它映射成
// http://deskpet.localhost/<path>；macOS/Linux 是 deskpet://localhost/<path>。
const ASSET_BASE = (location.protocol === "http:" || location.protocol === "https:")
  ? `${location.protocol}//deskpet.localhost/`
  : "deskpet://localhost/";
const assetUrl = (rel) => ASSET_BASE + rel;
const TIERS = [
  { name: "小", ratio: 0.10 },
  { name: "中", ratio: 0.15 },
  { name: "大", ratio: 0.30 },
];
const DEFAULT_TIER = "中";

let CANVAS = [576, 530];
let CLIPS = {};
let IDLE_ID = null;
let ACTIONS = [];
let bmpW = 1, bmpH = 1;
let curTier = DEFAULT_TIER;

// ---- Tauri 全局 API（零构建，withGlobalTauri: true）----------------------
function T() { return window.__TAURI__ || null; }
async function invoke(cmd, args = {}) {
  const t = T();
  if (t && t.core && typeof t.core.invoke === "function") return t.core.invoke(cmd, args);
  if (t && typeof t.invoke === "function") return t.invoke(cmd, args);
  return null; // 浏览器预览
}
async function showWindow() {
  try { await invoke("show_window"); } catch (_) { /* ignore */ }
}
async function quitApp() {
  try { await invoke("quit_app"); } catch (_) { /* ignore */ }
  try { window.close(); } catch (_) { /* ignore */ }
}
async function startNativeDrag() {
  const t = T();
  try {
    if (t && t.window && t.window.getCurrentWindow) {
      return await t.window.getCurrentWindow().startDragging();
    }
  } catch (_) { /* ignore */ }
}

function logEvent(msg) {
  invoke("log_event", { msg }).catch(() => {});
}

// ---- 帧清单 --------------------------------------------------------------
// clips.json（顺序/类型/名字/defaultIdle）+ meta.json（目录/帧数/fps/格式/画布）
// 与 deskpet-mini 读的是同一份文件，字段语义完全一致，只是这里多了一步合并。
async function fetchJson(rel) {
  const res = await fetch(assetUrl(rel), { cache: "no-store" });
  if (!res.ok) throw new Error(`${rel} HTTP ${res.status}（${assetUrl(rel)}）`);
  return res.json();
}

async function loadClips() {
  const raw = await fetchJson("clips.json");
  const meta = await fetchJson("meta.json");
  const metaClips = meta.clips || {};
  const list = Array.isArray(raw.clips) ? raw.clips : Object.values(raw.clips || {});
  CLIPS = {};
  for (const e of list) {
    const id = e && e.id;
    if (!id) continue;
    const m = metaClips[id] || {};
    CLIPS[id] = {
      id,
      dir: m.dir || e.dir || `frames_${id}`,
      fps: m.fps ?? 24,
      count: m.count ?? 1,
      format: m.format || "webp",
      name: e.name || id,
      type: e.type || "action",
      loop: !!e.loop,
      priority: e.priority ?? 99,
    };
  }
  if (meta.canvas && meta.canvas.w && meta.canvas.h) {
    CANVAS = [meta.canvas.w, meta.canvas.h];
  }
  const ids = Object.keys(CLIPS);
  // defaultIdle 显式指定默认待机（旧 idle 可以降级成普通动作）；没有就退回第一个 type=idle
  IDLE_ID = (raw.defaultIdle && CLIPS[raw.defaultIdle])
    ? raw.defaultIdle
    : (ids.find((k) => CLIPS[k].type === "idle") || ids[0]);
  ACTIONS = ids.filter((k) => k !== IDLE_ID)
    .sort((a, b) => (CLIPS[a].priority ?? 99) - (CLIPS[b].priority ?? 99));
  if (!IDLE_ID) throw new Error("clips.json 里没有可用的待机段");
}

function frameUrl(id, idx) {
  const c = CLIPS[id];
  return assetUrl(`${c.dir}/f${String(idx).padStart(4, "0")}.${c.format || "webp"}`);
}

// ---- 位图缓存 ------------------------------------------------------------
const bitmaps = new Map();  // "id/idx" -> ImageBitmap，Map 顺序即 LRU
const pending = new Map();  // "id/idx" -> Promise<ImageBitmap>

function touch(map, key) {
  const value = map.get(key);
  map.delete(key);
  map.set(key, value);
  return value;
}

async function loadBitmap(id, idx) {
  const key = `${id}/${idx}`;
  if (bitmaps.has(key)) return touch(bitmaps, key);
  if (pending.has(key)) return pending.get(key);
  const job = fetch(frameUrl(id, idx), { cache: "no-store" })
    .then((r) => {
      if (!r.ok) throw new Error(`帧 ${key} HTTP ${r.status}`);
      return r.blob();
    })
    .then((blob) => createImageBitmap(blob, {
      // 直接解码到窗口物理像素（对齐 pet.py 的 QImage.scaled）
      resizeWidth: bmpW,
      resizeHeight: bmpH,
      resizeQuality: "medium",
    }))
    .then((bmp) => {
      pending.delete(key);
      bitmaps.set(key, bmp);
      trimBitmaps();
      return bmp;
    })
    .catch((e) => {
      pending.delete(key);
      throw e;
    });
  pending.set(key, job);
  return job;
}

function trimBitmaps() {
  while (bitmaps.size > KEEP) {
    const key = bitmaps.keys().next().value;
    const bmp = bitmaps.get(key);
    bitmaps.delete(key);
    if (bmp && typeof bmp.close === "function") bmp.close(); // 主动释放解码位图
  }
}

function dropOtherClips(keepIds) {
  const keep = new Set(keepIds);
  for (const key of [...bitmaps.keys()]) {
    const id = key.slice(0, key.indexOf("/"));
    if (!keep.has(id)) {
      const bmp = bitmaps.get(key);
      bitmaps.delete(key);
      if (bmp && typeof bmp.close === "function") bmp.close();
    }
  }
}

function clearBitmaps() {
  for (const bmp of bitmaps.values()) {
    if (bmp && typeof bmp.close === "function") bmp.close();
  }
  bitmaps.clear();
  // 尺寸变了，掩码也跟着失效
  maskCache.clear();
  maskCur = null;
  maskKey = "";
}

function prefetch(id, idx) {
  const n = CLIPS[id].count;
  for (let k = 1; k <= PREFETCH; k++) {
    loadBitmap(id, (idx + k) % n).catch(() => {});
  }
}

// 整段一次性预载并复用。WebView2 的 GPU 进程对频繁 createImageBitmap/close
// 的纹理回收很慢，逐圈重新解码会让 GPU 进程内存持续上涨；按片段整段保留后
// 每帧纹理只创建一次，待机循环不再产生新的纹理。
async function preloadClip(id, token) {
  const n = CLIPS[id].count;
  let next = 0;
  const worker = async () => {
    while (next < n) {
      const i = next++;
      if (token !== switchToken) return;
      try { await loadBitmap(id, i); } catch (_) { /* 单帧失败不阻塞整段 */ }
    }
  };
  await Promise.all([...Array(Math.min(PRELOAD_CONCURRENCY, n))].map(worker));
}

// ---- canvas 绘制 ---------------------------------------------------------
const canvasA = document.getElementById("layerA");
const canvasB = document.getElementById("layerB");
const ctxA = canvasA.getContext("2d");
const ctxB = canvasB.getContext("2d");

function sizeCanvases() {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(window.innerWidth * dpr));
  const h = Math.max(1, Math.round(window.innerHeight * dpr));
  for (const c of [canvasA, canvasB]) {
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  }
  bmpW = w;
  bmpH = h;
}

function drawCurrent(bmp) {
  ctxA.clearRect(0, 0, canvasA.width, canvasA.height);
  ctxA.drawImage(bmp, 0, 0, canvasA.width, canvasA.height);
}

// ---- 点击穿透 --------------------------------------------------------------
// 窗口在 Rust 侧用 set_ignore_cursor_events 整体开关；这里是"指针在不在宠物身上"
// 的判定：把当前帧画到 1/MASK_DIV 大小的离屏 canvas，读 alpha 得到二值掩码。
// 掩码按帧缓存（同一帧只算一次），重建节流 MASK_REFRESH_MS，正常待机第二轮起
// 命中的都是缓存。轮询 50ms 是因为窗口一旦 ignore，pointermove 就收不到了。
const MASK_DIV = 2;           // 与 pet.py 一致，边界精度 ~2px
const MASK_ALPHA_MIN = 8;
const MASK_REFRESH_MS = 150;
const HIT_POLL_MS = 50;
const MASK_CACHE_MAX = 600;

const maskCanvas = document.createElement("canvas");
const maskCtx = maskCanvas.getContext("2d", { willReadFrequently: true });
const maskCache = new Map();  // "id/idx" -> {gw, gh, alpha}
let maskCur = null;
let maskKey = "";
let maskLastAt = 0;
let maskBuilds = 0, maskLastMs = 0, maskTotalMs = 0;

function maskFor(key, bmp) {
  let m = maskCache.get(key);
  if (m) { maskCache.delete(key); maskCache.set(key, m); return m; }  // LRU 触碰
  const gw = Math.max(1, Math.floor(bmpW / MASK_DIV));
  const gh = Math.max(1, Math.floor(bmpH / MASK_DIV));
  if (maskCanvas.width !== gw) maskCanvas.width = gw;
  if (maskCanvas.height !== gh) maskCanvas.height = gh;
  const t0 = performance.now();
  maskCtx.clearRect(0, 0, gw, gh);
  maskCtx.drawImage(bmp, 0, 0, gw, gh);
  const data = maskCtx.getImageData(0, 0, gw, gh).data;
  const alpha = new Uint8Array(gw * gh);
  for (let i = 0; i < alpha.length; i++) alpha[i] = data[i * 4 + 3];
  const dt = performance.now() - t0;
  maskBuilds++; maskLastMs = dt; maskTotalMs += dt;
  m = { gw, gh, alpha };
  maskCache.set(key, m);
  while (maskCache.size > MASK_CACHE_MAX) maskCache.delete(maskCache.keys().next().value);
  return m;
}

function updateMask(id, idx, bmp, force = false) {
  const now = performance.now();
  if (!force && now - maskLastAt < MASK_REFRESH_MS) return;
  const key = `${id}/${idx}`;
  if (key === maskKey && !force) return;
  maskLastAt = now;
  maskCur = maskFor(key, bmp);
  maskKey = key;
}

let clickThrough = null;
async function setClickThrough(ignore) {
  if (ignore === clickThrough) return;
  const prev = clickThrough;
  clickThrough = ignore;
  try {
    await invoke("set_click_through", { ignore });
    logEvent(`click_through ignore=${ignore}`);
  } catch (_) {
    clickThrough = prev;
  }
}

function hitAt(lx, ly) {
  const m = maskCur;
  if (!m) return true;   // 掩码还没建好时先按命中处理，避免把点击放给下层
  const cx = Math.floor(lx * m.gw / Math.max(1, bmpW));
  const cy = Math.floor(ly * m.gh / Math.max(1, bmpH));
  if (cx < 0 || cy < 0 || cx >= m.gw || cy >= m.gh) return false;
  return m.alpha[cy * m.gw + cx] >= MASK_ALPHA_MIN;
}

async function decide(lx, ly, inside) {
  const hit = inside && hitAt(lx, ly);
  await setClickThrough(!hit || dragging);
  return hit;
}

async function pollCursor() {
  try {
    const p = await invoke("cursor_local");
    if (p) await decide(p.x, p.y, p.inside);
  } catch (_) { /* 命令不可用（浏览器预览）时什么都不做 */ }
}

let hasPainted = false;

function crossFade(bmp) {
  if (!hasPainted) { drawCurrent(bmp); hasPainted = true; return; }
  ctxB.clearRect(0, 0, canvasB.width, canvasB.height);
  ctxB.drawImage(canvasA, 0, 0);
  canvasB.style.transition = "none";
  canvasB.style.opacity = "1";
  drawCurrent(bmp);
  void canvasB.offsetWidth;
  canvasB.style.transition = `opacity ${FADE_MS}ms ease`;
  canvasB.style.opacity = "0";
}

// ---- 播放器 --------------------------------------------------------------
let state = null;          // {id, idx, isIdle, fps, next}
let raf = 0;
let idleResumeAt = 0;      // 动作结束后待机从哪一帧续播
let dblCursor = 0;
let switchToken = 0;

async function startClip(id, { isIdle = false, resumeFrom = 0, loop = false, onEnd = null } = {}) {
  const c = CLIPS[id];
  if (!c) return;
  if (!isIdle && state && state.isIdle) {
    idleResumeAt = (state.idx + 1) % CLIPS[IDLE_ID].count;
  }
  const token = ++switchToken;
  const idx = Math.min(resumeFrom, c.count - 1);
  // 待机段的位图始终保留，动作播完回待机是瞬时的；只释放上一段动作。
  dropOtherClips([IDLE_ID, id]);
  try {
    const bmp = await loadBitmap(id, idx);
    if (token !== switchToken) return; // 被更新的切换取代
    crossFade(bmp);
    updateMask(id, idx, bmp, true);
    logEvent(`clip_start id=${id} idle=${isIdle} idx=${idx} loop=${loop}`);
    state = {
      id, idx, isIdle, loop, onEnd,
      fps: c.fps || 24,
      next: performance.now() + 1000 / (c.fps || 24),
    };
    preloadClip(id, token);   // 不 await：先播首帧，整段在后台补载
    if (!raf) raf = requestAnimationFrame(loop);
  } catch (e) {
    console.error("帧加载失败", e);
  }
}

function startIdle() {
  startClip(IDLE_ID, { isIdle: true, resumeFrom: idleResumeAt, loop: true });
}

function playAction(id) {
  if (id && id !== IDLE_ID) startClip(id, { isIdle: false });
  else startIdle();
}

function loop(now) {
  if (paused) { raf = 0; return; }
  if (!state) { raf = 0; return; }
  const s = state;
  const interval = 1000 / s.fps;
  if (now >= s.next) {
    const n = CLIPS[s.id].count;
    if (s.idx + 1 >= n && !s.loop) {
      // 非循环段播完：先摘掉状态并停掉本轮 rAF，避免异步切段期间重复触发
      state = null;
      raf = 0;
      if (typeof s.onEnd === "function") s.onEnd();
      else startIdle();
      return;
    }
    const nextIdx = (s.idx + 1) % n;
    const key = `${s.id}/${nextIdx}`;
    if (bitmaps.has(key)) {
      s.idx = nextIdx;
      const bmp = touch(bitmaps, key);
      drawCurrent(bmp);
      updateMask(s.id, s.idx, bmp);
      prefetch(s.id, s.idx);
      s.next = now + interval;
    } else {
      // 位图还没解码好：保持当前帧，稍后重试（不快放）
      loadBitmap(s.id, nextIdx).catch(() => {});
      s.next = now + 8;
    }
  }
  raf = requestAnimationFrame(loop);
}

function playNextAction() {
  if (!ACTIONS.length || !state || !state.isIdle) return;
  playAction(ACTIONS[dblCursor % ACTIONS.length]);
  dblCursor = (dblCursor + 1) % ACTIONS.length;
}

window.addEventListener("resize", () => {
  const before = bmpW + "x" + bmpH;
  sizeCanvases();
  if (before === bmpW + "x" + bmpH) return;
  clearBitmaps();
  ctxB.clearRect(0, 0, canvasB.width, canvasB.height);
  hasPainted = false;
  if (state) {
    const s = state;
    loadBitmap(s.id, s.idx)
      .then((bmp) => { if (state === s) { drawCurrent(bmp); hasPainted = true; } })
      .catch(() => {});
    prefetch(s.id, s.idx);
  }
});

// ---- 尺寸档位 ------------------------------------------------------------
const aspect = () => (CANVAS[1] || 530) / (CANVAS[0] || 576);

function screenBox() {
  const s = window.screen;
  return {
    width: s.availWidth || s.width || 1280,
  };
}

async function applyTier(name, { reposition = false } = {}) {
  const t = TIERS.find((x) => x.name === name) || TIERS[1];
  curTier = t.name;
  const box = screenBox();
  const width = Math.max(140, Math.round(box.width * t.ratio));
  const height = Math.max(1, Math.round(width * aspect()));
  await invoke("set_tier", { width, height, reposition });
  await new Promise((r) => requestAnimationFrame(() => r()));
  const before = bmpW + "x" + bmpH;
  sizeCanvases();
  if (before !== bmpW + "x" + bmpH) {
    clearBitmaps();
    ctxB.clearRect(0, 0, canvasB.width, canvasB.height);
    hasPainted = false;
    if (state) {
      const s = state;
      loadBitmap(s.id, s.idx)
        .then((bmp) => { if (state === s) { drawCurrent(bmp); hasPainted = true; } })
        .catch(() => {});
      prefetch(s.id, s.idx);
    }
  }
  return { width, height };
}

// 由 Rust 原生菜单回调（避免 HTML 菜单被无边框窗口裁切）
window.__deskpetPick = (key) => {
  if (key.startsWith("tier:")) applyTier(key.slice(5));
  else if (key.startsWith("action:")) {
    if (state && state.isIdle) playAction(key.slice(7));
  } else if (key === "quit") quitApp();
};

// ---- 右键菜单 ------------------------------------------------------------
document.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  invoke("show_menu", {
    tier: curTier,
    actions: ACTIONS.map((id) => ({ id, name: CLIPS[id].name })),
  }).catch(() => {});
});

// ---- 拖动 + 双击 ---------------------------------------------------------
let down = null;
let dragging = false;

window.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  down = { x: e.screenX, y: e.screenY, t: performance.now() };
  dragging = false;
});
window.addEventListener("pointermove", (e) => {
  if (!down || dragging) return;
  if (Math.hypot(e.screenX - down.x, e.screenY - down.y) < 4) return;
  dragging = true;
  down = null;
  startNativeDrag().finally(() => { dragging = false; });
});
let clickTimer = 0;
window.addEventListener("pointerup", () => {
  const d = down;
  down = null;
  // 单击（不是拖动的松开）算一个可观测状态，交给状态映射决定播什么。
  // 延迟 250ms 再判定：如果紧跟一个 dblclick，就把这次单击取消（否则炸毛会被顶成"得意"）
  if (d && !dragging && performance.now() - d.t < 400) {
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => {
      clearReminder("单击");
      setState("click", "单击");
    }, 250);
  }
});
window.addEventListener("dblclick", () => {
  clearTimeout(clickTimer);
  logEvent("dblclick");
  clearReminder("双击");
  // 有 double_click 规则就走状态映射（默认给炸毛）；没有就退回旧的"依次轮播动作"
  if (rules.has("double_click")) setState("double_click", "双击");
  else playNextAction();
});
// 右键（菜单）也是"用户在响应"，要清掉提醒态
window.addEventListener("contextmenu", () => clearReminder("右键"));

// ---- 键盘 ----------------------------------------------------------------
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") quitApp();
});

// ---- 状态映射 --------------------------------------------------------------
// 配置：assets/state-map.json（源文件 repo 的 packs/<packId>/state-map.json）。
// 与 deskpet-mini/state_map.py 同一套语义：每个可观测状态 → 一个 clip，
// 播放前过 冷却 / 占用（busy|hold）/ 禁用 三道闸；priority 用 0~100 口径。
// 两个提醒态（awaiting_user / needs_decision）额外支持 repeat：
//   once     —— 只播一次，之后静默回待机（不常驻循环）
//   interval —— 每隔 intervalSec 再播一次，最多 maxTimes 次
const STATES = ["idle", "long_idle", "awaiting_user", "busy", "streaming",
                "done", "needs_decision", "error", "click", "double_click"];
const STATE_LOG_MAX = 20;

const stateLog = [];
let rules = new Map();       // state -> rule
let longIdleSec = 600;
let awaitingUserSec = 30;
let stateCfgText = "";
let curState = null;
let longIdleAt = 0;          // 进入 idle 的时刻（用于 long_idle / awaiting_user）
let awaitingAt = 0;          // 进入 done 的时刻（用于 awaiting_user）
let cur = null;              // 当前占用：{clipId, priority, holdUntil, interruptible}
let silenced = false;        // 当前状态已经"不再播"（once 播完 / 次数用尽 / 超时）
let reminders = 0;           // 提醒态已重复次数
let reminderPending = false; // 提醒态正在等下一次提醒（此间不做画面补齐，免得每 tick 重播）
let deadlineAt = 0;          // 提醒态超时时刻（0=没有）
const lastPlay = new Map();  // clipId -> 上次开始播放的时刻

function stateDbg(msg) {
  const line = `${msg} | state=${curState || "-"} cur=${cur ? cur.clipId : "-"}`;
  stateLog.push(line);
  if (stateLog.length > STATE_LOG_MAX) stateLog.shift();
  logEvent(`state ${line}`);
}

function num(v, dflt) {
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : dflt;
}

function parseStateMap(text) {
  const raw = JSON.parse(text);
  const list = Array.isArray(raw.rules) ? raw.rules : [];
  const out = new Map();
  for (let i = 0; i < list.length; i++) {
    const r = list[i] || {};
    try {
      if (typeof r.state !== "string" || !STATES.includes(r.state)) {
        throw new Error(`未知状态 ${JSON.stringify(r.state)}`);
      }
      if (!r.clipId || !CLIPS[r.clipId]) throw new Error(`clipId 不存在: ${r.clipId}`);
      if (out.has(r.state)) throw new Error(`状态 ${r.state} 重复，只保留第一条`);
      const rp = r.repeat && typeof r.repeat === "object" ? r.repeat : null;
      out.set(r.state, {
        state: r.state,
        clipId: r.clipId,
        priority: num(r.priority, 20),
        minPlayMs: num(r.minPlayMs, 0),
        cooldownSec: num(r.cooldownSec, 0),
        loop: !!r.loop,
        interruptible: r.interruptible !== false,
        enabled: r.enabled !== false,
        repeat: rp ? {
          mode: rp.mode === "interval" ? "interval" : "once",
          intervalSec: num(rp.intervalSec, 75),
          maxTimes: Math.max(1, Math.floor(num(rp.maxTimes, 3))),
        } : null,
      });
    } catch (e) {
      // 只废这一条，其它规则照常生效
      stateDbg(`rule#${i} 无效: ${e.message}`);
    }
  }
  return {
    rules: out,
    longIdleSec: num(raw.longIdleSec, 600),
    awaitingUserSec: num(raw.awaitingUserSec, 30),
  };
}

async function loadStateMap(reason) {
  let text;
  try {
    const res = await fetch(assetUrl("state-map.json"), { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    text = await res.text();
  } catch (e) {
    if (!stateCfgText) stateDbg(`state-map 缺失: ${e.message}（用内置默认）`);
    return false;
  }
  if (text === stateCfgText) return false;
  try {
    const parsed = parseStateMap(text);
    rules = parsed.rules;
    longIdleSec = parsed.longIdleSec;
    awaitingUserSec = parsed.awaitingUserSec;
    stateCfgText = text;
    stateDbg(`state-map 载入 ${reason}: ${rules.size} 条 longIdle=${longIdleSec}s `
      + `awaitingUser=${awaitingUserSec}s`);
    return true;
  } catch (e) {
    stateDbg(`state-map 解析失败（保留上一版）: ${e.message}`);
    return false;
  }
}

function rejectPlay(rule, source, why) {
  stateDbg(`拒绝 ${source}→${rule.clipId}：${why}`);
  return false;
}

function requestPlay(rule, source) {
  if (!rule.enabled) return rejectPlay(rule, source, "disabled");
  const now = performance.now();
  const last = lastPlay.has(rule.clipId) ? lastPlay.get(rule.clipId) : -1e12;
  if (now < last + rule.cooldownSec * 1000) {
    return rejectPlay(rule, source,
      `cooldown 剩余 ${((last + rule.cooldownSec * 1000 - now) / 1000).toFixed(1)}s`);
  }
  if (cur && now < cur.holdUntil) {
    if (!cur.interruptible) return rejectPlay(rule, source, `hold:${cur.clipId}`);
    if (rule.priority <= cur.priority) return rejectPlay(rule, source, `busy:${cur.clipId}`);
  }
  const c = CLIPS[rule.clipId];
  const durMs = (c.count / (c.fps || 24)) * 1000;
  lastPlay.set(rule.clipId, now);
  cur = {
    clipId: rule.clipId, priority: rule.priority,
    holdUntil: now + Math.max(durMs, rule.minPlayMs),
    interruptible: rule.interruptible,
  };
  stateDbg(`命中 ${source}→${rule.clipId}（${c.name}）prio=${rule.priority} loop=${rule.loop}`);
  startClip(rule.clipId, {
    isIdle: rule.clipId === IDLE_ID,
    loop: rule.loop,
    onEnd: () => {
      cur = null;
      onStateClipEnd(rule, source);
    },
  });
  return true;
}

/// 一个状态段播完之后去哪：普通状态回 idle；提醒态按 repeat 决定再播还是静默。
function onStateClipEnd(rule, source) {
  const rp = rule.repeat;
  if (!rp || rule.loop) {
    if (curState !== "idle") setState("idle", "clip-end");
    return;
  }
  if (rp.mode === "interval" && reminders + 1 < rp.maxTimes
      && (deadlineAt === 0 || performance.now() < deadlineAt)) {
    reminders += 1;
    reminderPending = true;
    startIdle();                       // 间隔期先回到低干扰的待机画面
    setTimeout(() => {
      reminderPending = false;
      if (curState === source && !silenced) requestPlay(rule, source + ":reminder");
    }, rp.intervalSec * 1000);
    stateDbg(`提醒 ${source} 第 ${reminders + 1}/${rp.maxTimes} 次，${rp.intervalSec}s 后`);
    return;
  }
  // once 或次数用尽：不再播，静默待机（状态本身还没退出，等外部事件）
  silenced = true;
  reminderPending = false;
  stateDbg(`${source} 提醒结束（${rp.mode}），转为静默待机`);
  startIdle();
}

function setState(name, why = "") {
  if (!STATES.includes(name)) return;
  if (name === curState) return;
  const prev = curState;
  curState = name;
  silenced = false;
  reminders = 0;
  reminderPending = false;
  deadlineAt = 0;
  const now = performance.now();
  if (name === "idle") longIdleAt = now;
  if (name === "done") awaitingAt = now;
  if (name === "needs_decision") {
    deadlineAt = now + (num(needsDecisionTimeoutSec, 300) * 1000);
  }
  stateDbg(`切换 ${name}（${why}${prev ? "，来自 " + prev : ""}）`);
  const rule = rules.get(name);
  if (!rule) {
    stateDbg(`${name} 没有规则 → 保持当前画面`);
    return;
  }
  // 已经在这个画面（启动时先画了首帧）就别从第 0 帧重开，避免开屏闪一下
  if (name === "idle" && state && state.id === rule.clipId) {
    const c = CLIPS[rule.clipId];
    cur = { clipId: rule.clipId, priority: rule.priority,
            holdUntil: now + (c.count / (c.fps || 24)) * 1000,
            interruptible: rule.interruptible };
    stateDbg(`已在播 ${rule.clipId}，续播`);
    return;
  }
  requestPlay(rule, name);
}

// 外部事件入口（DSH 桥接层调用）：
//   done / busy / streaming / error / attention —— 状态事件
//   user_input —— 用户开始输入（会清掉两个提醒态）
// needs_decision 需要 DSH 补 attention 事件，见 docs/material-packs.md §状态事件
// 调试：控制台里 __deskpetLog() 看最近 20 条"状态 → 命中哪条 → 被谁拒"
window.__deskpetLog = () => stateLog.slice();
window.__deskpetState = (name, extra) => {
  if (name === "attention") {
    needsDecisionTimeoutSec = extra && extra.timeoutSec;
    setState("needs_decision", "attention:" + ((extra && extra.kind) || "?"));
    return;
  }
  if (name === "user_input") {
    onUserInput();
    return;
  }
  setState(name, "external");
};

let needsDecisionTimeoutSec = 0;

function onUserInput() {
  if (curState === "awaiting_user" || curState === "needs_decision") {
    stateDbg(`用户有输入 → 清除提醒（原状态 ${curState}）`);
    setState("busy", "user_input");
  }
}

// 手动操作（单击/双击/右键）＝用户在响应，提醒态立即退场（不必等 DSH 的 user_input）
function clearReminder(why) {
  if (curState === "awaiting_user" || curState === "needs_decision") {
    stateDbg(`手动操作（${why}）→ 清除提醒（原状态 ${curState}）`);
    setState("idle", "manual-clear");
  }
}

// 每秒一次的派生状态维护：长待机、awaiting_user、超时、以及"状态与画面不同步"的补齐
function stateTick() {
  const now = performance.now();
  if (curState === "idle" && now - longIdleAt > longIdleSec * 1000) {
    setState("long_idle", `空闲 ${longIdleSec}s`);
  }
  if (curState === "done" && now - awaitingAt > awaitingUserSec * 1000) {
    setState("awaiting_user", `done 后 ${awaitingUserSec}s 无输入`);
  }
  if (curState === "needs_decision" && deadlineAt && now > deadlineAt) {
    stateDbg("needs_decision 超时 → 降级为 idle");
    setState("idle", "decision-timeout");
    return;
  }
  // 画面补齐：目标状态的 clip 没在播、且当前没有占用 → 重试（比如被冷却/占用拒过一次）
  // 提醒态正在等下一次提醒时不补齐，否则会每秒重播一遍完整动作
  if (!silenced && !reminderPending && curState) {
    const rule = rules.get(curState);
    if (rule && !(state && state.id === rule.clipId) && !(cur && now < cur.holdUntil)) {
      requestPlay(rule, curState + ":reconcile");
    }
  }
}

async function startStateMap() {
  await loadStateMap("启动");
  if (!rules.has("idle")) {
    // 配置缺失/坏了时的兜底：至少待机照旧，不让窗口空着
    rules.set("idle", { state: "idle", clipId: IDLE_ID, priority: 0, minPlayMs: 0,
                        cooldownSec: 0, loop: true, interruptible: true,
                        enabled: true, repeat: null });
  }
  curState = null;
  setState("idle", "启动");
  setInterval(loadStateMap, 1000);   // 热加载：内容没变就什么都不做
  setInterval(stateTick, 1000);
}

function showHint(title, detail) {
  const el = document.getElementById("hint");
  if (!el) return;
  el.style.display = "block";
  el.innerHTML = `<b>${title}</b><br><br>素材目录：<br><code>${detail}</code><br><br>`
    + `把 <code>assets</code> 目录放到 exe 同级，或设置环境变量 `
    + `<code>DESKPET_ASSETS</code> 指向素材目录。`;
}

// ---- 启动 ----------------------------------------------------------------
// 调试钩子（仅用于测量/排障，不影响正常逻辑）
let paused = false;
window.__deskpetDebug = {
  stats: () => ({ bitmaps: bitmaps.size, pending: pending.size, state: state && state.id }),
  mask: () => ({ key: maskKey, gw: maskCur && maskCur.gw, gh: maskCur && maskCur.gh,
                 builds: maskBuilds, lastMs: maskLastMs, totalMs: maskTotalMs,
                 cache: maskCache.size, clickThrough }),
  hitAt: (x, y) => hitAt(x, y),
  simCursor: (x, y) => decide(x, y, x >= 0 && y >= 0 && x < bmpW && y < bmpH),
  setIgnore: (v) => setClickThrough(v),
  pause: () => { paused = true; if (raf) cancelAnimationFrame(raf); raf = 0; },
  resume: () => { if (!raf) { paused = false; raf = requestAnimationFrame(loop); } },
};

(async () => {
  try {
    const info = await invoke("assets_info");
    if (info && (!info.exists || !info.clips)) {
      showHint("找不到素材目录", info.root);
      // 默认窗口只有 192x177 逻辑像素，红框里的补救说明放不下：临时放大到 460x300
      try { await invoke("set_tier", { width: 460, height: 300, reposition: true }); } catch (_) { /* ignore */ }
      await showWindow();
      return;
    }
    await loadClips();
    await applyTier(DEFAULT_TIER, { reposition: true });
    sizeCanvases();
    const first = await loadBitmap(IDLE_ID, 0);  // 首帧解码完再显示窗口
    drawCurrent(first);
    updateMask(IDLE_ID, 0, first, true);
    hasPainted = true;
    await setClickThrough(true);   // 先整体穿透，指针在宠物身上时由轮询翻回来
    await showWindow();
    setInterval(pollCursor, HIT_POLL_MS);   // 窗口 ignore 时收不到 pointermove，只能轮询
    state = { id: IDLE_ID, idx: 0, isIdle: true, fps: CLIPS[IDLE_ID].fps || 24,
              next: performance.now() + 1000 / (CLIPS[IDLE_ID].fps || 24) };
    prefetch(IDLE_ID, 0);
    // 素材目录已就绪，顺便把状态映射配置读进来（热加载，见 §state-map）
    startStateMap();
    raf = requestAnimationFrame(loop);
  } catch (e) {
    console.error("素材加载失败", e);
    showHint("素材加载失败", String(e && e.message ? e.message : e));
    await showWindow();
  }
})();
