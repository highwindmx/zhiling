"""素材包（pack）读写：packs/<packId>/manifest.json 是权威清单，clips.json 是运行时索引。

约定（详见 docs/material-packs.md）：
- packs/<packId>/manifest.json   角色名、画布、license、每个 clip 的 scale/offset/anchor/loop/priority
- packs/<packId>/frames/<clipId>/ 帧序列（webp/png），路径相对 pack 根
- packs/themes/<id>.json          系列主题映射（13 状态 → clipIds[]；v2 当前格式）
- clips.json 由 manifest 派生（deskpet-python/clips.json），两版桌宠都只读它，字段语义不分叉
"""
import glob, json, os, shutil, sys, time

BASE = os.path.dirname(os.path.abspath(__file__))       # build/tools/
BUILD = os.path.dirname(BASE)                            # build/
ROOT = os.path.dirname(BUILD)                            # deskpet-python/
REPO = os.path.dirname(ROOT)                             # 仓库根

# ---------------- 素材目录统一解析（所有模块只走这里，别再各写一套） ----------------
# 解析顺序（先命中先用；不存在的候选跳过并记一条告警，绝不因为目录缺失而崩）：
#   1. 环境变量 DESKPET_PACKS（便携版 / 测试隔离用）
#   2. config.json 的 packsDir（设置窗「素材目录」，优先级高于默认位置）
#   3. %LOCALAPPDATA%\DeskPet\packs（默认位置）
#   4. 脚本旁 deskpet-python/packs（老布局）
#   5. 仓库根 zhiling/packs（Python / Tauri 共用布局）
_WARNED_MSGS = set()
_WARN_SINK = None
_CONFIG_CACHE = {"sig": None, "data": {}}


def set_warn_sink(fn):
    """把「素材目录回落」告警接到调用方日志（pet.py 的 log）；不设就只写 stderr。"""
    global _WARN_SINK
    _WARN_SINK = fn


def _warn(msg):
    """同一条告警每进程只记一次，避免 1s 轮询把日志刷爆。"""
    if msg in _WARNED_MSGS:
        return
    _WARNED_MSGS.add(msg)
    sink = _WARN_SINK
    if callable(sink):
        try:
            sink(msg)
        except Exception:                   # noqa: BLE001 - 日志失败不能影响主流程
            pass
    if sys.stderr is not None:              # pythonw 下 stderr 为 None
        print(msg, file=sys.stderr)


# ---------------- 配置文件统一解析（Python 与 Tauri 共用同一份） ----------------
# 默认 %LOCALAPPDATA%\DeskPet\config.json；环境变量 DESKPET_CONFIG 可整体覆盖
# （便携版 / 测试隔离用）。旧版 deskpet-python\config.json 首次访问时合并进新文件：
# 新文件已有键优先，旧文件只补缺失键；迁移后旧文件改名为 config.json.migrated，
# 之后不再读写它。
CONFIG_ENV = "DESKPET_CONFIG"
LEGACY_CONFIG = os.path.join(ROOT, "config.json")
_CONFIG_MIGRATED = False


def default_config_path():
    """默认配置：%LOCALAPPDATA%\\DeskPet\\config.json（与 Tauri 版同一份）。"""
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "DeskPet", "config.json")


def config_path():
    """最终配置路径：DESKPET_CONFIG → 默认 LOCALAPPDATA 位置。"""
    env = os.environ.get(CONFIG_ENV, "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(default_config_path())


def _read_json_file(path):
    """读一个 JSON 对象；缺失 / 坏文件 / 非对象一律当空字典。"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_json(path, data):
    """临时文件 + 原子替换：半截文件永远不会被读到；目录不存在时自动建。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def migrate_legacy_config():
    """旧 deskpet-python\\config.json → 新配置，每进程只做一次。

    - 新文件已有键优先，旧文件只补缺失键（不覆盖 Tauri 版写的值）
    - DESKPET_CONFIG 显式覆盖时不动旧文件（测试/便携场景保持隔离）
    - 迁移成功后旧文件改名为 config.json.migrated，避免后续复活旧键
    """
    global _CONFIG_MIGRATED
    if _CONFIG_MIGRATED:
        return
    _CONFIG_MIGRATED = True
    if os.environ.get(CONFIG_ENV, "").strip():
        return
    target = os.path.abspath(default_config_path())
    legacy = os.path.abspath(LEGACY_CONFIG)
    if legacy == target or not os.path.isfile(legacy):
        return
    new = _read_json_file(target)
    old = _read_json_file(legacy)
    filled = [k for k in old if k not in new]
    merged = dict(new)
    for k in filled:
        merged[k] = old[k]
    if not os.path.exists(target) or filled:
        try:
            _atomic_json(target, merged)
        except OSError as e:
            _warn("config 迁移失败：%s → %s err=%s" % (legacy, target, e))
            return
        _warn("config 已迁移：%s → %s（新文件优先，旧文件补齐 %d 个键：%s）"
              % (legacy, target, len(filled), ", ".join(filled) or "无"))
    else:
        _warn("config 无需合并：%s（旧文件没有新键，已停用 %s）" % (target, legacy))
    try:
        os.replace(legacy, legacy + ".migrated")
    except OSError as e:
        _warn("config 旧文件改名失败：%s err=%s" % (legacy, e))


def user_config():
    """读配置（默认 %LOCALAPPDATA%\\DeskPet\\config.json）；按 mtime+size 缓存。"""
    migrate_legacy_config()
    p = config_path()
    try:
        st = os.stat(p)
        sig = (p, st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _CONFIG_CACHE["sig"] == sig:
        return dict(_CONFIG_CACHE["data"])
    data = _read_json_file(p)
    _CONFIG_CACHE["sig"] = sig
    _CONFIG_CACHE["data"] = data
    return dict(data)


def _deep_merge(base, patch):
    """深度合并：patch 里的对象逐键并入 base，其它类型直接覆盖（与 Tauri merge_json 一致）。"""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def update_user_config(patch):
    """读-改-写：只覆盖 patch 里的键，原样保留不认识的键（Tauri / 其它工具写的），
    落到临时文件再原子替换。嵌套对象做深度合并（与 Tauri save_config 对齐）。
    返回合并后的完整配置。
    """
    migrate_legacy_config()
    p = config_path()
    data = _read_json_file(p)
    if isinstance(patch, dict):
        _deep_merge(data, patch)
    _atomic_json(p, data)
    _CONFIG_CACHE["sig"] = None          # 下次 user_config() 重新读盘
    _CONFIG_CACHE["data"] = {}
    return data


def config_packs_dir():
    v = user_config().get("packsDir")
    return v.strip() if isinstance(v, str) else ""


def default_packs_dir():
    """默认素材目录：%LOCALAPPDATA%\\DeskPet\\packs（拿不到环境变量时退回用户目录同构路径）。"""
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "DeskPet", "packs")


def packs_candidates():
    """按优先级列出候选目录：[{source, path, exists}]（空值候选直接省略）。"""
    env = os.environ.get("DESKPET_PACKS", "").strip()
    cfg = config_packs_dir()
    out = []
    for src, p in (("env:DESKPET_PACKS", env),
                   ("config:packsDir", cfg),
                   ("default:LocalAppData", default_packs_dir()),
                   ("local:deskpet-python", os.path.join(ROOT, "packs")),
                   ("repo:zhiling", os.path.join(REPO, "packs"))):
        if not p:
            continue
        p = os.path.abspath(os.path.expanduser(p))
        out.append({"source": src, "path": p, "exists": os.path.isdir(p)})
    return out


def packs_resolution():
    """解析结果：{path, source, candidates, warnings}。path 永不为空、永不抛。

    找不到任何存在的候选时给默认 LOCALAPPDATA 位置（首次写入时由 _atomic_json 建目录）。
    """
    cands = packs_candidates()
    chosen = next((c for c in cands if c["exists"]), None)
    warnings = []
    for c in cands:
        if c["source"] in ("env:DESKPET_PACKS", "config:packsDir") and not c["exists"]:
            warnings.append("%s 指向的素材目录不存在：%s（已回落到下一个候选）"
                            % (c["source"], c["path"]))
    if chosen is None:
        # 一个存在的候选都没有：给默认 LOCALAPPDATA 位置（首次写入时由 _atomic_json 建目录）
        dp = os.path.abspath(default_packs_dir())
        chosen = next((c for c in cands if c["path"] == dp), cands[-1])
    return {"path": chosen["path"], "source": chosen["source"],
            "candidates": cands, "warnings": warnings}


def packs_root():
    r = packs_resolution()
    for w in r["warnings"]:
        _warn(w)
    return r["path"]


# ---------------- ffmpeg 定位（Python 版抽帧） ----------------
# 不再读 ffmpegPath / DESKPET_FFMPEG，也不向共享配置写任何 ffmpeg 键；
# 顺序：imageio-ffmpeg 随依赖自带的二进制 → PATH 里的 ffmpeg。
FFMPEG_MISSING_HINT = (
    "抽帧不可用：未找到 ffmpeg。\n"
    "1) 抽帧依赖随 `uv sync` 一起安装（imageio-ffmpeg 自带二进制），执行 `uv sync` 重装依赖；\n"
    "2) 或安装 ffmpeg 并加入 PATH。\n"
    "仍可打开工坊浏览已有素材，但点「生成」会失败。"
)


def bundled_ffmpeg():
    """imageio-ffmpeg 随依赖安装的二进制；没装依赖时返回 None。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe() or None
    except Exception:                       # noqa: BLE001 - 可选依赖，缺失时回落到 PATH
        return None


def ffmpeg_candidates():
    """按优先级列出候选：[{source, path, exists}]。"""
    out = []
    p = bundled_ffmpeg()
    if p:
        out.append({"source": "imageio-ffmpeg", "path": os.path.abspath(p), "exists": True})
    w = shutil.which("ffmpeg")
    if w:
        out.append({"source": "PATH", "path": os.path.abspath(w), "exists": True})
    return out


def ffmpeg_resolution():
    """{path, source, candidates}；path=None 表示没找到，永不抛。"""
    cands = ffmpeg_candidates()
    chosen = cands[0] if cands else None
    if chosen is None:
        return {"path": None, "source": "none", "candidates": cands}
    return {"path": chosen["path"], "source": chosen["source"], "candidates": cands}


def ffmpeg_exe():
    """工坊抽帧用；两者都找不到时抛 RuntimeError（带 uv sync / PATH 指引）。"""
    r = ffmpeg_resolution()
    if not r["path"]:
        raise RuntimeError(FFMPEG_MISSING_HINT)
    return r["path"]


def themes_dir():
    return os.path.join(packs_root(), "themes")


def anim_dir():
    return os.path.join(packs_root(), "animations")


def active_json_path():
    return os.path.join(packs_root(), "active.json")


CACHE = os.path.join(BUILD, "cache")
DEFAULT_PACK = "maid-dsh"       # 多 pack 时由 packs/active.json 决定，这只是兜底
# 新建 pack 的默认画布；画布是 pack 级属性，加载时以 manifest 为准，不在这里硬编码
DEFAULT_CANVAS = (576, 530)
LICENSE_KINDS = ("cc0", "cc-by", "cc-by-nc", "all-rights-reserved", "unknown", "custom")


def list_packs():
    root = packs_root()
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if os.path.isdir(os.path.join(root, d)) and not d.startswith("."))


def active_pack():
    """当前启用的 pack：packs/active.json → 第一个带 clips.json 的 pack → 兜底。"""
    root = packs_root()
    p = os.path.join(root, "active.json")
    try:
        with open(p, encoding="utf-8") as f:
            pid = (json.load(f) or {}).get("pack")
        if pid and os.path.isdir(os.path.join(root, pid)):
            return pid
    except Exception:
        pass
    for d in list_packs():
        if os.path.exists(os.path.join(root, d, "clips.json")):
            return d
    packs = list_packs()
    return packs[0] if packs else DEFAULT_PACK


def set_active(pack):
    """写 packs/active.json（当前系列唯一真相）。

    原子写（tmp + os.replace）并**保留未知字段**：active.json 以后可能被其它工具
    加 version / theme 之类，切换系列不能把它们冲掉。
    """
    root = packs_root()
    os.makedirs(root, exist_ok=True)
    p = os.path.join(root, "active.json")
    data = {}
    try:
        with open(p, encoding="utf-8") as f:
            old = json.load(f)
        if isinstance(old, dict):
            data = old
    except Exception:                       # noqa: BLE001 - 缺失/坏文件都按空开始
        data = {}
    data["pack"] = pack
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)                      # 半截文件永远不会被读到
    return p


def pack_dir(pack=None):
    return os.path.join(packs_root(), pack or active_pack())


def frames_dir(pack, cid):
    return os.path.join(pack_dir(pack), "frames", cid)


def manifest_path(pack=None):
    return os.path.join(pack_dir(pack), "manifest.json")


def load_manifest(pack=None):
    p = manifest_path(pack)
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_manifest(pack, data):
    os.makedirs(pack_dir(pack), exist_ok=True)
    p = manifest_path(pack)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)                    # 原子替换：半截的文件不会被读到
    return p


def new_pack(pack, role, canvas=DEFAULT_CANVAS):
    if os.path.isdir(pack_dir(pack)):
        raise FileExistsError(pack_dir(pack))
    data = {"version": 1, "packId": pack, "charId": pack, "role": role,
            "canvas": {"w": int(canvas[0]), "h": int(canvas[1])},
            "frames": {"format": "webp", "quality": 80, "fps": 24},
            "defaultIdle": None,
            "license": {"kind": "unknown", "source": "", "note": ""},
            "clips": []}
    save_manifest(pack, data)
    with open(os.path.join(pack_dir(pack), "clips.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "defaultIdle": None, "clips": []}, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.join(pack_dir(pack), "frames"), exist_ok=True)
    os.makedirs(os.path.join(pack_dir(pack), "posters"), exist_ok=True)
    return data


def license_errors(data):
    """license 校验。返回错误列表；unknown 不阻塞本地运行，只在导出/发布时提示。

    - kind 必须是 LICENSE_KINDS 之一，缺省按 unknown 处理
    - custom 必须有 note
    - all-rights-reserved 必须有 source（来源不明的素材不能声称"保留所有权利"）
    """
    lic = data.get("license")
    if not isinstance(lic, dict) or not lic:
        return []                                   # 老 pack 没写 license：按 unknown，不阻塞
    kind = lic.get("kind", "unknown")
    if kind not in LICENSE_KINDS:
        return ["license.kind 非法: %r（可选 %s）" % (kind, "/".join(LICENSE_KINDS))]
    out = []
    if kind == "custom" and not str(lic.get("note") or "").strip():
        out.append("license.kind=custom 时必须写 license.note")
    if kind == "all-rights-reserved" and not str(lic.get("source") or "").strip():
        out.append("license.kind=all-rights-reserved 时必须写 license.source")
    return out


def license_hint(data):
    """返回给人看的 license 一行摘要；unknown 时带上"需要补来源"的提醒。"""
    lic = data.get("license") or {}
    kind = lic.get("kind", "unknown")
    src = lic.get("source") or "（未填写来源）"
    note = ("；" + lic["note"]) if lic.get("note") else ""
    if kind == "unknown":
        return "unknown（来源：%s%s）——发布/分发前请补 license.kind 与 source" % (src, note)
    return "%s（来源：%s%s）" % (kind, src, note)


def load_manifest_validated(pack=None):
    """读取 manifest 并校验 license；license 错误不阻塞（unknown 允许本地运行）。"""
    data = load_manifest(pack)
    for e in license_errors(data):
        raise ValueError("packs/%s 的 license 非法：%s" % (pack or active_pack(), e))
    return data


def check_canvas(pack, data, ext_order=("webp", "png")):
    """校验 pack 内所有 clip 的帧尺寸等于 manifest.canvas。

    只读每段第一帧的文件头（PIL.Image.open 不解码整帧），速度快；
    出现不一致时返回 [(clipId, 实际尺寸)]，由调用方报错——**不静默缩放**。
    """
    from PIL import Image
    canvas = data.get("canvas") or {}
    cw, ch = int(canvas.get("w", 0)), int(canvas.get("h", 0))
    if not cw or not ch:
        return [("manifest", "canvas 缺失")]
    bad = []
    for c in data.get("clips", []):
        cid = c.get("clipId") or "?"
        d = os.path.join(pack_dir(pack), c.get("dir") or ("frames/" + cid))
        path = None
        for ext in [c.get("format")] + list(ext_order):
            if not ext:
                continue
            p = os.path.join(d, "f0000." + ext)
            if os.path.exists(p):
                path = p
                break
        if not path:
            bad.append((cid, "缺帧目录/首帧（%s）" % d))
            continue
        try:
            with Image.open(path) as im:
                w, h = im.size
        except Exception as e:                      # noqa: BLE001
            bad.append((cid, "读不了首帧：%s" % e))
            continue
        if (w, h) != (cw, ch):
            bad.append((cid, "%dx%d" % (w, h)))
    return bad


def canvas_error_text(pack, canvas, bad):
    lines = ["packs/%s 的帧尺寸与 manifest.canvas=%dx%d 不一致：" % (pack, canvas[0], canvas[1])]
    for cid, got in bad:
        lines.append("  %-14s %s" % (cid, got))
    lines.append("同一 pack 内必须统一画布；跨 pack 的帧不能混用。改 manifest.canvas 或重新导出帧。")
    return "\n".join(lines)


def upsert_clip(pack, entry):
    """按 clipId 合并一条 clip（覆盖同 id，不产生 _v2 目录）。"""
    data = load_manifest(pack)
    arr = data.setdefault("clips", [])
    for i, old in enumerate(arr):
        if old.get("clipId") == entry["clipId"]:
            arr[i] = dict(old, **entry)
            break
    else:
        arr.append(entry)
    save_manifest(pack, data)
    return data


def backup_frames(pack, cid, keep=3):
    """重抠前把旧帧挪到 .backup/<cid>_<ts>/，保留最近 keep 份。"""
    d = frames_dir(pack, cid)
    if not os.path.isdir(d):
        return None
    files = glob.glob(os.path.join(d, "*"))
    if not files:
        return None
    dst = os.path.join(pack_dir(pack), ".backup", "%s_%s" % (cid, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(dst, exist_ok=True)
    for f in files:
        shutil.move(f, os.path.join(dst, os.path.basename(f)))
    # 只留最近 keep 份
    olds = sorted(glob.glob(os.path.join(pack_dir(pack), ".backup", cid + "_*")))
    for o in olds[:-keep]:
        shutil.rmtree(o, ignore_errors=True)
    return dst


def sync_runtime(pack=None, dst=None):
    """manifest → packs/<pack>/clips.json（段级清单）+ deskpet-python/clips.json（runtime 索引）。

    两版桌宠都只读 clips.json；raw 目录结构不再扁平到 deskpet-python 下。
    """
    pack = pack or active_pack()
    data = load_manifest(pack)
    clips = []
    for c in data.get("clips", []):
        clips.append({
            "id": c["clipId"],
            "displayName": c.get("displayName") or c["clipId"],
            "type": c.get("type", "action"),
            "loop": bool(c.get("loop", False)),
            "priority": int(c.get("priority", 0)),
            "dir": c.get("dir") or ("frames/" + c["clipId"]),
            "fps": c.get("fps", 24),
            "count": c.get("count", 0),
        })
    out = {"version": 1, "defaultIdle": data.get("defaultIdle") or (clips[0]["id"] if clips else None),
           "canvas": data.get("canvas") or {"w": DEFAULT_CANVAS[0], "h": DEFAULT_CANVAS[1]},
           "clips": clips}

    def _write(path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    _write(os.path.join(pack_dir(pack), "clips.json"))
    if pack == active_pack():                 # 只有当前 pack 才更新运行时索引
        _write(dst or os.path.join(ROOT, "clips.json"))
    return out


# ---------------- 多系列（multi-pack）统一解析 ----------------
# 所有模块都走这里，别再各自拼 packs/<id>/ 的路径。
STATE_MAP_NAME = "state-map.json"
PACK_ID_RE = r"^[a-z][a-z0-9_-]{1,23}$"


def state_map_path(pack=None):
    return os.path.join(pack_dir(pack), STATE_MAP_NAME)


def empty_state_map(pack):
    """新系列没有状态映射时的空模板：结构齐全、规则为空，读端不需要特判。"""
    return {"version": 1, "packId": pack, "longIdleSec": 600,
            "awaitingUserSec": 30, "rules": []}


def load_clips(pack=None):
    """packs/<pack>/clips.json → {clipId: clip}；文件缺失/坏掉时从 manifest 推导。"""
    pack = pack or active_pack()
    p = os.path.join(pack_dir(pack), "clips.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f) or {}
        clips = {c["id"]: c for c in data.get("clips", []) if c.get("id")}
        if clips:
            return clips
    except Exception:                        # noqa: BLE001
        pass
    out = {}
    try:
        data = load_manifest(pack)
        canvas = data.get("canvas") or {}
        for c in data.get("clips", []):
            cid = c.get("clipId")
            if cid:
                out[cid] = {"id": cid,
                            "displayName": c.get("displayName") or cid,
                            "type": c.get("type", "action"),
                            "loop": bool(c.get("loop", False)),
                            "priority": int(c.get("priority", 0)),
                            "dir": c.get("dir") or ("frames/" + cid),
                            "fps": c.get("fps", 24), "count": c.get("count", 0)}
    except Exception:                        # noqa: BLE001
        pass
    return out


def resolve_pack(pack_id=None):
    """统一解析一个系列。**永不抛**：目录/清单缺失时 exists=False，其余字段给安全默认。

    返回 dict：
      id / dir / exists / manifest / clips(dict) / clip_count
      state_map_path / has_state_map / state_map(dict|空模板)
      canvas (w,h) / display_name / default_idle
    """
    pid = pack_id or active_pack()
    d = os.path.join(packs_root(), pid)
    exists = os.path.isdir(d)
    manifest = {}
    if exists and os.path.exists(os.path.join(d, "manifest.json")):
        try:
            manifest = load_manifest(pid)
        except Exception:                    # noqa: BLE001
            manifest = {}
    clips = load_clips(pid) if exists else {}
    canvas = manifest.get("canvas") or {}
    cw = int(canvas.get("w") or DEFAULT_CANVAS[0])
    ch = int(canvas.get("h") or DEFAULT_CANVAS[1])
    smp = os.path.join(d, STATE_MAP_NAME)
    has_sm = exists and os.path.exists(smp)
    sm = empty_state_map(pid)
    if has_sm:
        try:
            with open(smp, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                sm = loaded
        except Exception:                    # noqa: BLE001
            sm = empty_state_map(pid)
    return {
        "id": pid, "dir": d, "exists": exists,
        "manifest": manifest, "clips": clips, "clip_count": len(clips),
        "state_map_path": smp, "has_state_map": has_sm, "state_map": sm,
        "canvas": (cw, ch),
        "display_name": manifest.get("role") or pid,
        "default_idle": manifest.get("defaultIdle"),
    }


def list_pack_infos():
    """工坊/设置的下拉用：[{id, display_name, canvas, clip_count, has_state_map}]。"""
    out = []
    for pid in list_packs():
        p = resolve_pack(pid)
        out.append({"id": pid, "display_name": p["display_name"],
                    "canvas": p["canvas"], "clip_count": p["clip_count"],
                    "has_state_map": p["has_state_map"]})
    return out


def split_ref(ref):
    """"<packId>:<clipId>" → (packId, clipId)；普通 clipId → (None, clipId)。"""
    s = str(ref or "")
    if ":" in s:
        pid, cid = s.split(":", 1)
        if pid and cid:
            return pid, cid
    return None, s


def resolve_clip(ref, default_pack=None):
    """把（可能跨系列的）clip 引用解析成实际帧目录。解析不到时 clip 为 None，不抛。"""
    pid, cid = split_ref(ref)
    pid = pid or default_pack or active_pack()
    p = resolve_pack(pid)
    clip = p["clips"].get(cid)
    return {"pack": pid, "pack_id": pid, "clip_id": cid, "clip": clip,
            "dir": os.path.join(p["dir"], "frames", cid),
            "canvas": p["canvas"], "exists": bool(clip) and p["exists"]}


def write_state_map(pack, data):
    """只写指定系列的 `themes/<id>.json`（原子写）；仅用于迁移/兼容旧调用。"""
    p = state_map_path(pack)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)
    return p


def create_pack(new_id, display_name, canvas=DEFAULT_CANVAS, template=None, copy_map=True):
    """新建系列。返回 (info, report)：

    report = {"matched": [状态名], "unmatched": [状态名]} —— 复制模板状态映射时
    只有 clipId 在新系列里也存在才继承，否则留空（UI 标黄）。
    """
    import re as _re
    if not _re.match(PACK_ID_RE, new_id or ""):
        raise ValueError("主题 id 必须匹配 %s（小写字母开头，2–24 位）" % PACK_ID_RE)
    if os.path.isdir(pack_dir(new_id)):
        raise FileExistsError("主题已存在：%s" % pack_dir(new_id))
    new_pack(new_id, display_name or new_id, canvas)
    sm = empty_state_map(new_id)
    matched, unmatched = [], []
    if copy_map and template:
        tpl = resolve_pack(template)
        src = tpl["state_map"] or {}
        new_clips = load_clips(new_id)
        rules = []
        for r in src.get("rules", []):
            r = dict(r)
            cid = r.get("preferredClipId") or r.get("clipId") or ""
            base = cid.split(":", 1)[1] if ":" in cid else cid
            if base and base in new_clips:
                matched.append(r.get("state") or "?")
            elif cid:
                # 模板里指向别的系列/本系列没有的 clip：留空，等新系列生成同名 clip 后再选
                r["preferredClipId"] = ""
                unmatched.append(r.get("state") or "?")
            rules.append(r)
        sm["rules"] = rules
        for k in ("longIdleSec", "awaitingUserSec"):
            if k in src:
                sm[k] = src[k]
        sm["packId"] = new_id
        write_state_map(new_id, sm)
    else:
        write_state_map(new_id, sm)
    return resolve_pack(new_id), {"matched": matched, "unmatched": unmatched}


def migrate_from_legacy(pack=DEFAULT_PACK):
    """把 deskpet-python/clips.json + meta.json 迁成 packs/<pack>/manifest.json（一次性）。"""
    clips = json.load(open(os.path.join(ROOT, "clips.json"), encoding="utf-8"))
    meta = json.load(open(os.path.join(ROOT, "meta.json"), encoding="utf-8"))
    mclips = meta.get("clips", {})
    canvas = meta.get("canvas", {"w": DEFAULT_CANVAS[0], "h": DEFAULT_CANVAS[1]})
    data = {"version": 1, "packId": pack, "role": "蓝鲸女仆（DSMaid）",
            "canvas": {"w": canvas["w"], "h": canvas["h"]},
            "frames": {"format": "webp", "quality": 80, "fps": 24},
            "defaultIdle": clips.get("defaultIdle", "dsh_idle"),
            "license": {"kind": "unknown", "source": "jimeng / hailuo（HappyHorse 1.1）",
                        "note": "生成工具商用条款未核实"},
            "clips": []}
    for c in clips["clips"]:
        m = mclips.get(c["id"], {})
        data["clips"].append({
            "clipId": c["id"], "displayName": c.get("displayName") or c.get("name", c["id"]),
            "type": c.get("type", "action"), "loop": bool(c.get("loop")),
            "priority": int(c.get("priority", 0)),
            "dir": "frames_" + c["id"], "fps": m.get("fps", 24), "count": m.get("count", 0),
            "scale": m.get("scale", 1.0), "offset": m.get("offset", [0, 0]),
            "anchor": {"foot": [0.5, (m.get("foot") or [canvas["w"] // 2, canvas["h"] - 2])[1] / canvas["h"]]},
            "tags": [], "crop": m.get("crop"),
            "source": m.get("source", ""),
            "keyer": m.get("keyer", {}),
        })
    save_manifest(pack, data)
    return data


# =====================================================================
# v2：主题 = 独立绑定索引；动画 = 自描述文件夹
# ---------------------------------------------------------------------
# packs/
#   themes/<themeId>.json       纯绑定索引（状态 → clipIds[] + 调度参数）
#                               + "active": true/false —— 当前启用哪个主题（唯一真相）
#   animations/<assetId>/
#       meta.json               名称 / 画布 / fps / 帧数 / 格式 / 来源
#       f0000.webp ...
#   active.json                 可选的兼容镜像，v2 代码不再依赖；存在时随切换同步，
#                               仅供 Tauri prepare_assets.py / v1 工具读。删掉也能跑。
# 旧 packs/<packId>/ 目录原样保留，仅供迁移对照与回滚，v2 代码不再读取。
# v1 函数（上半部分）保留给历史工具脚本，新代码一律走本节 v2 API。
# =====================================================================
# 目录一律通过 themes_dir() / anim_dir() / active_json_path() 现取，
# 不再在导入时把 packs 根固定成常量（设置里改素材目录后无需改代码）。
# 13 行状态（long_idle 拆成两档，各自可绑 0~N 段；focus/focus_break 为番茄钟新增），
# 顺序 = 优先级从高到低
STATE_KEYS = (
    "double_click", "click", "error", "needs_decision", "done",
    "busy", "focus", "streaming", "awaiting_user", "focus_break",
    "long_idle_2", "long_idle_1", "idle",
)
STATE_CN_V2 = {
    "idle": "待机", "long_idle_1": "长时间空闲 ①", "long_idle_2": "长时间空闲 ②",
    "awaiting_user": "等你回复", "busy": "忙碌", "streaming": "输出中",
    "done": "完成", "needs_decision": "待决策", "error": "出错",
    "click": "单击", "double_click": "双击",
    "focus": "专注", "focus_break": "休息",
}
# 出厂默认优先级（数字），保存时按列表顺序等距重算，这里只作初始模板
DEFAULT_PRIORITY = {
    "double_click": 85, "click": 70, "error": 60, "needs_decision": 55,
    "done": 45, "busy": 35, "focus": 32, "streaming": 30, "awaiting_user": 25,
    "focus_break": 22, "long_idle_2": 12, "long_idle_1": 10, "idle": 0,
}
DEFAULT_LONG_IDLE_AFTER = {"long_idle_1": 600, "long_idle_2": 1800}
V2_DEFAULT_CANVAS = (576, 530)
_ASSET_CACHE = {"sig": None, "data": None}


def _atomic_json(path, data):
    """原子写 JSON：半截文件永远不会被读到；保留调用方传进来的未知字段。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                       # noqa: BLE001 - 缺失/坏文件都给默认
        return {} if default is None else default


# ---- 主题 ----------------------------------------------------------------
def _active_from_themes():
    """扫描 themes/*.json 的 active 标记；多个为真时取「最近写入」的那个。

    切换主题是先写新主题 active=true、再清旧主题。万一进程在两步之间被杀，
    会短暂出现两个 true；用 mtime_ns 裁定，最新写入者胜出，保证任何时刻都有
    唯一确定的赢家，不需要额外的事务文件。
    """
    if not os.path.isdir(themes_dir()):
        return ""
    marked = []
    for fn in sorted(os.listdir(themes_dir())):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        p = os.path.join(themes_dir(), fn)
        d = _read_json(p)
        if isinstance(d, dict) and d.get("active") is True:
            try:
                mt = os.stat(p).st_mtime_ns
            except OSError:
                mt = 0
            marked.append((mt, fn[:-5]))
    if not marked:
        return ""
    marked.sort(reverse=True)
    return marked[0][1]


def active_theme():
    """当前主题：主题文件里的 active 标记（唯一真相）→ active.json 兼容镜像 → 第一个主题 → 兜底。"""
    tid = _active_from_themes()
    if tid and os.path.exists(theme_path(tid)):
        return tid
    # 软迁移兜底：老树/手工拷贝进来的主题还没写 active 字段时，仍认 active.json。
    d = _read_json(active_json_path())
    tid = d.get("theme") or d.get("pack")
    if tid and os.path.exists(theme_path(tid)):
        return tid
    ts = list_themes()
    return ts[0]["id"] if ts else DEFAULT_PACK


def set_active_theme(theme):
    """把启用状态写进主题文件（互斥）；active.json 只在已存在时作为镜像同步。"""
    if not theme or not os.path.exists(theme_path(theme)):
        return ""
    # 1) 先置新主题：中途被打断也有唯一赢家（见 _active_from_themes）
    d = load_theme(theme)
    d["active"] = True
    write_theme(theme, d)
    # 2) 其余主题全部置 false；读原始文件避免 load_theme 补默认值造成多余改写
    for fn in sorted(os.listdir(themes_dir())):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        p = os.path.join(themes_dir(), fn)
        if fn[:-5] == theme:
            continue
        o = _read_json(p)
        if isinstance(o, dict) and o.get("active") is True:
            o["active"] = False
            _atomic_json(p, o)
    # 3) 兼容镜像：文件在就同步（Tauri / v1 工具），已删除则不重建——v2 不依赖它
    if os.path.exists(active_json_path()):
        m = _read_json(active_json_path())
        if not isinstance(m, dict):
            m = {}
        m["theme"] = theme
        m["pack"] = theme
        _atomic_json(active_json_path(), m)
    invalidate_cache()
    return theme


def theme_path(tid):
    return os.path.join(themes_dir(), "%s.json" % tid)


def list_themes():
    """[{id, name, state_count, bound_count, has_file}]，供设置/右键菜单下拉。"""
    out = []
    if not os.path.isdir(themes_dir()):
        return out
    for fn in sorted(os.listdir(themes_dir())):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        tid = fn[:-5]
        d = _read_json(os.path.join(themes_dir(), fn))
        states = d.get("states") or {}
        out.append({"id": tid, "name": d.get("name") or tid,
                    "active": d.get("active") is True,
                    "state_count": len(states),
                    "bound_count": sum(1 for v in states.values()
                                       if isinstance(v, dict) and (v.get("clipIds") or [])),
                    "has_file": True})
    return out


def load_theme(tid=None):
    """读主题索引；缺失/坏文件返回结构齐全的空主题（读端不需要特判）。"""
    tid = tid or active_theme()
    d = _read_json(theme_path(tid))
    if not isinstance(d, dict) or not d:
        return empty_theme(tid)
    d.setdefault("id", tid)
    d.setdefault("name", tid)
    d.setdefault("version", 2)
    d.setdefault("states", {})
    d.setdefault("longIdleSec", 600)
    d.setdefault("awaitingUserSec", 30)
    d.setdefault("active", False)       # 缺字段的老主题不误判为启用；保存时会带回，不丢标记
    return d


def write_theme(tid, data):
    data = dict(data)
    data["id"] = tid
    data["version"] = 2
    invalidate_cache()
    return _atomic_json(theme_path(tid), data)


def theme_bak_path(tid):
    """单份备份：与主题文件同名 + .bak（旧方案的时间戳备份已废弃）。"""
    return theme_path(tid) + ".bak"


def backup_theme(tid):
    """保存前调用：把现有主题文件覆盖到唯一一份 .bak；没有源文件时跳过。"""
    src = theme_path(tid)
    if not os.path.isfile(src):
        return None
    bak = theme_bak_path(tid)
    try:
        shutil.copy2(src, bak)
    except OSError:
        return None
    return bak


def empty_theme(tid, name=None, idle_asset=None):
    """纯空映射：11 状态结构齐全、clipIds 全空；可给 idle 预填一段。"""
    states = {}
    for st in STATE_KEYS:
        states[st] = {
            "clipIds": [idle_asset] if (st == "idle" and idle_asset) else [],
            "fallbackClipIds": [],
            "priority": DEFAULT_PRIORITY[st],
            "minPlayMs": 0, "cooldownSec": 0,
            "loop": st in ("idle", "busy", "streaming", "focus", "focus_break"),
            "interruptible": True,
        }
        if st in DEFAULT_LONG_IDLE_AFTER:
            states[st]["afterSec"] = DEFAULT_LONG_IDLE_AFTER[st]
        if st in ("awaiting_user", "needs_decision"):
            states[st]["repeat"] = {"mode": "once", "intervalSec": 75, "maxTimes": 1}
    return {"version": 2, "id": tid, "name": name or tid, "active": False,
            "longIdleSec": 600, "awaitingUserSec": 30, "states": states}


def theme_add_clip(tid, state, asset_id):
    """工坊生成完回写：把一段动画追加到目标状态的 clipIds（去重、不覆盖手选）。"""
    if state not in STATE_KEYS:
        return False
    d = load_theme(tid)
    st = d["states"].setdefault(state, {"clipIds": []})
    pool = st.setdefault("clipIds", [])
    if asset_id not in pool:
        pool.append(asset_id)
    return write_theme(tid, d)


# ---- 动画资产 ------------------------------------------------------------
def asset_dir(aid):
    return os.path.join(anim_dir(), aid)


def asset_meta_path(aid):
    return os.path.join(asset_dir(aid), "meta.json")


def _asset_sig():
    """目录签名：只看 meta.json 的 mtime_ns + size，扫 2155 帧太贵。"""
    sig = []
    if os.path.isdir(anim_dir()):
        for d in sorted(os.listdir(anim_dir())):
            p = asset_meta_path(d)
            try:
                st = os.stat(p)
                sig.append((d, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
    return tuple(sig)


def list_assets(force=False):
    """扫 animations/*/meta.json → {assetId: meta}。按签名做进程内缓存。"""
    sig = _asset_sig()
    if not force and _ASSET_CACHE["sig"] == sig and _ASSET_CACHE["data"] is not None:
        return dict(_ASSET_CACHE["data"])
    out = {}
    for d, _m, _s in sig:
        meta = _read_json(asset_meta_path(d))
        if not isinstance(meta, dict) or not meta:
            continue
        aid = meta.get("assetId") or d
        meta.setdefault("assetId", aid)
        meta.setdefault("name", aid)
        meta.setdefault("dir", asset_dir(aid))
        meta.setdefault("canvas", {"w": V2_DEFAULT_CANVAS[0], "h": V2_DEFAULT_CANVAS[1]})
        meta.setdefault("fps", 24)
        meta.setdefault("count", 0)
        meta.setdefault("format", "webp")
        meta.setdefault("loop", False)
        out[aid] = meta
    _ASSET_CACHE["sig"] = sig
    _ASSET_CACHE["data"] = dict(out)
    return out


def load_asset(aid):
    return list_assets().get(aid)


def write_asset(aid, meta):
    meta = dict(meta)
    meta["assetId"] = aid
    meta["version"] = 2
    meta.setdefault("dir", asset_dir(aid))
    invalidate_cache()
    return _atomic_json(asset_meta_path(aid), meta)


# ---------------- 随包示例素材（首次运行兜底，纳入版本管理） ----------------
SAMPLE_PACK_ID = "sample"


def sample_pack_dir():
    """随包示例：deskpet-python/sample_packs/sample（不在仓库根 packs/ 里）。"""
    return os.path.join(ROOT, "sample_packs", SAMPLE_PACK_ID)


def theme_is_sample(tid=None):
    """当前系列是否随包示例（主题里带 "sample": true，或 id 就是 sample）。"""
    tid = tid or active_theme()
    if tid == SAMPLE_PACK_ID:
        return True
    return bool(load_theme(tid).get("sample"))


def _theme_ids_in(root):
    td = os.path.join(root, "themes")
    if not os.path.isdir(td):
        return []
    return sorted(fn[:-5] for fn in os.listdir(td)
                  if fn.endswith(".json") and not fn.startswith("."))


def _idle_usable_in(root, tid):
    """该主题 idle 绑定的动画至少有一段帧（否则启动会因“idle 缺首帧”退出）。"""
    d = _read_json_file(os.path.join(root, "themes", tid + ".json"))
    pool = ((d.get("states") or {}).get("idle") or {}).get("clipIds") or []
    ad = os.path.join(root, "animations")
    for aid in pool:
        meta = _read_json_file(os.path.join(ad, aid, "meta.json"))
        try:
            count = int(meta.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        try:
            files = [f for f in os.listdir(os.path.join(ad, aid)) if f != "meta.json"]
        except OSError:
            files = []
        if files:
            return True
    return False


def has_usable_material(root=None):
    """目录里是否至少有一个主题能把 idle 放出来；root 为空时按现有解析顺序。"""
    root = root or packs_root()
    return any(_idle_usable_in(root, tid) for tid in _theme_ids_in(root))


def install_sample_pack():
    """把 sample_packs/sample 复制到默认 %LOCALAPPDATA%\\DeskPet\\packs。

    返回 (ok, msg)。只在没有可用素材、且解析结果就是默认位置时安装；
    逐文件复制，已存在的一律跳过，绝不覆盖用户数据。
    """
    src = sample_pack_dir()
    if not os.path.isdir(os.path.join(src, "themes")):
        return False, "随包示例不存在：%s" % src
    target = os.path.abspath(default_packs_dir())
    res = packs_resolution()
    if res["path"] != target:
        return False, ("已有更高优先级的素材目录（%s），不安装示例包" % res["path"])
    if has_usable_material(target):
        return False, "目标目录已有可用素材，跳过示例包"
    copied = 0
    try:
        for dirpath, _dirs, files in os.walk(src):
            rel = os.path.relpath(dirpath, src)
            dst_dir = target if rel == "." else os.path.join(target, rel)
            os.makedirs(dst_dir, exist_ok=True)
            for fn in files:
                dst = os.path.join(dst_dir, fn)
                if os.path.exists(dst):
                    continue                  # 已存在则跳过，不覆盖用户数据
                shutil.copy2(os.path.join(dirpath, fn), dst)
                copied += 1
    except OSError as e:
        return False, "示例包复制失败：%s（目标 %s）" % (e, target)
    invalidate_cache()
    return True, "示例包已安装到 %s（%d 个文件）" % (target, copied)


def backup_asset_frames(aid, keep=3):
    """重抠前把 animations/<aid>/ 的旧帧（不含 meta.json）挪到 .backup/<ts>/，保留最近 keep 份。

    旧帧是 NTFS 硬链接时 move 只摘掉新树这一侧的目录项，v1 旧树的帧完全不受影响。
    """
    d = asset_dir(aid)
    if not os.path.isdir(d):
        return None
    files = [f for f in glob.glob(os.path.join(d, "*"))
             if os.path.isfile(f) and os.path.basename(f) != "meta.json"]
    if not files:
        return None
    dst = os.path.join(d, ".backup", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dst, exist_ok=True)
    for f in files:
        shutil.move(f, os.path.join(dst, os.path.basename(f)))
    olds = sorted(glob.glob(os.path.join(d, ".backup", "*")))
    for o in olds[:-keep]:
        shutil.rmtree(o, ignore_errors=True)
    return dst


def invalidate_cache():
    _ASSET_CACHE["sig"] = None
    _ASSET_CACHE["data"] = None


# ---- 画布：全程整数，禁止浮点比较 ----------------------------------------
def aspect_pair(canvas):
    """画布 → (w, h) 整数对。同时接受 {"w","h"} 与 (w,h) 两种形式。

    meta.json 里是 dict；pet.py 的 CLIPS 里为省事存整数对，两条路径都要能过。
    """
    if isinstance(canvas, (tuple, list)) and len(canvas) >= 2:
        return (int(canvas[0] or V2_DEFAULT_CANVAS[0]),
                int(canvas[1] or V2_DEFAULT_CANVAS[1]))
    c = canvas or {}
    return (int(c.get("w") or V2_DEFAULT_CANVAS[0]),
            int(c.get("h") or V2_DEFAULT_CANVAS[1]))


def same_aspect(a, b):
    """两段画布是否同比例：交叉相乘，纯整数比较。

    绝不用 `ch/cw` 或 `abs(a-b) < eps`：576/530 与 1152/1060 写成浮点后
    可能差 1 ulp，误判成「不同比例」就会在切段时白白 resize 一次（桌宠跳一下）。
    w1*h2 == w2*h1 在整数域精确，且天然把 2 倍分辨率视作同比例。
    """
    w1, h1 = aspect_pair(a)
    w2, h2 = aspect_pair(b)
    return w1 * h2 == w2 * h1


def display_height(width_px, canvas):
    """窗口高度：round(width_px * h / w) 的纯整数实现（无浮点、无 banker's rounding）。"""
    w, h = aspect_pair(canvas)
    return (2 * width_px * h + w) // (2 * w)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        d = migrate_from_legacy(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PACK)
        print("写入 %s，%d 段" % (manifest_path(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PACK),
                                len(d["clips"])))
        out = sync_runtime(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PACK)
        print("clips.json 已同步：%d 段，defaultIdle=%s" % (len(out["clips"]), out["defaultIdle"]))
    else:
        print("packs:", list_packs())
