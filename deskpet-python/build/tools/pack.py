"""素材包（pack）读写：packs/<packId>/manifest.json 是权威清单，clips.json 是运行时索引。

约定（详见 docs/material-packs.md）：
- packs/<packId>/manifest.json   角色名、画布、license、每个 clip 的 scale/offset/anchor/loop/priority
- packs/<packId>/frames/<clipId>/ 帧序列（webp/png），路径相对 pack 根
- packs/<packId>/state-map.json  状态映射（可缺省，缺省用内置默认）
- clips.json 由 manifest 派生（deskpet-python/clips.json），两版桌宠都只读它，字段语义不分叉
"""
import glob, json, os, shutil, time

BASE = os.path.dirname(os.path.abspath(__file__))       # build/tools/
BUILD = os.path.dirname(BASE)                            # build/
ROOT = os.path.dirname(BUILD)                            # deskpet-python/
REPO = os.path.dirname(ROOT)                             # 仓库根
# packs 跟 Python 版一起放在 deskpet-python/packs/（老仓库在仓库根，按优先级回退）
PACKS = next(
    (p for p in (os.path.join(ROOT, "packs"), os.path.join(REPO, "packs")) if os.path.isdir(p)),
    os.path.join(ROOT, "packs"))
CACHE = os.path.join(BUILD, "cache")
DEFAULT_PACK = "maid-dsh"       # 多 pack 时由 packs/active.json 决定，这只是兜底
# 新建 pack 的默认画布；画布是 pack 级属性，加载时以 manifest 为准，不在这里硬编码
DEFAULT_CANVAS = (576, 530)
LICENSE_KINDS = ("cc0", "cc-by", "cc-by-nc", "all-rights-reserved", "unknown", "custom")


def list_packs():
    if not os.path.isdir(PACKS):
        return []
    return sorted(d for d in os.listdir(PACKS)
                  if os.path.isdir(os.path.join(PACKS, d)) and not d.startswith("."))


def active_pack():
    """当前启用的 pack：packs/active.json → 第一个带 clips.json 的 pack → 兜底。"""
    p = os.path.join(PACKS, "active.json")
    try:
        with open(p, encoding="utf-8") as f:
            pid = (json.load(f) or {}).get("pack")
        if pid and os.path.isdir(os.path.join(PACKS, pid)):
            return pid
    except Exception:
        pass
    for d in list_packs():
        if os.path.exists(os.path.join(PACKS, d, "clips.json")):
            return d
    packs = list_packs()
    return packs[0] if packs else DEFAULT_PACK


def set_active(pack):
    os.makedirs(PACKS, exist_ok=True)
    p = os.path.join(PACKS, "active.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pack": pack}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def pack_dir(pack=None):
    return os.path.join(PACKS, pack or active_pack())


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
