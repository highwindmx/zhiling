// DeskPet Tauri 前端：透明桌宠播放器 + v2 状态机（与 deskpet-python 对齐）。
//
// 数据：assets/themes/<id>.json（11 行状态 × 0~N 段 clipIds）、
//       assets/animations/<assetId>/{meta.json,frames}、assets/active.json。
// 播放：帧用 fetch + createImageBitmap 解码，位图由 LRU 自己管，淘汰时 close()；
//       不能用 <img src> 循环切帧——Chromium 的 MemoryCache 会留住每一帧的解码位图。
//
// 与 pet.py / state_map.py 的关键语义：
//   - 每个状态维护 clipIds 游标，顺序轮播；单段池才真循环，多段池播完接着下一段。
//   - clipIds 为空 = 零绑定：到达该状态时不换画面，也算正常。
//   - 被冷却/占用拒绝时回滚状态名，避免“状态=streaming 但画面还在 handup”。
//   - 提醒态常驻（config.json: remindHold）：done/error/needs_decision 播完不回 idle，
//     一直轮播，直到单击/右键/输入（force 清除）。

import { STATES, STATE_CN, assetUrl, T, invoke, esc, DEFAULT_CANVAS, canvasOf, FrameCache, BITMAP_KEEP } from "./shared.js";

const FADE_MS = 250;     // 与 pet.py 的 FADE_MS 一致
const PREFETCH = 8;      // 向前预取帧数（pet.py 同值）
const PRELOAD_CONCURRENCY = 4;
const STATE_LOG_MAX = 20;

const TIERS = [
  { name: "小", ratio: 0.10 },
  { name: "中", ratio: 0.15 },
  { name: "大", ratio: 0.30 },
];
const DEFAULT_TIER = "中";

const REMINDER_STATES = new Set(["awaiting_user", "needs_decision"]);
const HOLD_STATES = new Set(["done", "error", "needs_decision"]);

let CLIPS = {};          // assetId -> {id,dir,fps,count,format,name,type,loop,priority,canvas}
let ACTIONS = [];        // 双击兜底轮播：除 idle 外按 priority 升序
let IDLE_ID = null;
let CANVAS = null;       // 当前窗口对应的画布；切到不同比例的段时更新
let curTier = DEFAULT_TIER;
let curWidth = 0;        // 当前档位宽度（逻辑像素），换画布时保持宽度
let holdReminders = true;

let THEMES = [];         // [{id,name,active,nStates,nBound}]
let THEME_ID = "";
let THEME_RAW = null;    // 原始主题对象（保留未知字段）
let THEME_TEXT = "";
let themeCheckAt = 0;

let longIdleSec = 600;
let awaitingUserSec = 30;

// 设置里换了素材目录后，Rust 调这个让桌宠按新目录重新加载（窗口/位置不变）
window.__deskpetReload = () => location.reload();

async function showWindow() {
  try { await invoke("show_window"); } catch (_) { /* ignore */ }
}
async function quitApp() {
  try { await invoke("quit_app"); } catch (_) { /* ignore */ }
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
function reportState() {
  invoke("report_state", { state: curState || "", playing: playing || "" }).catch(() => {});
}
function saveConfig(patch) {
  invoke("save_config", { patch }).catch(() => {});
}

// ---- 数据加载 -------------------------------------------------------------
async function fetchJson(rel, optional = false) {
  const res = await fetch(assetUrl(rel), { cache: "no-store" });
  if (!res.ok) {
    if (optional) return null;
    throw new Error(`${rel} HTTP ${res.status}（${assetUrl(rel)}）`);
  }
  return res.json();
}

function sameAspect(a, b) {
  // 整数交叉相乘，2 倍分辨率也算同比例（516/530 vs 1152/1060）
  if (!a || !b) return false;
  return a[0] * b[1] === b[0] * a[1];
}

function displayHeight(width, canvas) {
  const [w, h] = canvas || CANVAS || DEFAULT_CANVAS;
  return Math.round((width * h) / w);
}

async function loadAssets() {
  const list = (await invoke("list_assets")) || [];
  const next = {};
  for (const m of list) {
    const id = m && (m.assetId || m.id);
    if (!id) continue;
    next[id] = {
      id,
      dir: m.dir || `animations/${id}`,
      fps: Number(m.fps) || 24,
      count: Math.max(1, Number(m.count) || 1),
      format: String(m.format || "webp").toLowerCase(),
      name: m.name || id,
      type: m.type || "action",
      loop: !!m.loop,
      priority: Number.isFinite(Number(m.priority)) ? Number(m.priority) : 99,
      canvas: canvasOf(m),
    };
  }
  CLIPS = next;
  return Object.keys(next).length;
}

function rebuildActions() {
  ACTIONS = Object.keys(CLIPS)
    .filter((id) => id !== IDLE_ID)
    .sort((a, b) => (CLIPS[a].priority ?? 99) - (CLIPS[b].priority ?? 99));
}

async function loadThemeText(tid) {
  const res = await fetch(assetUrl(`themes/${tid}.json`), { cache: "no-store" });
  if (!res.ok) throw new Error(`主题 ${tid} 读取失败 (HTTP ${res.status})`);
  return res.text();
}

function computeIdle() {
  const st = (THEME_RAW && THEME_RAW.states && THEME_RAW.states.idle) || {};
  const pool = Array.isArray(st.clipIds) ? st.clipIds : [];
  const pick = pool.find((id) => CLIPS[id]);
  return pick || Object.keys(CLIPS)[0] || null;
}

// ---- 帧清单 --------------------------------------------------------------
function frameUrl(id, idx) {
  const c = CLIPS[id];
  return assetUrl(`${c.dir}/f${String(idx).padStart(4, "0")}.${c.format || "webp"}`);
}

// ---- 位图缓存 ------------------------------------------------------------
// 用共享的 FrameCache：idle 与当前播放片段加入 protected 前缀，整段预载的循环
// 永不被淘汰（详见 P2-10）。
const frameCache = new FrameCache({
  keep: BITMAP_KEEP,
  decoder: async (id, idx) => {
    const blob = await fetch(frameUrl(id, idx), { cache: "no-store" }).then((r) => {
      if (!r.ok) throw new Error(`帧 ${id}/${idx} HTTP ${r.status}`);
      return r.blob();
    });
    // 直接解码到窗口物理像素（对齐 pet.py 的 QImage.scaled）
    return createImageBitmap(blob, { resizeWidth: bmpW, resizeHeight: bmpH, resizeQuality: "medium" });
  },
});

function loadBitmap(id, idx) { return frameCache.load(id, idx); }
function trimBitmaps() { frameCache.trim(); }
function dropOtherClips(keepIds) { frameCache.dropOtherClips(keepIds); }
function clearBitmaps() {
  frameCache.clear();
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
//
// 该片段（以及 idle 片段）被加入 FrameCache 的 protected 前缀，所以即便片段帧数
// 超过 BITMAP_KEEP，整段也始终常驻、循环不重解码（P2-10）。startClip 切走时再用
// dropOtherClips / unprotect 释放。
async function preloadClip(id, token) {
  frameCache.protect(id);
  frameCache.protect(IDLE_ID);
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
// 掩码按帧缓存，重建节流 MASK_REFRESH_MS。轮询 50ms 是因为窗口一旦 ignore，
// pointermove 就收不到了。
const MASK_DIV = 2;
const MASK_ALPHA_MIN = 8;
const MASK_REFRESH_MS = 150;
const HIT_POLL_MS = 50;
const MASK_CACHE_MAX = 600;

const maskCanvas = document.createElement("canvas");
const maskCtx = maskCanvas.getContext("2d", { willReadFrequently: true });
const maskCache = new Map();
let maskCur = null;
let maskKey = "";
let maskLastAt = 0;
let maskBuilds = 0, maskLastMs = 0, maskTotalMs = 0;

function maskFor(key, bmp) {
  let m = maskCache.get(key);
  if (m) { maskCache.delete(key); maskCache.set(key, m); return m; }
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
  if (!m) return true;
  const cx = Math.floor(lx * m.gw / Math.max(1, bmpW));
  const cy = Math.floor(ly * m.gh / Math.max(1, bmpH));
  if (cx < 0 || cy < 0 || cx >= m.gw || cy >= m.gh) return false;
  return m.alpha[cy * m.gw + cx] >= MASK_ALPHA_MIN;
}

let hover = false;
function setHover(v) {
  if (v === hover) return;
  hover = v;
  // hover 手型：窗口命中时不再穿透，光标由页面决定；未命中时窗口整体穿透，
  // 光标自然显示下层应用的光标。
  document.body.style.cursor = v ? "pointer" : "default";
}

async function decide(lx, ly, inside) {
  const hit = inside && hitAt(lx, ly);
  await setClickThrough(!hit || dragging);
  setHover(hit && !dragging && !hidden);
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
let paused = false;
let hidden = false;

// 切到不同画布比例的段时保持窗口宽度、右下角不动（与 pet.py 切段 resize 一致）
async function ensureCanvas(c) {
  if (CANVAS && sameAspect(CANVAS, c.canvas)) return;
  CANVAS = c.canvas.slice();
  const box = screenBox();
  const width = curWidth || Math.max(140, Math.round(box.width * 0.15));
  const height = displayHeight(width, CANVAS);
  try { await invoke("set_tier", { width, height, reposition: false, keepAnchor: true }); } catch (_) { /* ignore */ }
  await new Promise((r) => requestAnimationFrame(() => r()));
  const before = bmpW + "x" + bmpH;
  sizeCanvases();
  if (before !== bmpW + "x" + bmpH) {
    clearBitmaps();
    ctxB.clearRect(0, 0, canvasB.width, canvasB.height);
    hasPainted = false;
  }
}

async function startClip(id, { isIdle = false, resumeFrom = 0, loop = false, onEnd = null } = {}) {
  const c = CLIPS[id];
  if (!c) return;
  await ensureCanvas(c);
  if (!isIdle && state && state.isIdle && CLIPS[IDLE_ID]) {
    idleResumeAt = (state.idx + 1) % CLIPS[IDLE_ID].count;
  }
  const token = ++switchToken;
  const idx = Math.min(resumeFrom, c.count - 1);
  // 放开上一个动作片段的保护（idle 与即将播放的片段要保留）。
  if (playing && playing !== IDLE_ID && playing !== id) frameCache.unprotect(playing);
  dropOtherClips([IDLE_ID, id]);
  try {
    const bmp = await loadBitmap(id, idx);
    if (token !== switchToken) return;
    crossFade(bmp);
    updateMask(id, idx, bmp, true);
    logEvent(`clip_start id=${id} idle=${isIdle} idx=${idx} loop=${loop}`);
    state = {
      id, idx, isIdle, loop, onEnd,
      fps: c.fps || 24,
      next: performance.now() + 1000 / (c.fps || 24),
    };
    playing = id;
    reportState();
    preloadClip(id, token);
    if (!raf && !paused) raf = requestAnimationFrame(loopTick);
  } catch (e) {
    console.error("帧加载失败", e);
  }
}

function startIdle() {
  if (!IDLE_ID) return;
  startClip(IDLE_ID, { isIdle: true, resumeFrom: idleResumeAt, loop: true });
  playing = IDLE_ID;
  reportState();
}

function playAction(id) {
  if (id && id !== IDLE_ID) startClip(id, { isIdle: false, onEnd: () => startIdle() });
  else startIdle();
}

function loopTick(now) {
  if (paused) { raf = 0; return; }
  if (!state) { raf = 0; return; }
  const s = state;
  const interval = 1000 / s.fps;
  if (now >= s.next) {
    const n = CLIPS[s.id] ? CLIPS[s.id].count : 1;
    if (s.idx + 1 >= n && !s.loop) {
      state = null;
      raf = 0;
      if (typeof s.onEnd === "function") s.onEnd();
      else startIdle();
      return;
    }
    const nextIdx = (s.idx + 1) % n;
    const key = frameCache.key(s.id, nextIdx);
    if (frameCache.has(key)) {
      s.idx = nextIdx;
      const bmp = frameCache.get(key);
      drawCurrent(bmp);
      updateMask(s.id, s.idx, bmp);
      prefetch(s.id, s.idx);
      s.next = now + interval;
    } else {
      loadBitmap(s.id, nextIdx).catch(() => {});
      s.next = now + 8;
    }
  }
  raf = requestAnimationFrame(loopTick);
}

function playNextAction() {
  if (!ACTIONS.length) return;
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
function screenBox() {
  const s = window.screen;
  return { width: s.availWidth || s.width || 1280 };
}

async function applyTier(name, { reposition = false, persist = false } = {}) {
  const t = TIERS.find((x) => x.name === name) || TIERS[1];
  curTier = t.name;
  const width = Math.max(140, Math.round(screenBox().width * t.ratio));
  curWidth = width;
  const height = displayHeight(width, CANVAS || DEFAULT_CANVAS);
  await invoke("set_tier", { width, height, reposition, tier: t.name });
  if (persist) saveConfig({ tier: t.name });
  await new Promise((r) => requestAnimationFrame(() => r()));
  const before = bmpW + "x" + bmpH;
  sizeCanvases();
  if (before !== bmpW + "x" + bmpH) {
    clearBitmaps();
    ctxB.clearRect(0, 0, canvasB.width, canvasB.height);
    hasPainted = false;
  }
  if (state) {
    const s = state;
    loadBitmap(s.id, s.idx)
      .then((bmp) => { if (state === s) { drawCurrent(bmp); hasPainted = true; } })
      .catch(() => {});
    prefetch(s.id, s.idx);
  }
  return { width, height };
}

// ---- 原生菜单 ------------------------------------------------------------
window.__deskpetPick = (key) => {
  if (key.startsWith("tier:")) applyTier(key.slice(5), { persist: true });
  else if (key.startsWith("theme:")) switchThemeById(key.slice(6));
  else if (key === "hide") hideToTray();
  else if (key === "workshop") {
    invoke("open_workshop")
      .then((p) => logEvent("workshop_open path=" + p))
      .catch((e) => showHint("这个功能需要 Python 版工具", String(e)));
  } else if (key === "settings") {
    invoke("open_settings").catch((e) => showHint("设置窗打开失败", String(e)));
  } else if (key === "quit") quitApp();
};

function hideToTray() {
  hidden = true;
  paused = true;
  setHover(false);
  invoke("hide_main").catch(() => {});
  logEvent("tray_hide");
}

window.__deskpetPause = () => {
  hidden = true;
  paused = true;
  setHover(false);
};

window.__deskpetResume = () => {
  hidden = false;
  paused = false;
  if (!raf) raf = requestAnimationFrame(loopTick);
  logEvent("tray_show");
};

async function switchThemeById(tid) {
  if (!tid || tid === THEME_ID) return;
  try {
    // Rust 写 active 后会发 theme-changed，由事件统一热切换；这里只负责落盘
    await invoke("set_active_theme", { id: tid });
  } catch (e) {
    showNotice("切换主题失败", String(e));
  }
}

// ---- 右键菜单 ------------------------------------------------------------
document.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  clearReminder("右键");
  setHover(false);
  invoke("show_menu", { tier: curTier }).catch(() => {});
});

// ---- 拖动 + 单击 + 双击 ---------------------------------------------------
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
  const rule = rules.get("double_click");
  // 有绑定就走状态映射（池内顺序轮播）；零绑定回退旧的「按优先级轮播动作」，
  // 否则 v2 里 11 条规则永远存在，双击会变成什么都没发生。
  if (rule && rule.clipIds.length) setState("double_click", "双击");
  else if (curState === "idle") playNextAction();
});

// ---- 键盘 ----------------------------------------------------------------
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") quitApp();
});

// ---- 状态机（对齐 state_map.py）-------------------------------------------
const stateLog = [];
let rules = new Map();       // state -> rule
let curState = null;
let playing = null;          // 真实在播的 assetId（由 startClip 回填）
let cur = null;              // 当前占用 {clipId, priority, holdUntil, interruptible}
let silenced = false;
let reminders = 0;
let pendingReminder = null;  // {rule, source, at}
let deadlineAt = 0;
let ttl = null;              // {state, at}
let longIdleAt = 0;
let doneAt = 0;
let decisionTimeout = 300;   // seconds；attention 事件可覆盖
const lastPlay = new Map();

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

function parseTheme(text) {
  const raw = JSON.parse(text);
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("主题不是 JSON 对象");
  const states = raw.states;
  if (!states || typeof states !== "object") {
    throw new Error("缺 states（v1 state-map.json 的 rules[] 已不再支持）");
  }
  const out = new Map();
  for (const st of STATES) {
    const r = (states[st] && typeof states[st] === "object") ? states[st] : {};
    const wanted = Array.isArray(r.clipIds) ? r.clipIds.filter((c) => typeof c === "string") : [];
    const pool = wanted.filter((c) => CLIPS[c]);
    const missing = wanted.filter((c) => !CLIPS[c]);
    if (missing.length) stateDbg(`${st} 缺段 ${missing.join(",")}，已剔除`);
    if (!pool.length && !wanted.length) {
      const fb = Array.isArray(r.fallbackClipIds)
        ? r.fallbackClipIds.filter((c) => typeof c === "string" && CLIPS[c]) : [];
      if (fb.length) {
        pool.push(...fb);
        stateDbg(`${st} 主池为空，用备用段 ${fb.join(",")} 补位`);
      }
    }
    let rp = null;
    if (r.repeat && typeof r.repeat === "object") {
      rp = {
        mode: r.repeat.mode === "interval" ? "interval" : "once",
        intervalSec: num(r.repeat.intervalSec, 75),
        maxTimes: Math.max(1, Math.floor(num(r.repeat.maxTimes, 3))),
      };
      if (!REMINDER_STATES.has(st)) rp.mode = "once";  // repeat 只对提醒态有效
    }
    out.set(st, {
      state: st,
      clipIds: pool,
      fallbackClipIds: Array.isArray(r.fallbackClipIds)
        ? r.fallbackClipIds.filter((c) => typeof c === "string" && CLIPS[c]) : [],
      cursor: 0,
      clipId: pool[0] || "",
      afterSec: num(r.afterSec, 0),
      priority: num(r.priority, 20),
      minPlayMs: num(r.minPlayMs, 0),
      cooldownSec: num(r.cooldownSec, 0),
      loop: !!r.loop,
      interruptible: r.interruptible !== false,
      enabled: r.enabled !== false,
      repeat: rp,
    });
  }
  return {
    rules: out,
    longIdleSec: num(raw.longIdleSec, 600),
    awaitingUserSec: num(raw.awaitingUserSec, 30),
  };
}

function holding(rule = null) {
  const st = (rule && rule.state) || curState;
  return !!holdReminders && HOLD_STATES.has(st);
}

function clipMs(cid) {
  const c = CLIPS[cid] || { fps: 24, count: 1 };
  return (c.count / (c.fps || 24)) * 1000;
}

function nextPick(rule) {
  const pool = rule.clipIds;
  const idx = (rule.cursor || 0) % pool.length;
  rule.cursor = (idx + 1) % pool.length;
  return pool[idx];
}

function rejectPlay(rule, source, why) {
  stateDbg(`拒绝 ${source}→${rule.clipId || "∅"}：${why}`);
  return false;
}

function requestPlay(rule, source, force = false) {
  if (!rule.enabled) return rejectPlay(rule, source, "disabled");
  if (!rule.clipIds.length) {
    stateDbg(`命中 ${source}→∅（零绑定，保持当前画面）`);
    return false;
  }
  const pick = nextPick(rule);
  rule.clipId = pick;
  const now = performance.now();
  const last = lastPlay.has(pick) ? lastPlay.get(pick) : -1e12;
  if (!force) {
    if (now < last + rule.cooldownSec * 1000) {
      return rejectPlay(rule, source,
        `cooldown 剩余 ${((last + rule.cooldownSec * 1000 - now) / 1000).toFixed(1)}s`);
    }
    if (cur && now < cur.holdUntil) {
      if (!cur.interruptible) return rejectPlay(rule, source, `hold:${cur.clipId}`);
      if (rule.priority <= cur.priority) return rejectPlay(rule, source, `busy:${cur.clipId}`);
    }
  }
  lastPlay.set(pick, now);
  cur = {
    clipId: pick, priority: rule.priority, interruptible: rule.interruptible,
    holdUntil: now + Math.max(clipMs(pick), rule.minPlayMs),
  };
  // 只有单段池才让播放器无限循环；多段池每段只播一遍，播完由 clipFinished 推进游标，
  // 否则第二段永远轮不到（loop=true 时不会有结束回调）。
  const loopClip = rule.clipIds.length === 1 && (rule.loop || holding(rule));
  stateDbg(`命中 ${source}→${pick}（${(CLIPS[pick] || {}).name || ""}）`
    + `池 ${rule.clipIds.length} 段 游标=${rule.cursor} prio=${rule.priority} loop=${loopClip}`);
  playing = pick;  // 先占位，避免目标状态补齐逻辑在解码期间重复推游标
  reportState();
  startClip(pick, {
    isIdle: pick === IDLE_ID,
    loop: loopClip,
    onEnd: () => clipFinished(rule, source),
  });
  return true;
}

function clipFinished(rule, source) {
  cur = null;
  if (curState !== rule.state) return;
  const pool = rule.clipIds;
  // 状态级循环 / 提醒常驻：多段池按游标顺序接着播下一段，不换状态
  if (pool.length > 1 && (rule.loop || holding(rule))) {
    if (!requestPlay(rule, source + ":cycle", true)) startIdle();
    return;
  }
  if (holding(rule)) {
    // 单段池已在 requestPlay 里按 loop 播放；这里兜底循环被打断的情况
    requestPlay(rule, source + ":hold", true);
    return;
  }
  const rp = rule.repeat;
  if (!rp || rule.loop) {
    if (curState !== "idle") setState("idle", "clip-end");
    return;
  }
  const now = performance.now();
  if (rp.mode === "interval" && reminders + 1 < rp.maxTimes
      && (deadlineAt === 0 || now < deadlineAt)) {
    reminders += 1;
    startIdle();
    pendingReminder = { rule, source, at: now + rp.intervalSec * 1000 };
    stateDbg(`提醒 ${source} 第 ${reminders + 1}/${rp.maxTimes} 次，${rp.intervalSec}s 后`);
    return;
  }
  silenced = true;
  stateDbg(`${source} 提醒结束（${rp.mode}），转为静默待机`);
  startIdle();
}

function setState(name, why = "", { priority = null, timeoutSec = 0, force = false } = {}) {
  if (!STATES.includes(name)) { stateDbg(`未知状态 ${name} → 忽略`); return false; }
  if (name === curState) {
    if (timeoutSec) ttl = { state: name, at: performance.now() + timeoutSec * 1000 };
    return false;
  }
  const prev = curState;
  const prevSilenced = silenced;
  const prevReminders = reminders;
  const prevDeadline = deadlineAt;
  const now = performance.now();
  curState = name;
  silenced = false;
  reminders = 0;
  deadlineAt = 0;
  pendingReminder = null;
  ttl = timeoutSec ? { state: name, at: now + timeoutSec * 1000 } : null;
  if (name === "idle") longIdleAt = now;
  if (name === "done") doneAt = now;
  if (name === "needs_decision") deadlineAt = now + decisionTimeout * 1000;
  stateDbg(`切换 ${name}（${why}${prev ? "，来自 " + prev : ""}）`);
  const base = rules.get(name);
  if (!base) {
    curState = prev;
    stateDbg(`${name} 没有规则 → 保持 ${prev || "-"}`);
    return false;
  }
  let rule = base;
  if (priority !== null && Number.isFinite(Number(priority)) && Number(priority) !== base.priority) {
    rule = { ...base, priority: Number(priority) };
  }
  // 已在播池里任一段就续播，避免 idle 回位时把游标白推一格
  if (name === "idle" && playing && rule.clipIds.includes(playing)) {
    cur = { clipId: playing, priority: rule.priority, interruptible: rule.interruptible,
            holdUntil: now + clipMs(playing) };
    stateDbg(`已在播 ${playing}，续播`);
    reportState();
    return true;
  }
  const ok = requestPlay(rule, name, force);
  if (!ok && rule.clipIds.length) {
    // 被冷却/高优先级占用拒掉时回滚状态名，否则会出现
    // “state=streaming 但画面还在播 handup”的错位（提醒也会被误取消）
    curState = prev;
    silenced = prevSilenced;
    reminders = prevReminders;
    deadlineAt = prevDeadline;
    ttl = null;
    stateDbg(`${name} 未生效 → 保持 ${prev || "-"}`);
  }
  reportState();
  return ok;
}

function isReminder() {
  return REMINDER_STATES.has(curState) || (!!holdReminders && HOLD_STATES.has(curState));
}

function onUserInput() {
  if (isReminder()) {
    stateDbg(`用户有输入 → 清除提醒（原状态 ${curState}）`);
    setState("busy", "user_input", { force: true });
  }
}

function clearReminder(why) {
  if (isReminder()) {
    stateDbg(`手动操作（${why}）→ 清除提醒（原状态 ${curState}）`);
    setState("idle", "manual-clear", { force: true });
  }
  // 番茄钟：阶段结束的提醒被点掉后进入下一阶段（没有待开始的阶段就什么也不做）
  invoke("pomodoro_next").catch(() => { /* 非 Tauri 环境忽略 */ });
}

function external(name, extra) {
  const ex = extra || {};
  if (name === "attention") {
    decisionTimeout = num(ex.timeoutSec, 300);
    setState("needs_decision", `attention:${ex.kind || "?"}`);
    return;
  }
  if (name === "user_input") { onUserInput(); return; }
  // 番茄钟：专注/休息两键未绑定时回落 busy / idle（不把画面停在空中）
  if (name === "focus" || name === "focus_break") {
    const pool = ((rules.get(name) || {}).clipIds) || [];
    if (!pool.length) {
      const fb = name === "focus" ? "busy" : "idle";
      const fbPool = ((rules.get(fb) || {}).clipIds) || [];
      const target = fbPool.length ? fb : "idle";
      stateDbg(`外部状态 ${name} 未绑定 → 回落 ${target}`);
      name = target;
    }
  }
  if (!rules.has(name)) { stateDbg(`外部状态 ${name} 在主题里没有规则 → 忽略`); return; }
  setState(name, "external", {
    priority: ex.priority != null ? ex.priority : null,
    timeoutSec: num(ex.timeoutSec, 0),
  });
}

function stateTick() {
  const now = performance.now();
  if (ttl && now >= ttl.at && curState === ttl.state) {
    const st = ttl.state;
    ttl = null;
    if (holdReminders && HOLD_STATES.has(st)) {
      stateDbg(`${st} 超时但提醒常驻 → 保持`);
    } else {
      stateDbg(`${st} 超时 → 回落 idle`);
      cur = null;
      setState("idle", "state-timeout");
      return;
    }
  }
  if (pendingReminder && now >= pendingReminder.at && !silenced) {
    const p = pendingReminder;
    pendingReminder = null;
    if (curState === p.rule.state) requestPlay(p.rule, p.source + ":reminder");
  }
  if (curState === "idle" && now - longIdleAt > longIdleSec * 1000) {
    setState("long_idle_1", `空闲 ${Math.round(longIdleSec)}s`);
  }
  if (curState === "long_idle_1") {
    const r2 = rules.get("long_idle_2");
    const gate = (r2 && r2.afterSec) || 1800;
    if ((now - longIdleAt) >= gate * 1000 && r2 && r2.clipIds.length) {
      setState("long_idle_2", `空闲升档 ${Math.round(gate)}s`);
    }
  }
  if (curState === "done" && now - doneAt > awaitingUserSec * 1000 && !holding()) {
    setState("awaiting_user", `done 后 ${Math.round(awaitingUserSec)}s 无输入`);
  }
  if (curState === "needs_decision" && deadlineAt && now > deadlineAt && !holding()) {
    stateDbg("needs_decision 超时 → 降级为 idle");
    setState("idle", "decision-timeout");
    return;
  }
  // 画面补齐：目标状态的池里没有一段在播、且没有占用 → 重试
  // 用「是否在池内」判断，不用 rule.clipId，否则多段池会每 tick 白推游标
  if (!silenced && !pendingReminder && curState) {
    const rule = rules.get(curState);
    if (rule && rule.clipIds.length && !rule.clipIds.includes(playing)
        && now >= (cur ? cur.holdUntil : 0)) {
      requestPlay(rule, curState + ":reconcile");
    }
  }
}

// 外部事件入口：Rust 的 /state 与状态文件轮询都发 "state-in" 事件。
function applyExternalPayload(payload) {
  let p = payload;
  if (typeof p === "string") {
    const s = p.trim();
    if (!s) return;
    try { p = JSON.parse(s); } catch (_) { p = { state: s }; }
  }
  if (!p || typeof p !== "object") { stateDbg("外部状态不是 JSON 对象 → 忽略"); return; }
  const st = p.state;
  external(st, p);
  logEvent(`state_in recv state=${JSON.stringify(st)} prio=${p.priority} `
    + `ttl=${p.timeoutSec} src=${p.source || "?"}`);
}

window.__deskpetLog = () => stateLog.slice();
window.__deskpetState = (name, extra) => external(name, extra);

// ---- 主题热加载 -----------------------------------------------------------
let reloading = false;
async function reloadTheme(reason, opts) {
  if (reloading) return false;
  reloading = true;
  try { return await reloadThemeInner(reason, opts); }
  finally { reloading = false; }
}

async function reloadThemeInner(reason, { force = false } = {}) {
  let tid = "";
  try { tid = await invoke("active_theme"); } catch (_) { tid = ""; }
  const nextId = tid || THEME_ID;
  if (!nextId) return false;

  const switched = nextId !== THEME_ID;
  if (!switched) {
    // 同一主题：只在文本变化时重解析（设置面板保存 / 手改文件）
    let text = "";
    try { text = await loadThemeText(nextId); } catch (e) { stateDbg(`主题读取失败（保留上一版）: ${e.message}`); return false; }
    if (text === THEME_TEXT) return false;
    THEME_TEXT = text;
    THEME_RAW = JSON.parse(text);
    const parsed = parseTheme(text);
    rules = parsed.rules;
    longIdleSec = parsed.longIdleSec;
    awaitingUserSec = parsed.awaitingUserSec;
    const nextIdle = computeIdle();
    if (nextIdle && nextIdle !== IDLE_ID) {
      IDLE_ID = nextIdle;
      stateDbg(`待机段变更 → ${IDLE_ID}`);
    }
    rebuildActions();
    stateDbg(`${reason}：主题热更新（${rules.size} 行）`);
    const rule = rules.get(curState);
    if (rule && rule.clipIds.length && !rule.clipIds.includes(playing)) {
      requestPlay(rule, curState + ":reload");
    }
    return true;
  }

  // 切主题：重扫资产 → 重算 idle → 必要时 resize → 回 idle
  const count = await loadAssets();
  if (!count) throw new Error("animations/ 下没有可用动画资产");
  const text = await loadThemeText(nextId);
  THEME_ID = nextId;
  THEME_TEXT = text;
  THEME_RAW = JSON.parse(text);
  const parsed = parseTheme(text);
  rules = parsed.rules;
  longIdleSec = parsed.longIdleSec;
  awaitingUserSec = parsed.awaitingUserSec;
  IDLE_ID = computeIdle();
  if (!IDLE_ID) throw new Error("主题里的 idle 段不可用，且动作库为空");
  rebuildActions();
  stateDbg(`${reason}：载入主题 ${nextId}（${rules.size} 行，待机 ${IDLE_ID}）`);

  // 换装：先按新画布调整窗口，再重置播放器与状态机
  state = null;
  if (raf) { cancelAnimationFrame(raf); raf = 0; }
  switchToken++;
  await ensureCanvas(CLIPS[IDLE_ID]);
  curState = null;
  playing = null;
  cur = null;
  silenced = false;
  reminders = 0;
  pendingReminder = null;
  ttl = null;
  deadlineAt = 0;
  longIdleAt = performance.now();
  idleResumeAt = 0;
  // 先释放旧位图，再解码首帧；否则刚解码的 ImageBitmap 会被 clearBitmaps 一起 close
  clearBitmaps();
  sizeCanvases();
  dropOtherClips([]);
  const first = await loadBitmap(IDLE_ID, 0);
  hasPainted = false;
  drawCurrent(first);
  updateMask(IDLE_ID, 0, first, true);
  hasPainted = true;
  await setClickThrough(true);
  setState("idle", reason);
  idleResumeAt = 1 % CLIPS[IDLE_ID].count;
  return true;
}

async function pollTheme() {
  const now = performance.now();
  if (now - themeCheckAt < 1000) return;
  themeCheckAt = now;
  try {
    // reloadTheme 内部已做短路：同一主题且主题文本未变（THEME_TEXT）时直接返回、
    // 不再 JSON.parse；只有“活跃主题被切换”或“文本确实变了”才会重解析。这里每秒
    // 轮询一次的额外开销仅一次 active_theme + loadThemeText 请求。
    await reloadTheme("定时检查");
  } catch (e) { stateDbg(`主题检查失败: ${e.message}`); }
}

// ---- 提示面板 ------------------------------------------------------------
function showNotice(title, bodyHtml) {
  const el = document.getElementById("hint");
  if (!el) return;
  el.style.display = "block";
  // 标题来自系统路径/错误字符串，先转义；正文是受控模板（含 <code> 标签）保持原样。
  el.innerHTML = `<b>${esc(title)}</b><br><br>${bodyHtml}`;
}

function showHint(title, detail) {
  // detail 是素材目录路径或异常消息，必须转义后再塞进 <code>。
  showNotice(title, `素材目录：<br><code>${esc(detail)}</code><br><br>`
    + `把 <code>assets</code> 目录放到 exe 同级，或设置环境变量 `
    + `<code>DESKPET_ASSETS</code> 指向素材目录。`);
}

// ---- 启动 ----------------------------------------------------------------
let bmpW = 1, bmpH = 1;

window.__deskpetDebug = {
  stats: () => ({ bitmaps: frameCache.bitmaps.size, pending: frameCache.pending.size, state: state && state.id }),
  mask: () => ({ key: maskKey, gw: maskCur && maskCur.gw, gh: maskCur && maskCur.gh,
                 builds: maskBuilds, lastMs: maskLastMs, totalMs: maskTotalMs,
                 cache: maskCache.size, clickThrough }),
  hitAt: (x, y) => hitAt(x, y),
  simCursor: (x, y) => decide(x, y, x >= 0 && y >= 0 && x < bmpW && y < bmpH),
  setIgnore: (v) => setClickThrough(v),
  pause: () => { paused = true; if (raf) cancelAnimationFrame(raf); raf = 0; },
  resume: () => { if (!raf) { paused = false; raf = requestAnimationFrame(loopTick); } },
  state: () => ({ state: curState, playing, silenced, reminders, rules: [...rules.keys()] }),
  theme: () => THEME_ID,
};

(async () => {
  try {
    const info = await invoke("assets_info");
    if (info && (!info.exists || !info.themes || !info.animations)) {
      showHint("找不到 v2 素材目录", info.root);
      try { await invoke("set_tier", { width: 460, height: 300, reposition: true }); } catch (_) { /* ignore */ }
      await showWindow();
      return;
    }
    const cfg = (await invoke("get_config")) || {};
    holdReminders = cfg.remindHold !== false;   // 默认开：完成/出错/待决策常驻

    await loadAssets();
    THEMES = (await invoke("list_themes")) || [];
    let tid = "";
    try { tid = await invoke("active_theme"); } catch (_) { tid = ""; }
    tid = tid || (THEMES[0] && THEMES[0].id) || "";
    if (!tid) throw new Error("themes/ 下没有主题文件");
    THEME_ID = tid;
    THEME_TEXT = await loadThemeText(tid);
    THEME_RAW = JSON.parse(THEME_TEXT);
    const parsed = parseTheme(THEME_TEXT);
    rules = parsed.rules;
    longIdleSec = parsed.longIdleSec;
    awaitingUserSec = parsed.awaitingUserSec;
    IDLE_ID = computeIdle();
    if (!IDLE_ID) throw new Error("没有可用的待机段（检查 animations/ 与 themes 的 idle）");
    rebuildActions();

    CANVAS = CLIPS[IDLE_ID].canvas.slice();
    const tier = (cfg.tier && TIERS.some((t) => t.name === cfg.tier)) ? cfg.tier : DEFAULT_TIER;
    await applyTier(tier, { reposition: true });
    sizeCanvases();
    const first = await loadBitmap(IDLE_ID, 0);  // 首帧解码完再显示窗口
    drawCurrent(first);
    updateMask(IDLE_ID, 0, first, true);
    hasPainted = true;
    await setClickThrough(true);
    await showWindow();
    setInterval(pollCursor, HIT_POLL_MS);
    setInterval(pollTheme, 1000);
    setInterval(stateTick, 1000);
    // 不要预置 playing，否则 setState 会走“续播”分支而跳过 startClip，
    // 导致首帧画了但没有真正启动待机动画循环
    setState("idle", "启动");
    idleResumeAt = 1 % CLIPS[IDLE_ID].count;
    prefetch(IDLE_ID, 0);
    // 循环由 startClip 在首帧解码完成后启动，这里不要再抢着起 rAF
    logEvent(`boot theme=${THEME_ID} assets=${Object.keys(CLIPS).length} idle=${IDLE_ID} hold=${holdReminders}`);

    // 外部状态入口（Rust 侧 /state 与状态文件轮询）
    const ev = T() && T().event;
    if (ev && typeof ev.listen === "function") {
      ev.listen("state-in", (e) => applyExternalPayload(e.payload)).catch(() => {});
      ev.listen("config-changed", (e) => {
        const c = (e && e.payload) || {};
        holdReminders = c.remindHold !== false;
        if (c.tier && c.tier !== curTier && TIERS.some((t) => t.name === c.tier)) {
          applyTier(c.tier, { reposition: false });
        }
        const rule = rules.get(curState);
        if (rule && rule.clipIds.length) requestPlay(rule, "config:hold");
        logEvent(`config_changed remindHold=${holdReminders}`);
      }).catch(() => {});
      ev.listen("theme-changed", () => reloadTheme("外部切换", { force: true }).catch(() => {})).catch(() => {});
    }
  } catch (e) {
    console.error("素材加载失败", e);
    showHint("素材加载失败", String(e && e.message ? e.message : e));
    await showWindow();
  }
})();
