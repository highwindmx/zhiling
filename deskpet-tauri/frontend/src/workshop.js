// 素材工坊前端（对齐 deskpet-python/workshop/workshop.py 的 2×2 布局）：
// 左上导入视频 / 左下导出；右上三视图（原片 / 抠像后 / alpha）+ 播放轴；右下参数（可折叠）+ 日志。
// Rust 侧 src/workshop.rs 一一对应；参数名与默认值与 Python 版一致
// （sat_lo .30 / sat_hi .55 / hue_in 18 / hue_out 34 / floor .16 / despill .75 /
//  hue_ref 136 / blur 1.0 / erode 1 / keep_frac .05；val_lo .30 / val_hi .45 固定）。

import { STATES, STATE_CN, esc, DEFAULT_CANVAS } from "./shared.js";

const FIXED_VAL_LO = 0.30;
const FIXED_VAL_HI = 0.45;
const PREVIEW_DEBOUNCE = 160;

function T() { return window.__TAURI__ || null; }
async function invoke(cmd, args = {}) {
  const t = T();
  if (t && t.core && typeof t.core.invoke === "function") return t.core.invoke(cmd, args);
  if (t && typeof t.invoke === "function") return t.invoke(cmd, args);
  throw new Error("Tauri 接口不可用（请用 deskpet-tauri 打开本页）");
}

const $ = (id) => document.getElementById(id);

/** 二进制回包统一成 ArrayBuffer（Tauri 2 的 ipc::Response 给 ArrayBuffer）。 */
function toArrayBuffer(r) {
  if (r instanceof ArrayBuffer) return r;
  if (ArrayBuffer.isView(r)) return r.buffer.slice(r.byteOffset, r.byteOffset + r.byteLength);
  if (Array.isArray(r)) return Uint8Array.from(r).buffer;
  throw new Error("未知的二进制回包类型");
}

const S = {
  path: "",
  info: null,          // {width,height,fps,duration,frames}
  at: 0,
  crop: null,          // [l,t,r,b] 源帧像素，r/b 为右/下边界
  series: [],
  outDir: "",          // 上次导出目录（「打开导出目录」用）
  mode: "crop",        // crop | sample | erase（同一块画布三种工具）
  rubber: null,        // 正在拖的取色/擦除框 [x0,y0,x1,y1]（源帧坐标）
  solid: false,        // 「抠图后」预览是否铺纯色底
  transparent: null,   // 最近一次预览的透明占比（0–100）
  hoverRaw: false,     // 鼠标是否在原片视图上（悬停才画裁切框）
  dirty: false,        // 当前源视频是否有未导出的裁切 / 标注改动
};

const rawCv = document.createElement("canvas");   // 源帧
const keyCv = document.createElement("canvas");   // 裁边后抠像结果（crop 尺寸）
const alphaCv = document.createElement("canvas"); // alpha 通道（白=不透明）
keyCv.width = keyCv.height = 0;
alphaCv.width = alphaCv.height = 0;
rawCv.width = rawCv.height = 0;
const prevCv = document.createElement("canvas");  // 成品画布
let checker = null;

function log(msg, cls = "") {
  const box = $("log");
  const t = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const span = document.createElement("span");
  span.className = cls;
  span.textContent = `[${t}] ${msg}\n`;
  box.appendChild(span);
  box.scrollTop = box.scrollHeight;
}

function checkerPattern(ctx) {
  if (!checker) {
    const c = document.createElement("canvas");
    c.width = c.height = 16;
    const g = c.getContext("2d");
    g.fillStyle = "#2a2d34"; g.fillRect(0, 0, 16, 16);
    g.fillStyle = "#22252b"; g.fillRect(0, 0, 8, 8); g.fillRect(8, 8, 8, 8);
    checker = c;
  }
  return ctx.createPattern(checker, "repeat");
}

// ---------------------------------------------------------------- 参数
function collectParams() {
  const num = (id) => parseFloat($(id).value);
  return {
    hue_ref: num("hue_ref"),
    hue_in: num("hue_in"),
    hue_out: num("hue_out"),
    sat_lo: num("sat_lo"),
    sat_hi: num("sat_hi"),
    val_lo: FIXED_VAL_LO,
    val_hi: FIXED_VAL_HI,
    erode: parseInt($("erode").value, 10),
    blur: num("blur"),
    despill: num("despill"),
    floor: num("floor"),
    keep_frac: $("keepMain").checked ? 0.05 : 1.0,
    watermarks: rowMarks("erases").slice(),
  };
}

function fmtVal(id, v) {
  if (id === "erode") return String(v);
  return Number(v).toFixed(2);
}

function syncParamLabels() {
  for (const el of document.querySelectorAll("#advBox input[type=range], .slider input[type=range]")) {
    const out = document.querySelector(`.val[data-for="${el.id}"]`);
    if (out) out.textContent = fmtVal(el.id, el.value);
  }
  $("hueRefVal").textContent = Number($("hue_ref").value).toFixed(1);
}

function setParamsDefaults() {
  const d = { hue_ref: 136.0, sat_lo: 0.30, sat_hi: 0.55, hue_in: 18.0, hue_out: 34.0,
              floor: 0.16, despill: 0.75, blur: 1.0, erode: 1 };
  for (const [k, v] of Object.entries(d)) $(k).value = v;
  $("keepMain").checked = true;
  syncParamLabels();
}

// ---------------------------------------------------------------- 画面
/** 原片视图：整帧；裁切框只在鼠标悬停该视图时显示（其余时间不画细线框）。 */
function paintRawView() {
  const cv = $("cvRaw");
  if (!S.info || !rawCv.width) { cv.width = cv.height = 1; return; }
  cv.width = rawCv.width;
  cv.height = rawCv.height;
  const ctx = cv.getContext("2d");
  ctx.drawImage(rawCv, 0, 0);
  if (!S.crop || !S.hoverRaw) return;
  ctx.strokeStyle = "#7c9cff";
  ctx.lineWidth = Math.max(1, cv.width / 480);
  ctx.strokeRect(S.crop[0], S.crop[1], S.crop[2] - S.crop[0], S.crop[3] - S.crop[1]);
}

// 原片视图只做对照，不接拖拽；裁切框仅在悬停时出现，避免常驻蓝框误导
$("cvRaw").addEventListener("mouseenter", () => { S.hoverRaw = true; paintRawView(); });
$("cvRaw").addEventListener("mouseleave", () => { S.hoverRaw = false; paintRawView(); });

/** 「抠图后」：抠像结果铺在裁切框里（棋盘格/实色底），叠加裁切框与标注。 */
function paintStage() {
  const cv = $("stage");
  if (!S.info) { cv.width = cv.height = 1; return; }
  if (!cv.width || cv.width !== S.info.width) {
    cv.width = S.info.width;
    cv.height = S.info.height;
  }
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!rawCv.width) return;
  if (!S.crop) { ctx.drawImage(rawCv, 0, 0); return; }

  const [l, t, r, b] = S.crop;
  const w = r - l, h = b - t;
  ctx.save();
  ctx.beginPath();
  ctx.rect(l, t, w, h);
  ctx.clip();
  if (S.solid) {
    ctx.fillStyle = "#2f323a";
    ctx.fillRect(l, t, w, h);
  } else {
    ctx.fillStyle = checkerPattern(ctx);
    ctx.fillRect(l, t, w, h);
  }
  if (keyCv.width) ctx.drawImage(keyCv, l, t, w, h);
  else ctx.drawImage(rawCv, l, t, w, h, l, t, w, h);
  ctx.restore();

  ctx.strokeStyle = "#7c9cff";
  ctx.lineWidth = 1;
  ctx.strokeRect(l + 0.5, t + 0.5, w - 1, h - 1);
  ctx.fillStyle = "#7c9cff";
  const hs = 4;
  for (const [hx, hy] of [[l, t], [r, t], [l, b], [r, b]]) ctx.fillRect(hx - hs, hy - hs, hs * 2, hs * 2);
  drawMarks(ctx, 1, 1);
}

/** alpha 通道：白=不透明，黑=透明。 */
function paintAlphaView() {
  const cv = $("cvAlpha");
  if (!keyCv.width) { cv.width = cv.height = 1; return; }
  cv.width = alphaCv.width;
  cv.height = alphaCv.height;
  cv.getContext("2d").drawImage(alphaCv, 0, 0);
}

function buildAlpha(img) {
  const out = new ImageData(img.width, img.height);
  const src = img.data, dst = out.data;
  for (let i = 0; i < src.length; i += 4) {
    const a = src[i + 3];
    dst[i] = dst[i + 1] = dst[i + 2] = a;
    dst[i + 3] = 255;
  }
  alphaCv.width = img.width;
  alphaCv.height = img.height;
  alphaCv.getContext("2d").putImageData(out, 0, 0);
}

/** 取色框（柔蓝）/ 擦除框（暗红）/ 正在拖的虚线框，全部用源帧坐标。 */
function drawMarks(ctx, sx, sy) {
  const box = (rect, stroke, fill, dash) => {
    const x = rect[0] * sx, y = rect[1] * sy;
    const w = (rect[2] - rect[0]) * sx, h = (rect[3] - rect[1]) * sy;
    if (w <= 0 || h <= 0) return;
    ctx.save();
    ctx.fillStyle = fill;
    ctx.fillRect(x, y, w, h);
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1;
    if (dash) ctx.setLineDash([4, 3]);
    ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
    ctx.restore();
  };
  for (const r of rowMarks("samples")) box(r, "#5b8cff", "rgba(91,140,255,.16)", false);
  for (const r of rowMarks("erases")) box(r, "#c04a4a", "rgba(192,74,74,.20)", false);
  if (S.rubber) {
    const r = S.rubber;
    const rect = [Math.min(r[0], r[2]), Math.min(r[1], r[3]),
                  Math.max(r[0], r[2]), Math.max(r[1], r[3])];
    box(rect, S.mode === "erase" ? "#e07070" : "#9db6ff",
        S.mode === "erase" ? "rgba(224,112,112,.14)" : "rgba(157,182,255,.12)", true);
  }
}

function curRow() {
  return VINDEX >= 0 && VINDEX < VIDEOS.length ? VIDEOS[VINDEX] : null;
}

/** 取色框 / 擦除框挂在「每一行视频」上，切行互不串（与 Python 版一致）。 */
function rowMarks(kind) {
  const v = curRow();
  return v ? v[kind] : [];
}

function updateMarks() {
  if (!$("marks")) return;
  const c = S.crop && S.info;
  const cropTxt = c && (S.crop[0] || S.crop[1] || S.crop[2] !== S.info.width || S.crop[3] !== S.info.height)
    ? `L${S.crop[0]} T${S.crop[1]} R${S.crop[2]} B${S.crop[3]}`
    : "全帧";
  $("marks").textContent = `裁切 ${cropTxt} ｜ 取色框 ${rowMarks("samples").length} ｜ 擦除框 ${rowMarks("erases").length} ｜ 拖框即重算 · 右键删框`;
}

/** 把二进制回包拆成 {w,h,rgba} 两张图。 */
function unpackPreview(ab) {
  const dv = new DataView(ab);
  const [w1, h1, w2, h2, bg] = [0, 1, 2, 3, 4].map((i) => dv.getUint32(i * 4, true));
  const off1 = 24;
  const off2 = off1 + w1 * h1 * 4;
  return {
    img1: new ImageData(new Uint8ClampedArray(ab, off1, w1 * h1 * 4), w1, h1),
    img2: new ImageData(new Uint8ClampedArray(ab, off2, w2 * h2 * 4), w2, h2),
    bg,
  };
}

function putImage(cv, data) {
  cv.width = data.width;
  cv.height = data.height;
  cv.getContext("2d").putImageData(data, 0, 0);
}

// ---------------------------------------------------------------- 帧号 / 播放
function totalFrames() {
  return S.info ? Math.max(1, Math.round(S.info.duration * S.info.fps)) : 0;
}

function syncFrameLabel() {
  if (!S.info) { $("frameLabel").textContent = "0 / 0"; return; }
  const n = totalFrames();
  const k = Math.min(n, Math.round(S.at * S.info.fps) + 1);
  $("frameLabel").textContent = `${k} / ${n}`;
}

let playing = false, playTimer = null;
function setPlaying(on) {
  if (!S.info) return;
  playing = on;
  $("btnPlay").disabled = on;
  $("btnPause").disabled = !on;
  clearTimeout(playTimer);
  if (on) playStep();
}
async function playStep() {
  if (!playing || !S.info) return;
  const dur = S.info.duration;
  let at = S.at + 1 / S.info.fps;
  if (at >= dur) at = 0;
  S.at = at;
  $("seek").value = String(at);
  await loadRaw();
  await preview();
  if (playing) playTimer = setTimeout(playStep, 1000 / S.info.fps);
}

// ---------------------------------------------------------------- 裁边交互
function toSrc(ev) {
  const rect = $("stage").getBoundingClientRect();
  const sx = S.info.width / rect.width;
  const sy = S.info.height / rect.height;
  return [Math.round((ev.clientX - rect.left) * sx), Math.round((ev.clientY - rect.top) * sy)];
}

let drag = null;
function hitEdges(pt) {
  const rect = $("stage").getBoundingClientRect();
  const th = Math.max(4, 8 * (S.info.width / rect.width));
  const [x, y] = pt;
  const [l, t, r, b] = S.crop;
  if (x < l - th || x > r + th || y < t - th || y > b + th) return null;
  let mode = "";
  if (Math.abs(x - l) <= th) mode += "l";
  else if (Math.abs(x - r) <= th) mode += "r";
  if (Math.abs(y - t) <= th) mode += "t";
  else if (Math.abs(y - b) <= th) mode += "b";
  return mode || null;
}

$("stage").addEventListener("pointerdown", (ev) => {
  if (!S.info || !S.crop) return;
  if (S.mode !== "crop") {
    if (ev.button !== 0) return;
    const [x, y] = toSrc(ev);
    S.rubber = [x, y, x, y];
    $("stage").setPointerCapture(ev.pointerId);
    paintStage();
    ev.preventDefault();
    return;
  }
  const mode = hitEdges(toSrc(ev));
  if (!mode) return;
  $("cbCropAuto").checked = false;   // 手动拖框后自动关掉「自动裁黑边」（与 Python 版一致）
  drag = { mode, start: toSrc(ev), crop: S.crop.slice() };
  $("stage").setPointerCapture(ev.pointerId);
  ev.preventDefault();
});

$("stage").addEventListener("pointermove", (ev) => {
  if (!S.info || !S.crop) return;
  if (S.rubber) {
    const [x, y] = toSrc(ev);
    S.rubber[2] = Math.max(0, Math.min(x, S.info.width));
    S.rubber[3] = Math.max(0, Math.min(y, S.info.height));
    paintStage();
    return;
  }
  if (!drag) {
    if (S.mode !== "crop") { $("stage").style.cursor = "crosshair"; return; }
    const mode = hitEdges(toSrc(ev));
    $("stage").style.cursor = mode
      ? (mode.length === 2 ? `${mode}-resize` : (mode === "l" || mode === "r" ? "ew-resize" : "ns-resize"))
      : "crosshair";
    return;
  }
  const [x, y] = toSrc(ev);
  const c = drag.crop.slice();
  if (drag.mode.includes("l")) c[0] = Math.min(x, c[2] - 16);
  if (drag.mode.includes("r")) c[2] = Math.max(x, c[0] + 16);
  if (drag.mode.includes("t")) c[1] = Math.min(y, c[3] - 16);
  if (drag.mode.includes("b")) c[3] = Math.max(y, c[1] + 16);
  c[0] = Math.max(0, c[0]); c[1] = Math.max(0, c[1]);
  c[2] = Math.min(S.info.width, c[2]); c[3] = Math.min(S.info.height, c[3]);
  S.dirty = true;      // 手动拖过裁切 = 未导出的裁切改动
  setCrop(c, false);   // 拖动中只同步数值与框，松手才重算
});

$("stage").addEventListener("pointerup", () => {
  if (S.rubber) {
    const r = S.rubber;
    S.rubber = null;
    const rect = [Math.min(r[0], r[2]), Math.min(r[1], r[3]),
                  Math.max(r[0], r[2]), Math.max(r[1], r[3])].map((n) => Math.round(n));
    paintStage();
    if (rect[2] - rect[0] >= 16 && rect[3] - rect[1] >= 16) {
      if (S.mode === "erase") addErase(rect);
      else if (S.mode === "sample") addSample(rect);
    }
    return;
  }
  if (drag) { drag = null; schedulePreview(); }
});

// 右键点已画的框 = 删掉那一处（取色框优先，其次擦除框，后加的在上层）
$("stage").addEventListener("contextmenu", (ev) => {
  ev.preventDefault();
  if (!S.info) return;
  const [x, y] = toSrc(ev);
  const hit = (rect) => x >= rect[0] && x <= rect[2] && y >= rect[1] && y <= rect[3];
  for (const kind of ["samples", "erases"]) {
    const arr = rowMarks(kind);
    for (let i = arr.length - 1; i >= 0; i--) {
      if (hit(arr[i])) {
        arr.splice(i, 1);
        S.dirty = true;
        log(`已删除${kind === "samples" ? "取色框" : "擦除框"} #${i + 1}`, "w");
        updateMarks();
        paintStage();
        preview();
        return;
      }
    }
  }
});

/** 加一处取色框：多点汇总推导闸门（hue_ref 无条件回填，其余闸门按分布回填）。 */
async function addSample(rect) {
  const v = curRow();
  if (!v || !S.path) return;
  const rects = [...v.samples, rect];
  try {
    const r = await invoke("workshop_sample_rects", { path: S.path, at: S.at, rects });
    v.samples.push(rect);
    S.dirty = true;
    $("hue_ref").value = r.hue_ref;
    $("hue_in").value = r.hue_in;
    $("hue_out").value = r.hue_out;
    $("sat_lo").value = r.sat_lo;
    $("sat_hi").value = r.sat_hi;
    syncParamLabels();
    updateMarks();
    paintStage();
    await preview();
    log(`取色框 ${v.samples.length}：框 ${rect.join(",")} · ${r.samples} 像素 · ` +
        `色相 ${r.hue_ref}°（偏差 p60 ${r.hue_dev60}° / p90 ${r.hue_dev90}°）· ` +
        `sat p05 ${r.sat05} p10 ${r.sat10} p50 ${r.sat50} → 闸门 hue ${r.hue_in}/${r.hue_out}、sat ${r.sat_lo}~${r.sat_hi}`, "s");
  } catch (e) {
    log(`取色失败：${e}`, "e");
  }
}

function addErase(rect) {
  const v = curRow();
  if (!v) return;
  v.erases.push(rect);
  S.dirty = true;
  updateMarks();
  paintStage();
  preview();
  log(`擦除框 ${v.erases.length}：源帧坐标 ${rect.join(",")}（预览与导出都会减去裁切左上偏移）`, "w");
}

function setCrop(c, preview = true) {
  S.crop = c;
  paintStage();
  paintRawView();
  updateMarks();
  if (preview) schedulePreview();
}

// ---------------------------------------------------------------- 视频列表
const VIDEOS = [];
let VINDEX = -1;

function thumbUrl(ab) {
  const w = new DataView(ab).getUint32(0, true);
  const h = new DataView(ab).getUint32(4, true);
  if (!w || !h) return "";
  const full = document.createElement("canvas");
  full.width = w; full.height = h;
  full.getContext("2d").putImageData(new ImageData(new Uint8ClampedArray(ab, 8, w * h * 4), w, h), 0, 0);
  const sc = Math.min(1, 160 / w);
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w * sc));
  c.height = Math.max(1, Math.round(h * sc));
  c.getContext("2d").drawImage(full, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.72);
}

function renderVideoList() {
  $("videoCount").textContent = `${VIDEOS.length} 个`;
  const box = $("videoList");
  const empty = !VIDEOS.length;
  box.innerHTML = empty ? '<span class="dim">未添加</span>' : VIDEOS.map((v, i) => `<div class="vitem${i === VINDEX ? " on" : ""}" data-i="${i}" title="${esc(v.path)}">
      <img src="${v.thumb || ""}" alt="">
      <span class="nm">${esc(v.name)}</span>
      <button class="x" data-rm="${i}" title="移除">×</button>
    </div>`).join("");
  renderThumb();
}

function renderThumb() {
  const v = curRow();
  const box = $("thumbBox");
  if (!v || !v.thumb) {
    box.innerHTML = '<span>选中视频后显示首帧</span>';
    return;
  }
  box.innerHTML = `<img src="${v.thumb}" alt="">`;
}

function clearVideo() {
  S.path = "";
  S.info = null;
  S.crop = null;
  S.rubber = null;
  S.dirty = false;
  VINDEX = -1;
  $("seek").value = 0;
  $("seek").max = 1;
  $("videoInfo").textContent = "尚未导入视频";
  $("videoInfo").title = "";
  $("bgStat").textContent = "—";
  $("stateLine").textContent = "参数 → 结果：等待载入视频";
  $("resultLine").textContent = "";
  $("srcLabel").textContent = "预览来源：—";
  resetFrames();
  renderVideoList();   // 空态下也走统一入口：列表重建为「未添加」、缩略图复位
  updateMarks();
}

/** 只允许一个源视频：移除即整体回到空态（裁切、标注、预览帧一起清掉）。 */
function removeVideo() {
  if (!VIDEOS.length) return;
  VIDEOS.length = 0;
  clearVideo();
}

/** 清掉上一段视频的帧，避免移除/切换时画布还留着旧画面。 */
function resetFrames() {
  keyCv.width = 0;
  alphaCv.width = 0;
  rawCv.width = 0;
  paintStage();
  paintRawView();
  paintAlphaView();
  $("frameLabel").textContent = "0 / 0";
}

async function addVideo(path) {
  const hit = VIDEOS.findIndex((v) => v.path.toLowerCase() === path.toLowerCase());
  if (hit >= 0) {
    VINDEX = hit;
    renderVideoList();
    return loadVideo(path);
  }
  // 一次只允许一个源视频：替换前若有未导出的裁切/标注，先确认
  if (VIDEOS.length) {
    if (S.dirty && !confirm("当前源视频有未导出的裁切 / 标注，换成新视频会全部丢弃。\n确定替换吗？")) {
      log("已取消替换，当前源视频保持不变", "w");
      return false;
    }
    VIDEOS.length = 0;       // 旧的裁切、取色框、擦除框、预览帧全部作废
    VINDEX = -1;
    S.crop = null;
    S.rubber = null;
    S.dirty = false;
    resetFrames();
    updateMarks();
  }
  const item = { path, name: path.split(/[\\/]/).pop(), thumb: "", samples: [], erases: [], autoKeyDone: false };
  VIDEOS.push(item);
  VINDEX = 0;
  renderVideoList();
  await loadVideo(path);
  try {
    item.thumb = thumbUrl(toArrayBuffer(await invoke("workshop_frame_raw", { path, at: 0.05 })));
    renderVideoList();
  } catch (_) { /* 缩略图失败不影响主流程 */ }
}

// ---------------------------------------------------------------- 载入
async function loadVideo(path) {
  S.path = path;
  S.at = 0;
  setPlaying(false);
  $("seek").value = 0;
  const idx = VIDEOS.findIndex((v) => v.path.toLowerCase() === path.toLowerCase());
  if (idx >= 0) VINDEX = idx;
  renderVideoList();
  log(`载入 ${path}`);
  const info = await invoke("workshop_probe", { path });
  if (S.path !== path) return;   // 连续移除/切换时，旧请求结果直接丢弃
  if (!info) return log("视频探测失败", "e");
  S.info = info;
  $("videoInfo").textContent =
    `${info.width}×${info.height} · ${info.fps.toFixed(2)}fps · ${info.duration.toFixed(2)}s`;
  $("videoInfo").title = path;
  $("seek").max = Math.max(0.01, info.duration);
  $("seek").step = info.duration > 30 ? 0.1 : 0.02;
  $("srcLabel").textContent = `预览来源：${path}`;
  setCrop([0, 0, info.width, info.height], false);
  log(`分辨率 ${info.width}×${info.height}，${info.fps.toFixed(2)} fps，时长 ${info.duration.toFixed(2)}s`);
  if ($("cbCropAuto").checked) {
    try {
      const r = await invoke("workshop_auto_crop", { path: S.path, at: 0.1 });
      if (S.path !== path) return;
      if (r && r.crop) {
        setCrop(r.crop, false);
        log(`自动裁边：L${r.crop[0]} T${r.crop[1]} R${r.crop[2]} B${r.crop[3]}`, "s");
      } else {
        log(`自动裁边没找到暗边（${r ? r.detail : "无结果"}），保持全帧`, "w");
      }
    } catch (e) { log(`自动裁边失败：${e}`, "e"); }
  }
  // 每个视频首次载入自动取色（对齐 Python：出厂 hue_ref=136° 对本批绿幕无效，不改就会整片绿底）
  const item = VIDEOS[idx >= 0 ? idx : VINDEX];
  if (item && !item.autoKeyDone) {
    item.autoKeyDone = true;
    await autoKeyMulti(true);
  }
  await loadRaw();
  await preview();
  syncFrameLabel();
  refreshClipId();
}

/** 回填一组取样参数并重算预览。 */
async function applySampled(r) {
  $("hue_ref").value = r.hue_ref;
  $("sat_lo").value = r.sat_lo;
  $("sat_hi").value = r.sat_hi;
  if (r.hue_in != null) $("hue_in").value = r.hue_in;
  if (r.hue_out != null) $("hue_out").value = r.hue_out;
  syncParamLabels();
  await preview();
}

/** 多帧取色（对齐 Python 的 auto_key）：抽 3 帧四角取色，色相/饱和度取中位数后回填。 */
async function autoKeyMulti(silent = false) {
  if (!S.path || !S.info) return false;
  const d = S.info.duration;
  const times = [Math.min(0.5, d * 0.1), d * 0.5, Math.max(0.1, d * 0.9)];
  try {
    const rs = [];
    for (const at of times) rs.push(await invoke("workshop_auto_sample", { path: S.path, at, crop: S.crop }));
    const med = (arr) => { const a = [...arr].sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; };
    const agg = {
      hue_ref: med(rs.map((r) => r.hue_ref)),
      sat_lo: med(rs.map((r) => r.sat_lo)),
      sat_hi: med(rs.map((r) => r.sat_hi)),
      sat_med: med(rs.map((r) => r.sat_med)),
      samples: rs.reduce((n, r) => n + r.samples, 0),
    };
    await applySampled(agg);
    log(`${silent ? "首次载入自动取色" : "多帧取色"}（${times.map((t) => t.toFixed(2) + "s").join(" / ")}）：` +
        `hue_ref=${agg.hue_ref}° sat_lo=${agg.sat_lo} sat_hi=${agg.sat_hi}（共 ${agg.samples} 像素）`, "s");
    return true;
  } catch (e) {
    if (!silent) log(`多帧取色失败：${e}`, "e");
    return false;
  }
}

async function loadRaw() {
  if (!S.path) return;
  const ab = toArrayBuffer(await invoke("workshop_frame_raw", { path: S.path, at: S.at }));
  const w = new DataView(ab).getUint32(0, true);
  const h = new DataView(ab).getUint32(4, true);
  const img = new ImageData(new Uint8ClampedArray(ab, 8, w * h * 4), w, h);
  putImage(rawCv, img);
  paintRawView();
  paintStage();
  syncFrameLabel();
}

let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(preview, PREVIEW_DEBOUNCE);
}

function stateLineText(p) {
  if (!S.info) return "参数 → 结果：等待载入视频";
  const tr = S.transparent;
  const n = totalFrames();
  const k = Math.min(n, Math.round(S.at * S.info.fps) + 1);
  return `参数 → 结果：hue ${Number(p.hue_ref).toFixed(0)}° · sat ${Number(p.sat_lo).toFixed(2)}~${Number(p.sat_hi).toFixed(2)}` +
         ` · floor ${Number(p.floor).toFixed(2)} · despill ${Number(p.despill).toFixed(2)}` +
         ` → 透明 ${tr == null ? "—" : tr + "%"} · 第 ${k}/${n} 帧`;
}

async function preview() {
  if (!S.path) return;
  const canvas = seriesCanvas();
  const params = collectParams();
  const t0 = performance.now();
  try {
    const ab = toArrayBuffer(await invoke("workshop_preview", {
      req: { path: S.path, at: S.at, crop: S.crop, params, canvas, solid: S.solid },
    }));
    const { img1, bg } = unpackPreview(ab);
    putImage(keyCv, img1);
    buildAlpha(img1);
    paintStage();
    paintAlphaView();
    S.transparent = (bg / 10).toFixed(1);
    $("bgStat").textContent = `透明 ${S.transparent}%`;
    $("stateLine").textContent = stateLineText(params);
    $("perf").textContent = `${Math.round(performance.now() - t0)} ms`;
  } catch (e) {
    log(`预览失败：${e}`, "e");
  }
}

// ---------------------------------------------------------------- 主题 / 导出
let seriesMap = new Map();
function seriesCanvas() {
  const id = $("seriesSel").value;
  const item = seriesMap.get(id);
  return item && item.canvas ? item.canvas : DEFAULT_CANVAS;
}

async function loadSeries() {
  const list = (await invoke("workshop_series")) || [];
  S.series = list;
  seriesMap = new Map(list.map((x) => [x.id, x]));
  const sel = $("seriesSel");
  const keep = sel.value;
  sel.innerHTML = `<option value="">（不指定主题，画布 ${DEFAULT_CANVAS[0]}×${DEFAULT_CANVAS[1]}）</option>` +
    list.map((x) => `<option value="${x.id}">${x.name}${x.active ? " · 当前" : ""}（${x.canvas[0]}×${x.canvas[1]}）</option>`).join("");
  sel.value = list.some((x) => x.id === keep) ? keep : (list.find((x) => x.active)?.id || "");
}

function idFromName(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 24);
}

function updateIdStatus() {
  const id = $("clipId").value.trim();
  if (!id) {
    $("idStatus").textContent = "留空时按动画名称自动生成 ASCII id";
    $("idStatus").className = "hint";
    return;
  }
  if (!/^[A-Za-z0-9_]+$/.test(id)) {
    $("idStatus").textContent = "assetId 只能字母/数字/下划线（ASCII）";
    $("idStatus").className = "hint";
    $("idStatus").style.color = "var(--err)";
    return;
  }
  $("idStatus").style.color = "";
  $("idStatus").className = "hint";
  $("idStatus").textContent = `将写入 animations/${id}/（f0001.webp… + meta.json）`;
}

function refreshClipId() {
  if ($("clipId").value.trim()) return;
  const nm = $("clipName").value.trim();
  const base = nm || (S.path ? S.path.split(/[\\/]/).pop().replace(/\.[^.]+$/, "") : "");
  $("clipId").value = idFromName(base) || "clip_new";
  updateIdStatus();
}

let exporting = false;
async function doExport() {
  if (exporting) return;
  if (!S.path) return log("先选一段视频", "e");
  const clip = $("clipId").value.trim() || idFromName($("clipName").value.trim());
  if (!/^[A-Za-z0-9_]+$/.test(clip)) return log("assetId 只能用字母/数字/下划线", "e");
  $("clipId").value = clip;
  updateIdStatus();
  const name = $("clipName").value.trim() || clip;
  const theme = $("seriesSel").value || null;
  const state = $("stateSel").value || null;
  if (!theme && state) return log("要绑定状态，先选一个主题", "e");
  exporting = true;
  $("doExport").disabled = true;
  $("cancelExport").disabled = false;
  $("exportBadge").className = "badge warn";
  $("exportBadge").textContent = "导出中…";
  $("progBar").style.width = "8%";
  $("resultLine").textContent = "";
  log(`开始导出 ${clip}（${seriesCanvas().join("×")}）…`);
  try {
    const r = await invoke("workshop_export", {
      req: {
        path: S.path, crop: S.crop, params: collectParams(), fps: 24,
        canvas: seriesCanvas(), clip_id: clip, name, theme, state,
        overwrite: $("overwrite").checked,
      },
    });
    $("progBar").style.width = "100%";
    $("exportBadge").className = "badge ok";
    $("exportBadge").textContent = "完成";
    S.dirty = false;   // 已落盘的裁切/标注，不再算未导出改动
    S.outDir = r.outDir;
    $("resultLine").textContent =
      `已生成 ${r.count} 帧 · ${(r.bytes / 1e6).toFixed(2)} MB · ${r.width}×${r.height} · ` +
      `scale ${r.scale} · offset ${r.offset.join(",")}`;
    log(`导出完成：${r.count} 帧，${(r.bytes / 1024).toFixed(0)} KB，画布 ${r.width}×${r.height}，` +
        `scale=${r.scale} offset=${r.offset.join(",")}`, "s");
    log(`目录：${r.outDir}`);
    if (r.bound) log(`已绑定到主题「${r.theme}」的状态 ${state}`, "s");
    await loadSeries();
  } catch (e) {
    const msg = String(e);
    if (msg.includes("已取消")) {
      $("exportBadge").className = "badge warn";
      $("exportBadge").textContent = "已取消";
      $("progBar").style.width = "0";
      log(msg, "w");
    } else {
      $("exportBadge").className = "badge err";
      $("exportBadge").textContent = "失败";
      $("progBar").style.width = "0";
      log(msg, "e");
      $("resultLine").textContent = msg;
    }
  } finally {
    exporting = false;
    $("doExport").disabled = false;
    $("cancelExport").disabled = true;
  }
}

// ---------------------------------------------------------------- 绑定
function bindUI() {
  $("pickVideo").onclick = async () => {
    try {
      const p = await invoke("workshop_pick_video");
      if (p) await addVideo(p);
    } catch (e) {
      if (String(e).includes("未选择")) return;
      log(`选择视频失败：${e}`, "e");
    }
  };
  $("removeVideo").onclick = () => {
    if (!VIDEOS.length) return log("还没有源视频", "w");
    removeVideo();
  };

  $("videoList").onclick = (e) => {
    const rm = e.target.closest("[data-rm]");
    if (rm) {
      e.stopPropagation();
      return removeVideo();
    }
    const it = e.target.closest(".vitem");
    if (it) {
      const i = Number(it.dataset.i);
      if (i !== VINDEX) {
        VINDEX = i;
        renderVideoList();
        loadVideo(VIDEOS[i].path).catch((err) => log(`切换视频失败：${err}`, "e"));
      }
    }
  };

  $("cancelExport").onclick = async () => {
    if (!exporting) return;
    log("已请求取消（解码阶段需等当前解码结束，逐帧合成阶段会立即停）", "w");
    try { await invoke("workshop_cancel_export"); } catch (e) { log(`取消失败：${e}`, "e"); }
  };
  $("openOut").onclick = async () => {
    try {
      await invoke("workshop_open_dir", { path: S.outDir || null });
    } catch (e) {
      log(`打开目录失败：${e}`, "e");
    }
  };

  $("seek").oninput = () => {
    if (!S.info) return;
    S.at = Number($("seek").value);
    syncFrameLabel();
    scheduleSeek();
  };
  let seekTimer = null;
  function scheduleSeek() {
    clearTimeout(seekTimer);
    seekTimer = setTimeout(async () => { await loadRaw(); await preview(); }, PREVIEW_DEBOUNCE);
  }

  $("btnPlay").onclick = () => setPlaying(true);
  $("btnPause").onclick = () => setPlaying(false);
  addEventListener("keydown", (e) => {
    if (e.code === "Space" && !e.target.closest("input,select,button")) {
      e.preventDefault();
      setPlaying(!playing);
    }
  });

  $("autoCrop").onclick = async () => {
    if (!S.path) return;
    try {
      const r = await invoke("workshop_auto_crop", { path: S.path, at: S.at });
      if (!r.crop) return log(`自动裁边没找到暗边（${r.detail}），保持当前裁切`, "w");
      setCrop(r.crop, false);
      S.dirty = true;
      await preview();
      log(`重跑裁边：L${r.crop[0]} T${r.crop[1]} R${r.crop[2]} B${r.crop[3]}，可继续微调`, "s");
    } catch (e) { log(`自动裁边失败：${e}`, "e"); }
  };

  // 画布工具：裁切 / 加取色框 / 矩形擦除 / 清空标注
  const tools = [["crop", $("toolCrop")], ["sample", $("toolSample")], ["erase", $("toolErase")]];
  const setMode = (mode) => {
    S.mode = mode;
    S.rubber = null;
    for (const [m, btn] of tools) btn.classList.toggle("on", m === mode);
    paintStage();
  };
  for (const [m, btn] of tools) btn.onclick = () => setMode(m);
  $("clearMarks").onclick = () => {
    const v = curRow();
    if (!v) return;
    v.samples = [];
    v.erases = [];
    S.dirty = true;
    updateMarks();
    paintStage();
    preview();
    log("已清空全部取色框 + 矩形擦除框", "w");
  };

  $("autoSample").onclick = async () => {
    if (!S.path) return;
    try {
      const r = await invoke("workshop_auto_sample", { path: S.path, at: S.at, crop: S.crop });
      await applySampled(r);
      log(`当前帧取色：hue_ref=${r.hue_ref}° 背景饱和 ${r.sat_med}→ sat_lo=${r.sat_lo} sat_hi=${r.sat_hi}（${r.samples} 像素）`, "s");
    } catch (e) { log(`当前帧取色失败：${e}`, "e"); }
  };

  // 多帧取色：抽 3 帧各自取色，色相/饱和度取中位数后回填（近似 Python 的多帧闸门）
  $("multiSample").onclick = () => autoKeyMulti(false);

  $("resetParams").onclick = async () => { setParamsDefaults(); await preview(); };

  for (const el of document.querySelectorAll("#advBox input[type=range], .slider input[type=range]")) {
    el.oninput = () => { syncParamLabels(); schedulePreview(); };
  }
  $("keepMain").onchange = () => schedulePreview();
  $("hue_ref").oninput = () => { syncParamLabels(); schedulePreview(); };

  $("advBtn").onclick = () => {
    const on = $("advBox").classList.toggle("on");
    $("advBtn").textContent = `${on ? "▾" : "▸"} 更多参数（色相 / 地板 / 去溢色 / 预设）`;
  };

  $("cbSolid").onchange = async () => { S.solid = $("cbSolid").checked; await preview(); };

  $("clipId").oninput = updateIdStatus;
  $("clipName").onchange = () => { if (!$("clipId").value.trim()) refreshClipId(); };

  $("seriesSel").onchange = () => { preview(); };
  $("newSeries").onclick = async () => {
    const name = prompt("新主题名称（例如 女仆装）：");
    if (!name || !name.trim()) return;
    try {
      const id = await invoke("workshop_new_series", { name: name.trim() });
      await loadSeries();
      $("seriesSel").value = id;
      log(`已新建主题 ${id}`, "s");
    } catch (e) { log(`新建主题失败：${e}`, "e"); }
  };
  $("doExport").onclick = doExport;

  // 状态下拉
  $("stateSel").innerHTML = `<option value="">不绑定</option>` +
    STATES.map((s) => `<option value="${s}">${STATE_CN[s]} (${s})</option>`).join("");

  window.__wsOriginChanged = applyOrigin;
}

async function applyOrigin() {
  try {
    const o = await invoke("workshop_origin");
    if (!o) return;
    if (o.clip) { $("clipId").value = o.clip; updateIdStatus(); }
    if (o.state) $("stateSel").value = o.state;
    if (o.theme) {
      await loadSeries();
      if (seriesMap.has(o.theme)) $("seriesSel").value = o.theme;
    }
  } catch { /* 启动早期还没就绪，忽略 */ }
}

async function boot() {
  bindUI();
  setParamsDefaults();
  updateIdStatus();
  // 供自动化探针直接传入本地路径（正式入口仍是「添加视频…」的原生对话框）
  window.__wsAddVideo = addVideo;
  await loadSeries();
  await applyOrigin();
  // 已有动画 id 供参考
  try {
    const list = (await invoke("workshop_assets")) || [];
    const dl = document.createElement("datalist");
    dl.id = "clipIds";
    for (const a of list.slice(0, 200)) {
      const opt = document.createElement("option");
      opt.value = a.id;
      dl.appendChild(opt);
    }
    document.body.appendChild(dl);
    $("clipId").setAttribute("list", "clipIds");
  } catch { /* 忽略 */ }
  paintStage();
  paintRawView();
  paintAlphaView();
  log("工坊就绪：先「添加视频…」，再自动裁边/取色，最后导出。");
}

boot();
