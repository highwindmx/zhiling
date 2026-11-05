"""素材包（pack）读写：packs/<packId>/manifest.json 是权威清单，clips.json 是运行时索引。

约定（详见 docs/material-packs.md）：
- packs/<packId>/manifest.json   角色名、画布、license、每个 clip 的 scale/offset/anchor/loop/priority
- packs/<packId>/frames/<clipId>/ 帧序列（webp/png），路径相对 pack 根
- packs/<packId>/state-map.json  状态映射（可缺省，缺省用内置默认）
- clips.json 由 manifest 派生（deskpet-python/clips.json），两版桌宠都只读它，字段语义不分叉
"""
import glob, json, os, shutil, time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)              # deskpet-python/
REPO = os.path.dirname(ROOT)              # 仓库根
PACKS = os.path.join(REPO, "packs")
DEFAULT_PACK = "maid-dsh"       # 多 pack 时由 packs/active.json 决定，这只是兜底


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


def new_pack(pack, role, canvas=(576, 530)):
    if os.path.isdir(pack_dir(pack)):
        raise FileExistsError(pack_dir(pack))
    data = {"version": 1, "packId": pack, "charId": pack, "role": role,
            "canvas": {"w": canvas[0], "h": canvas[1]},
            "frames": {"format": "webp", "quality": 80, "fps": 24},
            "defaultIdle": None,
            "license": {"source": "", "license": "", "author": "", "url": ""},
            "clips": []}
    save_manifest(pack, data)
    with open(os.path.join(pack_dir(pack), "clips.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "defaultIdle": None, "clips": []}, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.join(pack_dir(pack), "frames"), exist_ok=True)
    os.makedirs(os.path.join(pack_dir(pack), "posters"), exist_ok=True)
    return data


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
    canvas = meta.get("canvas", {"w": 576, "h": 530})
    data = {"version": 1, "packId": pack, "role": "蓝鲸女仆（DSMaid）",
            "canvas": {"w": canvas["w"], "h": canvas["h"]},
            "frames": {"format": "webp", "quality": 80, "fps": 24},
            "defaultIdle": clips.get("defaultIdle", "dsh_idle"),
            "license": {"source": "DSMaid / 即梦（绿幕 mp4）", "license": "AI 生成，未授权商用",
                        "author": "DSMaid", "url": ""},
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
