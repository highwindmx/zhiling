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
async function loadClips() {
  const res = await fetch("assets/clips.json", { cache: "no-store" });
  if (!res.ok) throw new Error("clips.json HTTP " + res.status);
  const raw = await res.json();
  CLIPS = raw.clips || {};
  if (raw.canvas && raw.canvas.length === 2) {
    CANVAS = [raw.canvas[0], raw.canvas[1]];
  }
  const ids = Object.keys(CLIPS);
  IDLE_ID = ids.find((k) => CLIPS[k].type === "idle") || ids[0];
  ACTIONS = ids.filter((k) => CLIPS[k].type !== "idle")
    .sort((a, b) => (CLIPS[a].priority ?? 99) - (CLIPS[b].priority ?? 99));
  if (!IDLE_ID) throw new Error("clips.json 里没有 idle 段");
}

function frameUrl(id, idx) {
  const c = CLIPS[id];
  return `assets/${c.dir}/f${String(idx).padStart(4, "0")}.${c.format || "webp"}`;
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

async function startClip(id, { isIdle = false, resumeFrom = 0 } = {}) {
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
    logEvent(`clip_start id=${id} idle=${isIdle} idx=${idx}`);
    state = {
      id, idx, isIdle,
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
  startClip(IDLE_ID, { isIdle: true, resumeFrom: idleResumeAt });
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
    if (s.idx + 1 >= n && !s.isIdle) {
      // 动作播完：先摘掉状态并停掉本轮 rAF，避免异步切段期间重复触发
      state = null;
      raf = 0;
      startIdle();
      return;
    }
    const nextIdx = (s.idx + 1) % n;
    const key = `${s.id}/${nextIdx}`;
    if (bitmaps.has(key)) {
      s.idx = nextIdx;
      drawCurrent(touch(bitmaps, key));
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
  down = { x: e.screenX, y: e.screenY };
  dragging = false;
});
window.addEventListener("pointermove", (e) => {
  if (!down || dragging) return;
  if (Math.hypot(e.screenX - down.x, e.screenY - down.y) < 4) return;
  dragging = true;
  down = null;
  startNativeDrag().finally(() => { dragging = false; });
});
window.addEventListener("pointerup", () => { down = null; });
window.addEventListener("dblclick", () => { logEvent("dblclick"); playNextAction(); });

// ---- 键盘 ----------------------------------------------------------------
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") quitApp();
});

// ---- 启动 ----------------------------------------------------------------
// 调试钩子（仅用于测量/排障，不影响正常逻辑）
let paused = false;
window.__deskpetDebug = {
  stats: () => ({ bitmaps: bitmaps.size, pending: pending.size, state: state && state.id }),
  pause: () => { paused = true; if (raf) cancelAnimationFrame(raf); raf = 0; },
  resume: () => { if (!raf) { paused = false; raf = requestAnimationFrame(loop); } },
};

(async () => {
  try {
    await loadClips();
    await applyTier(DEFAULT_TIER, { reposition: true });
    sizeCanvases();
    const first = await loadBitmap(IDLE_ID, 0);  // 首帧解码完再显示窗口
    drawCurrent(first);
    hasPainted = true;
    await showWindow();
    state = { id: IDLE_ID, idx: 0, isIdle: true, fps: CLIPS[IDLE_ID].fps || 24,
              next: performance.now() + 1000 / (CLIPS[IDLE_ID].fps || 24) };
    prefetch(IDLE_ID, 0);
    raf = requestAnimationFrame(loop);
  } catch (e) {
    console.error("素材加载失败", e);
    await showWindow();
  }
})();
