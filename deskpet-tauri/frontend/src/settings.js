// 设置面板：状态 ↔ 动画配对、优先级排序、动作池与单段预览。
// 数据直接读写 assets/themes/*.json（Rust 命令），保存后主窗口 1s 内热更新。
// 约定与 pet.py 的 SettingsDialog 一致：
//   - 左栏顺序 = 优先级，越靠上 priority 越大；保存时按 (n-1-i)*10 均分。
//   - 一个状态可绑定 0~N 段；0 段不是错误，只是“到达该状态时保持当前画面”。
//   - 单段池才会循环；多段池按顺序轮播（对应 state_map 的游标）。

import { STATES, STATE_CN, assetUrl, T, invoke, esc, canvasOf, FrameCache, PREVIEW_KEEP, DEFAULT_DSH_PORT, DEFAULT_STATE_PORT, DEFAULT_CANVAS } from "./shared.js";

const DEFAULT_ORDER = STATES.slice();

let THEMES = [];
let THEME_ID = "";
let THEME = null;        // 原始主题对象（保存时保留未知字段）
let ASSETS = {};         // assetId -> meta
let ROWS = [];           // [{key, raw, clipIds, defIdx}]  （绑定语义：动作池为空即不绑定）
let SEL = "";            // 当前编辑状态 key
let PACKS_SAVED = "";    // 已保存的素材目录覆盖值（"" = 走默认）
let PACKS_VALUE = "";    // 当前待保存的覆盖值（浏览… / 恢复默认 改它）
let REMIND_HOLD = true;  // 提醒态常驻（停在 done / error / needs_decision 不回 idle）

const $ = (id) => document.getElementById(id);

function clipMeta(id) {
  return ASSETS[id] || null;
}
function clipName(id) {
  const m = clipMeta(id);
  return m ? (m.name || id) : `${id}（缺失）`;
}
function clipDir(id) {
  const m = clipMeta(id);
  return (m && m.dir) || `animations/${id}`;
}
function clipFirstFrame(id) {
  const m = clipMeta(id);
  if (!m) return null;
  const fmt = String(m.format || "webp").toLowerCase();
  return assetUrl(`${clipDir(id)}/f0000.${fmt}`);
}

function rowOf(key) { return ROWS.find((r) => r.key === key); }
function selRow() { return rowOf(SEL); }
function stateOf(key) { return (THEME && THEME.states && THEME.states[key]) || {}; }

function setStatus(msg, cls = "") {
  const el = $("status");
  el.textContent = msg;
  el.className = cls;
}

// ---- 载入 ----------------------------------------------------------------
async function refreshThemes(selectId) {
  THEMES = (await invoke("list_themes")) || [];
  const sel = $("themeSel");
  sel.innerHTML = THEMES.map((t) =>
    `<option value="${esc(t.id)}">${esc(t.name)}${t.active ? " · 使用中" : ""}</option>`).join("");
  if (selectId && THEMES.some((t) => t.id === selectId)) sel.value = selectId;
  else {
    // 默认落在「使用中」主题上（与 Python 设置窗一致），没有 active 才退第一个
    const act = THEMES.find((t) => t.active);
    sel.value = (act || THEMES[0] || {}).id || "";
  }
  return sel.value;
}

async function loadTheme(id) {
  stopPreview();
  THEME_ID = id;
  THEME = await invoke("read_theme", { id });
  if (!THEME || typeof THEME !== "object") THEME = { version: 2, id, name: id, states: {} };
  THEME.states = (THEME.states && typeof THEME.states === "object") ? THEME.states : {};
  ROWS = STATES.map((key, defIdx) => {
    const st = stateOf(key);
    return {
      key, raw: st, defIdx,
      clipIds: Array.isArray(st.clipIds) ? st.clipIds.filter((c) => typeof c === "string") : [],
      priority: Number(st.priority) || 0,
    };
  });
  ROWS.sort((a, b) => (b.priority - a.priority) || (a.defIdx - b.defIdx));
  SEL = (ROWS.find((r) => r.clipIds.length) || ROWS[ROWS.length - 1] || {}).key || "";
  // 选中状态后自动预览动作池第一段（对齐 Python 设置窗的「选中即预览」）
  const selRow = rowOf(SEL);
  SEL_CLIP = (selRow && selRow.clipIds[0]) || "";
  renderAll();
  if (SEL_CLIP) loadPreview(SEL_CLIP);
}

async function boot() {
  const cfg = (await invoke("get_config")) || {};
  PACKS_SAVED = cfg.packsDir || "";
  PACKS_VALUE = PACKS_SAVED;
  $("packsDir").value = PACKS_VALUE;
  const pd = cfg.pomodoro || {};
  $("pomoEnabled").checked = pd.enabled !== false;
  $("pomoWork").value = Number(pd.workMin) || 25;
  $("pomoBreak").value = Number(pd.breakMin) || 5;
  REMIND_HOLD = cfg.remindHold !== false;
  $("remindHold").checked = REMIND_HOLD;
  ASSETS = {};
  for (const m of (await invoke("list_assets")) || []) {
    if (m && (m.assetId || m.id)) ASSETS[m.assetId || m.id] = m;
  }
  const tid = await refreshThemes();
  await loadTheme(tid);
  await fitSettingsWindow();      // 打开时按内容自适应高度（不改变宽/内容布局）
  refreshConn().catch(() => {});
}

// ---- 分页 / 连接页 ---------------------------------------------------------

function switchTab(id) {
  for (const sec of ["pane-states", "pane-alert"]) {
    const el = $(sec);
    if (el) el.hidden = sec !== id;
  }
  for (const b of $("tabs").querySelectorAll(".tab")) {
    b.classList.toggle("on", b.dataset.tab === id);
  }
}

async function refreshConn() {
  const info = await invoke("assets_info");
  if (info) {
    // 输入框是可编辑的语义值（空 = 默认顺序），这里只把「实际生效目录」放进 title，不回填
    const srcCn = info.source === "env" ? "环境变量"
      : info.source === "config" ? "配置" : "默认";
    $("packsDir").title = `实际生效：${info.root}（来源：${srcCn}）。`
      + "可直接输入或粘贴绝对路径；支持 %LOCALAPPDATA% / %APPDATA% / %USERPROFILE% / %TEMP% 变量（保存时展开）；留空 = 按默认顺序解析。";
    const tag = $("packsSrc");
    tag.textContent = srcCn;
    tag.title = `来源：${srcCn}${info.source === "config" && !PACKS_VALUE ? "（已清除覆盖，按默认解析）" : ""} · 生效：${info.root}`;
    const missing = [];
    if (!info.exists) missing.push("目录不存在");
    if (!info.themes) missing.push("themes 缺失");
    if (!info.animations) missing.push("animations 缺失");
    const st = $("packsState");
    st.className = "v " + (missing.length ? "err" : "ok");
    st.textContent = missing.length
      ? `解码 Media Foundation · 校验失败（${missing.join("、")}）`
      : "解码 Media Foundation · 校验通过";
    st.title = st.textContent;
  }
  const si = await invoke("state_info");
  if (si) {
    $("stateUrl").textContent = si.port ? `127.0.0.1:${si.port}` : "已关闭（DESKPET_STATE_PORT=0）";
    $("stateUrl").title = si.port ? `POST/GET ${si.url}` : "HTTP 入口已关闭";
    $("stateLast").textContent = si.lastState ? `最近 ${si.lastState}` : "—";
    const pi = $("statePort");
    if (pi && !pi.matches(":focus")) pi.value = si.port ?? 0;
  }
  const ps = await invoke("pomodoro_status");
  if (ps) $("pomoNow").textContent = ps.text || "—";
  const di = await invoke("dsh_info").catch(() => null);
  if (di) renderDsh(di);
}

/// DSH 链路状态灯：绿=已连上广播；黄=端口在监听但没连上；红=插件没起。
function renderDsh(d) {
  const on = !!d.connected;
  const listen = !!d.listening;
  $("dshDot").className = "dot " + (on ? "on" : listen ? "warn" : "off");
  $("dshState").textContent = on
    ? "已连接（跟随 DSH 状态）"
    : listen
      ? "端口在监听，等待连接"
      : "未连接（DSH 插件未启动？）";
  $("dshUrl").textContent = `${d.url}（端口 ${d.port}）`;
  $("dshLast").textContent = d.lastEvent || "—";
  $("dshMsg").textContent = d.lastError ? `最近错误：${d.lastError}` : d.note || "—";
  const inp = $("dshPort");
  if (inp && !inp.matches(":focus") && d.port) inp.value = d.port;
}

// ---- 渲染 ----------------------------------------------------------------
function renderAll() {
  renderStates();
  renderRight();
}

function renderStates() {
  const list = $("stateList");
  list.innerHTML = ROWS.map((r, i) => {
    const names = r.clipIds.length
      ? r.clipIds.map((c) => clipName(c)).join(" → ")
      : "未绑定（保持当前画面）";
    const thumb = r.clipIds[0] ? clipFirstFrame(r.clipIds[0]) : null;
    const img = thumb
      ? `<img class="thumb" src="${esc(thumb)}" draggable="false" alt="" />`
      : `<div class="thumb"></div>`;
    return `<div class="state${r.key === SEL ? " sel" : ""}"
      data-idx="${i}" data-key="${esc(r.key)}" draggable="true" title="拖拽调整优先级（越靠上越高）">
      <span class="handle">⠿</span>
      ${img}
      <div class="mid">
        <div class="nm">${esc(STATE_CN[r.key] || r.key)}</div>
        <div class="sub">${esc(names)}</div>
      </div>
      <div class="ops">
        <button class="mini ghost" data-act="up" title="上移" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="mini ghost" data-act="down" title="下移" ${i === ROWS.length - 1 ? "disabled" : ""}>↓</button>
      </div>
    </div>`;
  }).join("");
}

function renderRight() {
  const r = selRow();
  if (!r) return;
  $("stateTitle").textContent = `${STATE_CN[r.key] || r.key}`;
  $("poolCount").textContent = `${r.clipIds.length} 段`;

  const pool = $("pool");
  if (!r.clipIds.length) {
    pool.innerHTML = `<div class="empty">还没有绑定动画。点右侧「+ 动作库」，或「新建动画」。</div>`;
  } else {
    pool.innerHTML = r.clipIds.map((cid, i) => {
      const m = clipMeta(cid);
      const thumb = m ? clipFirstFrame(cid) : null;
      const img = thumb ? `<img class="thumb" src="${esc(thumb)}" alt="" />` : `<div class="thumb"></div>`;
      const sub = m
        ? `${canvasOf(m).join("×")} · ${m.fps || 24}fps · ${m.count || "?"} 帧`
        : "素材缺失";
      return `<div class="clip${cid === SEL_CLIP ? " sel" : ""}" data-idx="${i}" data-cid="${esc(cid)}"
        title="${esc(sub)}">
        <span class="idx">${i + 1}</span>${img}
        <span class="nm">${esc(clipName(cid))}</span>
        <button class="mini ghost" data-act="preview" title="预览这一段">▶</button>
        <button class="mini ghost" data-act="pup" title="上移" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="mini ghost" data-act="pdown" title="下移" ${i === r.clipIds.length - 1 ? "disabled" : ""}>↓</button>
        <button class="mini ghost" data-act="del" title="移除绑定">×</button>
      </div>`;
    }).join("");
  }
  $("pvWorkshop").disabled = !SEL_CLIP;
  if (!SEL_CLIP) setPreviewInfo("选择动作池里的一段");
}

// ---- 左栏交互 --------------------------------------------------------------
let dragFrom = -1;

function bindStateList() {
  const list = $("stateList");
  list.addEventListener("click", (e) => {
    const rowEl = e.target.closest(".state");
    if (!rowEl) return;
    const key = rowEl.dataset.key;
    const act = e.target.dataset && e.target.dataset.act;
    if (act === "up" || act === "down") {
      const i = ROWS.findIndex((r) => r.key === key);
      const j = act === "up" ? i - 1 : i + 1;
      if (j < 0 || j >= ROWS.length) return;
      [ROWS[i], ROWS[j]] = [ROWS[j], ROWS[i]];
      SEL = key;
      markDirty();
      renderAll();
      return;
    }
    SEL = key;
    const row = rowOf(key);
    SEL_CLIP = (row && row.clipIds[0]) || "";
    renderStates();
    renderRight();
    if (SEL_CLIP) loadPreview(SEL_CLIP);
    else {
      pv.clip = "";
      pv.frames = 0;
      stopPreview();
      setFrameLabel();
      const cv = $("pvCanvas");
      previewCtx().clearRect(0, 0, cv.width, cv.height);
    }
  });

  list.addEventListener("dragstart", (e) => {
    const rowEl = e.target.closest(".state");
    if (!rowEl) return;
    dragFrom = Number(rowEl.dataset.idx);
    rowEl.classList.add("drag");
    e.dataTransfer.effectAllowed = "move";
  });
  list.addEventListener("dragover", (e) => {
    const rowEl = e.target.closest(".state");
    if (!rowEl) return;
    e.preventDefault();
    list.querySelectorAll(".over").forEach((x) => x.classList.remove("over"));
    if (Number(rowEl.dataset.idx) !== dragFrom) rowEl.classList.add("over");
  });
  list.addEventListener("drop", (e) => {
    const rowEl = e.target.closest(".state");
    if (!rowEl || dragFrom < 0) return;
    e.preventDefault();
    const to = Number(rowEl.dataset.idx);
    if (to !== dragFrom) {
      const [moved] = ROWS.splice(dragFrom, 1);
      ROWS.splice(to, 0, moved);
      SEL = moved.key;
      markDirty();
    }
    dragFrom = -1;
    renderAll();
  });
  list.addEventListener("dragend", () => {
    dragFrom = -1;
    list.querySelectorAll(".drag,.over").forEach((x) => x.classList.remove("drag", "over"));
  });
}

// ---- 右栏交互 --------------------------------------------------------------
let SEL_CLIP = "";

function bindRight() {
  const pool = $("pool");
  pool.addEventListener("click", (e) => {
    const el = e.target.closest(".clip");
    if (!el) return;
    const cid = el.dataset.cid;
    const act = e.target.dataset && e.target.dataset.act;
    const r = selRow();
    if (!r) return;
    if (act === "del") {
      r.clipIds = r.clipIds.filter((x) => x !== cid);
      if (SEL_CLIP === cid) { SEL_CLIP = ""; stopPreview(); }
      markDirty();
      renderStates();
      renderRight();
      return;
    }
    if (act === "pup" || act === "pdown") {
      const i = r.clipIds.indexOf(cid);
      const j = act === "pup" ? i - 1 : i + 1;
      if (j < 0 || j >= r.clipIds.length) return;
      [r.clipIds[i], r.clipIds[j]] = [r.clipIds[j], r.clipIds[i]];
      markDirty();
      renderStates();
      renderRight();
      return;
    }
    SEL_CLIP = cid;
    renderRight();
    loadPreview(cid);
  });
}

// ---- 预览播放器 ------------------------------------------------------------
const pv = {
  clip: "", frames: 0, fps: 24, canvas: DEFAULT_CANVAS.slice(),
  idx: 0, playing: false, raf: 0, next: 0,
};
// 预览位图走共享的 FrameCache（与主播放器同构的 LRU + close() 语义）。
const pvCache = new FrameCache({
  keep: PREVIEW_KEEP,
  decoder: async (clip, i) => {
    const m = clipMeta(clip);
    const fmt = String((m && m.format) || "webp");
    const blob = await fetch(assetUrl(`${clipDir(clip)}/f${String(i).padStart(4, "0")}.${fmt}`), { cache: "no-store" })
      .then((r) => { if (!r.ok) throw new Error(`帧 ${clip}/${i} HTTP ${r.status}`); return r.blob(); });
    return createImageBitmap(blob);
  },
});

function previewCtx() {
  return $("pvCanvas").getContext("2d");
}

function sizePreviewCanvas(meta) {
  const cv = $("pvCanvas");
  const [w, h] = canvasOf(meta);
  const maxW = 520, maxH = 290;
  const k = Math.min(maxW / w, maxH / h, 1);
  cv.width = Math.max(1, Math.round(w * k));
  cv.height = Math.max(1, Math.round(h * k));
}

async function pvBitmap(clip, i) {
  return pvCache.load(clip, i);
}

function setPreviewInfo(text) {
  $("pvInfo").textContent = text;
}
function setFrameLabel() {
  $("pvFrame").textContent = pv.frames ? `第 ${pv.idx + 1} / ${pv.frames} 帧` : "—";
}

function stopPreview() {
  pv.playing = false;
  if (pv.raf) cancelAnimationFrame(pv.raf);
  pv.raf = 0;
  $("pvPlay").textContent = "播放";
  $("pvPlay").disabled = !pv.clip;
}

async function loadPreview(clip) {
  stopPreview();
  const m = clipMeta(clip);
  pv.clip = clip;
  pv.frames = Number(m && m.count) || 0;
  pv.fps = Number(m && m.fps) || 24;
  pv.canvas = canvasOf(m);
  pv.idx = 0;
  sizePreviewCanvas(m);
  $("pvPlay").disabled = !m;
  $("pvWorkshop").disabled = !m;
  if (!m) { setPreviewInfo("素材缺失"); setFrameLabel(); return; }
  setPreviewInfo(`${m.name || clip} · ${pv.canvas.join("×")} · ${pv.fps}fps · ${pv.frames} 帧`);
  try {
    const bmp = await pvBitmap(clip, 0);
    if (pv.clip !== clip) return;
    drawPreview(bmp);
    setFrameLabel();
    pvLoop(performance.now());
    pv.playing = true;
    $("pvPlay").textContent = "暂停";
    pv.raf = requestAnimationFrame(pvLoop);
  } catch (e) {
    setPreviewInfo(`首帧加载失败：${e.message}`);
  }
}

function drawPreview(bmp) {
  const cv = $("pvCanvas");
  const ctx = previewCtx();
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.drawImage(bmp, 0, 0, cv.width, cv.height);
}

function pvLoop(now) {
  if (!pv.playing || !pv.clip) { pv.raf = 0; return; }
  const interval = 1000 / (pv.fps || 24);
  if (now >= pv.next) {
    const i = (pv.idx + 1) % Math.max(1, pv.frames);
    const clip = pv.clip;
    pvBitmap(clip, i).then((bmp) => {
      if (pv.clip !== clip) return;
      pv.idx = i;
      drawPreview(bmp);
      setFrameLabel();
    }).catch(() => {});
    pv.next = now + interval;
    pv.idx = i;
  }
  pv.raf = requestAnimationFrame(pvLoop);
}

// ---- 动作库弹窗 ------------------------------------------------------------
function openLib() {
  const r = selRow();
  if (!r) return;
  const used = new Set(r.clipIds);
  const ids = Object.keys(ASSETS).sort((a, b) =>
    (STATE_CN[ASSETS[a].type] || ASSETS[a].type || "").localeCompare(STATE_CN[ASSETS[b].type] || ASSETS[b].type || "")
    || a.localeCompare(b));
  const avail = ids.filter((id) => !used.has(id));
  $("libHint").textContent = avail.length ? `可添加 ${avail.length} 段` : "动作库里的段都已绑定";
  $("libList").innerHTML = avail.map((id) => {
    const m = ASSETS[id];
    const thumb = clipFirstFrame(id);
    return `<label><input type="checkbox" value="${esc(id)}" />
      ${thumb ? `<img class="thumb" src="${esc(thumb)}" alt="" />` : `<div class="thumb"></div>`}
      <span class="nm">${esc(m.name || id)}</span></label>`;
  }).join("") || `<div class="empty">动作库为空：请先在素材工坊生成动画。</div>`;
  $("libModal").classList.add("show");
}
function closeLib() { $("libModal").classList.remove("show"); }
function addFromLib() {
  const r = selRow();
  if (!r) return;
  const picked = [...$("libList").querySelectorAll("input[type=checkbox]:checked")].map((x) => x.value);
  if (!picked.length) { closeLib(); return; }
  r.clipIds.push(...picked);
  if (!SEL_CLIP) SEL_CLIP = picked[0];
  closeLib();
  markDirty();
  renderStates();
  renderRight();
  if (SEL_CLIP) loadPreview(SEL_CLIP);
}

function clampInt(v, lo, hi, dflt) {
  const n = Math.round(Number(v));
  if (!Number.isFinite(n)) return dflt;
  return Math.min(hi, Math.max(lo, n));
}

// ---- 素材目录：可编辑 + 环境变量展开 ----------------------------------------
// Rust 侧 set_packs_dir 只接受真实路径（不展开 %VAR%），所以展开在前端做：
// %LOCALAPPDATA% 从 assets_info.candidates 里的默认候选目录反推（总能拿到），
// 其余常见变量尽量走 Tauri path API；拿不到的变量直接报错、不写入配置。
let ENV_MAP = null;

function stripSlash(p) {
  return p.length > 3 ? p.replace(/[\\/]+$/, "") : p;
}

async function tauriPath(fn) {
  try {
    const mod = (T() && T().path) || null;
    if (!mod || typeof mod[fn] !== "function") return "";
    const v = await mod[fn]();
    return stripSlash(String(v || ""));
  } catch (_) {
    return "";
  }
}

async function envMap() {
  if (ENV_MAP) return ENV_MAP;
  const map = {};
  const info = await invoke("assets_info").catch(() => null);
  // 只认 %LOCALAPPDATA%\DeskPet\packs 形态的候选，避免把用户自定义的 D:\DeskPet\packs 误当成 LOCALAPPDATA
  const local = ((info && info.candidates) || []).find((p) => /\\AppData\\Local\\DeskPet\\packs$/i.test(p));
  if (local) {
    const base = stripSlash(local.replace(/\\DeskPet\\packs$/i, ""));
    map.LOCALAPPDATA = base;
    const m = base.match(/^(.*)\\AppData\\Local$/i);
    if (m) {
      map.USERPROFILE = m[1];
      map.APPDATA = `${m[1]}\\AppData\\Roaming`;
    }
  }
  for (const [name, fn] of [["LOCALAPPDATA", "localDataDir"], ["APPDATA", "roamingAppDataDir"],
    ["USERPROFILE", "homeDir"], ["TEMP", "tempDir"]]) {
    if (!map[name]) {
      const v = await tauriPath(fn);
      if (v) map[name] = v;
    }
  }
  if (map.TEMP) map.TMP = map.TEMP;
  ENV_MAP = map;
  return map;
}

/// 展开 %VAR%：返回 {value} 或 {error}（未知变量 → 报错且不落盘）。
async function expandPacksInput(raw) {
  const s = String(raw || "").trim();
  if (!s) return { value: "" };
  if (!/%[^%]+%/.test(s)) return { value: stripSlash(s) };
  const map = await envMap();
  const unknown = [];
  const out = s.replace(/%([^%]+)%/g, (m, name) => {
    const key = String(name).trim().toUpperCase();
    const v = map[key];
    if (!v) { unknown.push(name); return m; }
    return v;
  });
  if (unknown.length) {
    const known = Object.keys(map).filter((k) => map[k]).join(" / ");
    return { error: `⚠ 无法展开环境变量 %${unknown[0]}%（可用：${known || "无"}）` };
  }
  return { value: stripSlash(out.replace(/[\\/]{2,}/g, "\\")) };
}

function showPacksMsg(text) {
  const msg = $("packsMsg");
  msg.textContent = text;
  msg.title = text;
  msg.hidden = false;
  $("packsState").hidden = true;   // 同一行只占一个位置，避免多挤出一行
}

function clearPacksMsg() {
  $("packsMsg").hidden = true;
  $("packsState").hidden = false;
}

/// 把 Rust 侧的目录错误翻译成行内提示（不存在 / 结构不完整 / 其它）。
function packsErrorText(e) {
  const s = String(e || "");
  if (/找不到素材（packs）目录/.test(s)) return "⚠ 目录不存在，运行时将回落到默认位置";
  if (/缺少 themes|没有任何主题|缺少 animations/.test(s)) return "⚠ 目录存在，但不是有效素材目录（缺少 themes/ 或主题 JSON）";
  return `⚠ ${s.split("\n")[0] || "素材目录无效"}`;
}

// ---- 设置窗自适应高度 ------------------------------------------------------
// 打开后按「内容自然高度 + 4px 边距」调一次窗口尺寸：
// 下限 600（与 Rust 侧 min_inner_size 一致），上限 = 可用屏高 85%。
// 内容超过上限时窗口不继续长高，由内部列表/表单滚动。
const WIN_MIN_H = 600;

function listNaturalHeight() {
  const list = $("stateList");
  const last = list.lastElementChild;
  if (!last) return list.clientHeight;
  const padB = parseFloat(getComputedStyle(list).paddingBottom) || 0;
  return Math.round(last.getBoundingClientRect().bottom - list.getBoundingClientRect().top
    + list.scrollTop + padB);
}

function childrenNaturalHeight(el) {
  const kids = [...el.children];
  if (!kids.length) return 0;
  const cs = getComputedStyle(el);
  const gap = parseFloat(cs.rowGap || cs.gap) || 0;
  return Math.round(kids.reduce((s, k) => s + k.offsetHeight, 0) + gap * (kids.length - 1));
}

function contentNeedHeight() {
  const headerH = document.querySelector("header").offsetHeight;
  const pane = $("pane-states");
  const ps = getComputedStyle(pane);
  const panePad = (parseFloat(ps.paddingTop) || 0) + (parseFloat(ps.paddingBottom) || 0);
  const paneGap = parseFloat(ps.rowGap || ps.gap) || 0;
  const topH = pane.querySelector(".state-top").offsetHeight;
  const toolsH = pane.querySelector(".tools").offsetHeight;

  const [left, right] = pane.querySelector(".grid").querySelectorAll(".col");
  const leftNeed = left.querySelector(".col-h").offsetHeight + listNaturalHeight() + 2;

  const card = right.querySelector(".preview-wrap");
  const cs = getComputedStyle(card);
  const h3 = card.querySelector("h3");
  const cardPad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0) + 2;
  const cardGap = parseFloat(cs.rowGap || cs.gap) || 0;
  const cardNeed = cardPad + h3.offsetHeight + (parseFloat(getComputedStyle(h3).marginBottom) || 0)
    + cardGap + 300 + cardGap + card.querySelector(".pv-bar").offsetHeight;
  const rightPad = (parseFloat(getComputedStyle(right).paddingTop) || 0)
    + (parseFloat(getComputedStyle(right).paddingBottom) || 0);
  const rightNeed = right.querySelector(".pool-h").offsetHeight + 2 + rightPad + cardNeed;

  const statesNeed = panePad + topH + paneGap + Math.max(leftNeed, rightNeed) + paneGap + toolsH;

  // 提醒页平时 hidden，量高度前同步显示（同一帧内恢复，不会闪屏）
  const alertPane = $("pane-alert");
  const wasHidden = alertPane.hidden;
  let alertNeed = 0;
  if (wasHidden) alertPane.hidden = false;
  try {
    const forms = alertPane.querySelector(".forms");
    const fs = getComputedStyle(forms);
    const formsPad = (parseFloat(fs.paddingTop) || 0) + (parseFloat(fs.paddingBottom) || 0);
    alertNeed = formsPad + childrenNaturalHeight(forms);
  } finally {
    if (wasHidden) alertPane.hidden = true;
  }

  return Math.round(headerH + Math.max(statesNeed, alertNeed));
}

async function fitSettingsWindow() {
  const t = T();
  const win = t && t.window && t.window.getCurrentWindow ? t.window.getCurrentWindow() : null;
  if (!win || typeof win.setSize !== "function") return;
  try {
    const need = contentNeedHeight() + 4;
    const availH = Math.round(screen.availHeight || 0);
    const maxH = availH ? Math.floor(availH * 0.85) : 720;
    const h = Math.max(WIN_MIN_H, Math.min(need, maxH));
    const w = Math.round(window.innerWidth);
    const Size = t.window.LogicalSize;
    await win.setSize(Size ? new Size(w, h) : { type: "logical", width: w, height: h });
    if (typeof win.setPosition === "function") {
      const Pos = t.window.LogicalPosition;
      const availW = Math.round(screen.availWidth || w);
      const x = Math.round((screen.availLeft || 0) + (availW - w) / 2);
      const y = Math.round((screen.availTop || 0) + ((availH || h) - h) / 2);
      await win.setPosition(Pos ? new Pos(x, y) : { type: "logical", x, y });
    }
  } catch (_) {
    /* 无窗口 API / 权限不足时保持原尺寸，不打断加载 */
  }
}

// ---- 保存 / 主题 ----------------------------------------------------------
let DIRTY = false;          // 有未保存改动
let SAVING = false;

function markDirty() {
  if (SAVING) return;
  DIRTY = true;
  $("saveBtn").disabled = false;
  setStatus("有未保存的配置改动", "warn");
  document.title = "知灵桌宠 · 设置 *";
}

function clearDirty() {
  DIRTY = false;
  $("saveBtn").disabled = true;
  document.title = "知灵桌宠 · 设置";
}

async function save() {
  const n = ROWS.length;
  ROWS.forEach((row, i) => {
    row.raw.priority = (n - 1 - i) * 10;
    row.raw.clipIds = row.clipIds.slice();
    if (!Array.isArray(row.raw.fallbackClipIds)) row.raw.fallbackClipIds = [];
  });
  const states = {};
  for (const row of ROWS) states[row.key] = row.raw;
  THEME.states = states;
  try {
    SAVING = true;
    setStatus("保存中…");
    // 素材目录先展开环境变量（纯前端），有误就地标红并终止，不写主题/配置
    let packsDir = "";
    if (PACKS_VALUE.trim()) {
      const ex = await expandPacksInput(PACKS_VALUE);
      if (ex.error) {
        showPacksMsg(ex.error);
        setStatus(ex.error.replace(/^⚠ /, ""), "err");
        return;
      }
      packsDir = ex.value;
    }
    await invoke("write_theme", { id: THEME_ID, data: THEME });
    const remindHold = REMIND_HOLD;
    const pWork = clampInt($("pomoWork").value, 1, 180, 25);
    const pBreak = clampInt($("pomoBreak").value, 1, 60, 5);
    const pomoEnabled = $("pomoEnabled").checked;
    $("pomoWork").value = pWork;
    $("pomoBreak").value = pBreak;
    const dshPort = clampInt($("dshPort").value, 0, 65535, DEFAULT_DSH_PORT);
    $("dshPort").value = dshPort;
    const statePort = clampInt($("statePort").value, 0, 65535, DEFAULT_STATE_PORT);
    $("statePort").value = statePort;
    if (packsDir !== PACKS_SAVED) {
      try {
        await invoke("set_packs_dir", { path: packsDir });
      } catch (e) {
        // Rust 侧校验未通过且没有写入 → 原有效值保留在配置里
        const txt = packsErrorText(e);
        showPacksMsg(txt);
        setStatus(`素材目录未保存：${txt.replace(/^⚠ /, "")}`, "err");
        return;
      }
      PACKS_SAVED = packsDir;   // 语义值："" = 默认顺序；否则是展开后的绝对路径
      PACKS_VALUE = packsDir;
      $("packsDir").value = packsDir;
      clearPacksMsg();
    }
    await invoke("save_config", {
      patch: {
        remindHold, packsDir: PACKS_SAVED, dshPort, statePort,
        pomodoro: { enabled: pomoEnabled, workMin: pWork, breakMin: pBreak },
      },
    });
    refreshConn().catch(() => {});
    const tid = await refreshThemes(THEME_ID);
    clearDirty();
    setStatus(`配置已保存：${THEME.name || THEME_ID}`, "ok");
    if (tid !== THEME_ID) await loadTheme(tid);
  } catch (e) {
    setStatus(`保存配置失败：${e}`, "err");
  } finally {
    SAVING = false;
  }
}

/// 恢复上次配置：读 <packs>/.backup/<id>.json.bak 回填到界面，点「保存配置」才真正写入。
async function restoreBackup() {
  try {
    const bak = await invoke("read_theme_backup", { id: THEME_ID });
    if (!bak || typeof bak !== "object") {
      setStatus("没有可恢复的配置备份（还没保存过）", "err");
      return;
    }
    if (DIRTY && !confirm("当前有未保存的配置改动，载入备份会覆盖它们，继续？")) return;
    THEME = bak;
    THEME.states = (THEME.states && typeof THEME.states === "object") ? THEME.states : {};
    ROWS = STATES.map((key, defIdx) => {
      const st = stateOf(key);
      return {
        key, raw: st, defIdx,
        clipIds: Array.isArray(st.clipIds) ? st.clipIds.filter((c) => typeof c === "string") : [],
        priority: Number(st.priority) || 0,
      };
    });
    ROWS.sort((a, b) => (b.priority - a.priority) || (a.defIdx - b.defIdx));
    SEL = (ROWS.find((r) => r.clipIds.length) || ROWS[ROWS.length - 1] || {}).key || "";
    const selRow = rowOf(SEL);
    SEL_CLIP = (selRow && selRow.clipIds[0]) || "";
    renderAll();
    if (SEL_CLIP) loadPreview(SEL_CLIP);
    markDirty();
    setStatus("已载入上次保存的配置，点「保存配置」写回", "ok");
  } catch (e) {
    setStatus(`恢复上次配置失败：${e}`, "err");
  }
}

async function switchTheme(id) {
  if (!id || id === THEME_ID) return;
  try {
    await invoke("set_active_theme", { id });
    await refreshThemes(id);
    await loadTheme(id);
    setStatus(`已切换到「${THEME.name || id}」`, "ok");
  } catch (e) {
    setStatus(`切换失败：${e}`, "err");
  }
}

function openRename() {
  $("renameInput").value = THEME.name || THEME_ID;
  $("renameModal").classList.add("show");
  $("renameInput").focus();
}
function closeRename() { $("renameModal").classList.remove("show"); }
async function doRename() {
  const name = $("renameInput").value.trim();
  if (!name) return;
  THEME.name = name;
  closeRename();
  try {
    await invoke("write_theme", { id: THEME_ID, data: THEME });
    await refreshThemes(THEME_ID);
    setStatus(`主题已重命名为「${name}」`, "ok");
  } catch (e) {
    setStatus(`重命名失败：${e}`, "err");
  }
}

// ---- 事件绑定 ------------------------------------------------------------
bindStateList();
bindRight();

$("addClip").addEventListener("click", openLib);
$("libClose").addEventListener("click", closeLib);
$("libCancel").addEventListener("click", closeLib);
$("libAdd").addEventListener("click", addFromLib);
$("libModal").addEventListener("click", (e) => { if (e.target.id === "libModal") closeLib(); });

$("pvPlay").addEventListener("click", () => {
  if (!pv.clip) return;
  if (pv.playing) {
    stopPreview();
    return;
  }
  pv.playing = true;
  pv.next = performance.now();
  $("pvPlay").textContent = "暂停";
  pv.raf = requestAnimationFrame(pvLoop);
});

$("pvWorkshop").addEventListener("click", () => {
  if (!SEL_CLIP) return;
  invoke("open_workshop", { clip: SEL_CLIP }).catch((e) => setStatus(`打开工坊失败：${e}`, "err"));
});

$("newClip").addEventListener("click", () => {
  const r = selRow();
  invoke("open_workshop", { state: r ? r.key : "", theme: THEME_ID })
    .then(() => setStatus("已打开素材工坊", "ok"))
    .catch((e) => setStatus(`打开工坊失败：${e}`, "err"));
});

$("remindHold").addEventListener("change", (e) => {
  REMIND_HOLD = e.target.checked;
  markDirty();
});

$("resetOrder").addEventListener("click", () => {
  const byKey = new Map(ROWS.map((r) => [r.key, r]));
  ROWS = DEFAULT_ORDER.map((key, i) => {
    const r = byKey.get(key);
    r.defIdx = i;
    return r;
  });
  markDirty();
  renderAll();
});

$("themeSel").addEventListener("change", (e) => switchTheme(e.target.value));
$("renameTheme").addEventListener("click", openRename);
$("renameCancel").addEventListener("click", closeRename);
$("renameOk").addEventListener("click", doRename);
$("renameInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doRename();
  if (e.key === "Escape") closeRename();
});

$("saveBtn").addEventListener("click", save);
$("restoreBtn").addEventListener("click", restoreBackup);

// 任何表单改动都点亮「保存」并标记未保存
document.addEventListener("input", (e) => {
  if (e.target.closest && e.target.closest("header")) return;
  markDirty();
}, true);
document.addEventListener("change", (e) => {
  if (e.target && e.target.id === "themeSel") return;   // 切换主题立即生效，不走保存
  markDirty();
}, true);

// 分页切换
$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (btn) switchTab(btn.dataset.tab);
});

// 浏览选择素材目录（原生文件夹选择框，由 Rust 侧拉起）
$("pickPacks").addEventListener("click", async () => {
  try {
    const picked = await invoke("pick_packs_dir");
    if (picked) {
      PACKS_VALUE = picked;
      $("packsDir").value = picked;
      $("packsSrc").textContent = "配置";
      clearPacksMsg();
      markDirty();
      setStatus(`已选择素材目录：${picked}。点「保存配置」后生效。`, "ok");
    }
  } catch (e) {
    setStatus(`选择目录失败：${e}`, "err");
  }
});

// 用资源管理器打开「当前生效」的目录（不是输入框里尚未保存的草稿）
$("openPacks").addEventListener("click", async () => {
  const info = await invoke("assets_info").catch(() => null);
  const dir = (info && info.root) || "";
  if (!dir) return setStatus("还没有可打开的素材目录", "err");
  try {
    await invoke("workshop_open_dir", { path: dir });
    setStatus(`已在资源管理器中打开：${dir}`, "ok");
  } catch (e) {
    setStatus(`打开目录失败：${e}`, "err");
  }
});

// 手输/粘贴路径：实时同步语义值，并清掉上一次的红色校验提示
$("packsDir").addEventListener("input", (e) => {
  PACKS_VALUE = e.target.value;
  clearPacksMsg();
});

// 留空 = 按默认顺序解析（%LOCALAPPDATA%\DeskPet\packs → exe 同级 assets）
$("resetPacks").addEventListener("click", () => {
  PACKS_VALUE = "";
  $("packsDir").value = "";
  $("packsSrc").textContent = "默认";
  clearPacksMsg();
  markDirty();
  setStatus("已清空自定义路径：点「保存配置」后按默认顺序解析。", "ok");
});

// 复制 HTTP 入口（自定义协议下 navigator.clipboard 可能不可用，退回到 execCommand）
$("copyState").addEventListener("click", async () => {
  const si = await invoke("state_info").catch(() => null);
  const text = (si && si.port) ? si.url : "";
  if (!text) return setStatus("HTTP 入口已关闭，没有可复制的地址", "err");
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  setStatus(`已复制 ${text}`, "ok");
});

// DSH 立即重连（后台线程断开后会按退避重连，这里只是催一下）
$("dshRecheck").addEventListener("click", async () => {
  $("dshMsg").textContent = "重连中…";
  await invoke("dsh_recheck").catch(() => null);
  setTimeout(() => refreshConn().catch(() => {}), 400);
});

// 新建主题（与工坊共用同一套主题索引，画布默认 576×530）
$("newTheme").addEventListener("click", async () => {
  const name = prompt("新主题名称（例如 女仆装）：");
  if (!name || !name.trim()) return;
  try {
    const id = await invoke("workshop_new_series", { name: name.trim() });
    await refreshThemes(id);
    await loadTheme(id);
    setStatus(`已新建主题「${name.trim()}」（${id}）`, "ok");
  } catch (e) {
    setStatus(`新建主题失败：${e}`, "err");
  }
});
$("closeBtn").addEventListener("click", () => {
  if (DIRTY && !confirm("有未保存的配置改动，确定放弃并关闭？")) return;
  const t = T();
  try {
    if (t && t.window && t.window.getCurrentWindow) t.window.getCurrentWindow().close();
  } catch (_) { /* ignore */ }
});

// 原生关闭按钮（窗口右上角）也拦一次
(() => {
  const t = T();
  const win = t && t.window && t.window.getCurrentWindow ? t.window.getCurrentWindow() : null;
  if (!win || typeof win.onCloseRequested !== "function") return;
  win.onCloseRequested((ev) => {
    if (!DIRTY) return;
    if (!confirm("有未保存的配置改动，确定放弃并关闭？")) ev.preventDefault();
  });
})();

boot().catch((e) => setStatus(`载入失败：${e}`, "err"));

// 「提醒与连接」页开着时，每 3s 刷新一次连接状态（DSH 灯要反映真实连接）。
setInterval(() => {
  if (!$("pane-alert").hidden) refreshConn().catch(() => {});
}, 3000);
