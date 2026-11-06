"""旧扁平目录 → pack 结构的一次性迁移（幂等，可 --dry-run）。

旧：deskpet-python/frames_<id>/ + meta.json + clips.json
新：packs/maid-basic/{manifest.json,clips.json,frames/<id>/}
    packs/maid-dsh/{manifest.json,clips.json,frames/<id>/,state-map.json}

规则：
- 帧目录**移动**（不复制，避免同一份帧留两处）
- 旧 meta.json / clips.json / 旧 manifest 先备份到 packs/.legacy/
- 分段：dsh_* → maid-dsh，其余（idle/action/action2/action3）→ maid-basic
- 重复跑：源目录已不在、目标已存在 → 跳过，不报错
"""
import glob, json, os, shutil, sys, time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))              # deskpet-python/
REPO = os.path.dirname(ROOT)              # 仓库根
sys.path.insert(0, BASE)
import pack as packlib  # noqa: E402 - 素材目录统一解析（环境变量 / config / Documents / 就地）
PACKS = packlib.packs_root()
LEGACY = os.path.join(PACKS, ".legacy")
CANVAS = {"w": 576, "h": 530}
FRAMES_CONF = {"format": "webp", "quality": 80, "fps": 24}
GROUPS = {
    "maid-dsh": {"role": "蓝鲸女仆（DSMaid）", "match": lambda cid: cid.startswith("dsh_")},
    "maid-basic": {"role": "女仆（第一批）", "match": lambda cid: not cid.startswith("dsh_")},
}


def load_json(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def dump_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_atomic(p, data):
    tmp = p + ".tmp"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def clip_entry(c, meta, pid):
    m = meta.get(c["id"], {})
    foot = (m.get("foot") or [CANVAS["w"] // 2, CANVAS["h"] - 2])[1]
    name = c.get("displayName") or c.get("name") or c["id"]
    ctype = c.get("type", "action")
    loop = bool(c.get("loop", False))
    if pid == "maid-basic":
        name = name.replace("旧·", "")       # 在 basic 包里它就是正主，不用带「旧」字样
        if c["id"] == "idle":
            ctype, loop = "idle", True
    return {
        "clipId": c["id"],
        "displayName": name,
        "type": ctype,
        "loop": loop,
        "priority": int(c.get("priority", 0)),
        "dir": "frames/" + c["id"],
        "fps": m.get("fps", FRAMES_CONF["fps"]),
        "count": m.get("count", 0),
        "scale": m.get("scale", 1.0),
        "offset": m.get("offset", [0, 0]),
        "anchor": {"foot": [0.5, round(foot / CANVAS["h"], 4)]},
        "tags": [],
        "crop": m.get("crop"),
        "source": m.get("source", ""),
        "keyer": m.get("keyer", {}),
    }


def runtime_clips(data):
    return [{"id": c["clipId"], "displayName": c["displayName"], "type": c["type"],
             "loop": c["loop"], "priority": c["priority"], "dir": c["dir"],
             "fps": c["fps"], "count": c["count"]} for c in data["clips"]]


def main():
    dry = "--dry-run" in sys.argv
    old_clips = load_json(os.path.join(ROOT, "clips.json"))
    old_meta = load_json(os.path.join(ROOT, "meta.json"), {"clips": {}})
    if not old_clips:
        print("没有 deskpet-python/clips.json，跳过")
        return 0
    meta = old_meta.get("clips", {})
    plans = {}
    for c in old_clips["clips"]:
        for pid, g in GROUPS.items():
            if g["match"](c["id"]):
                plans.setdefault(pid, []).append(c)
                break

    # 旧 manifest（packs/dsh）里的 state-map / license 一并带过去
    state_map = os.path.join(PACKS, "dsh", "state-map.json")
    action = []
    for pid, cs in plans.items():
        for c in cs:
            src = os.path.join(ROOT, "frames_" + c["id"])
            dst = os.path.join(PACKS, pid, "frames", c["id"])
            if os.path.isdir(dst) and not os.path.isdir(src):
                action.append(("skip", pid, c["id"], "已迁移"))
            elif os.path.isdir(src):
                action.append(("move", pid, c["id"], src))
            else:
                action.append(("miss", pid, c["id"], src))
    print("迁移计划（%s）：" % ("干跑" if dry else "执行"))
    for kind, pid, cid, src in action:
        print("  [%s] %-11s %-12s %s" % (kind, pid, cid, src))
    if dry:
        return 0

    # 1) 备份旧的清单
    os.makedirs(LEGACY, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    for name in ("clips.json", "meta.json"):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(LEGACY, "%s.%s" % (name, ts)))
    dm = os.path.join(PACKS, "dsh", "manifest.json")
    if os.path.exists(dm):
        shutil.copy2(dm, os.path.join(LEGACY, "manifest-dsh.%s.json" % ts))

    # 2) 移动帧目录
    for kind, pid, cid, src in action:
        if kind == "move":
            dst = os.path.join(PACKS, pid, "frames", cid)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            print("  移动 %s → %s" % (os.path.relpath(src, REPO), os.path.relpath(dst, REPO)))

    # 3) 写 manifest + clips.json
    for pid, cs in plans.items():
        entries = [clip_entry(c, meta, pid) for c in cs]
        default_idle = old_clips.get("defaultIdle")
        if default_idle not in {e["clipId"] for e in entries}:
            idles = [e["clipId"] for e in entries if e["type"] == "idle"] or [entries[0]["clipId"]]
            default_idle = idles[0]
        data = {"version": 1, "packId": pid, "charId": pid, "role": GROUPS[pid]["role"],
                "canvas": dict(CANVAS), "frames": dict(FRAMES_CONF),
                "defaultIdle": default_idle,
                "license": {"source": "DSMaid / 即梦（绿幕 mp4）", "license": "AI 生成，未授权商用",
                            "author": "DSMaid", "url": ""},
                "clips": entries}
        save_atomic(os.path.join(PACKS, pid, "manifest.json"), data)
        save_atomic(os.path.join(PACKS, pid, "clips.json"),
                    {"version": 1, "defaultIdle": default_idle, "clips": runtime_clips(data)})
        print("  写入 packs/%s：%d 段，defaultIdle=%s" % (pid, len(entries), default_idle))

    # 4) state-map 跟着 DSMaid 走
    dst_sm = os.path.join(PACKS, "maid-dsh", "state-map.json")
    if os.path.exists(state_map) and not os.path.exists(dst_sm):
        shutil.copy2(state_map, dst_sm)
        print("  state-map.json → packs/maid-dsh/")
    # 临时的 packs/dsh 目录不再需要：整体挪进 .legacy（不删，便于回退）
    old_pack = os.path.join(PACKS, "dsh")
    if os.path.isdir(old_pack):
        dst_old = os.path.join(LEGACY, "pack-dsh.%s" % ts)
        shutil.move(old_pack, dst_old)
        print("  旧 packs/dsh → %s" % os.path.relpath(dst_old, REPO))

    # 5) active + 运行时索引
    save_atomic(os.path.join(PACKS, "active.json"), {"pack": "maid-dsh"})
    dsh = load_json(os.path.join(PACKS, "maid-dsh", "clips.json"))
    save_atomic(os.path.join(ROOT, "clips.json"),
                {"version": 1, "defaultIdle": dsh["defaultIdle"], "clips": dsh["clips"]})

    # 6) 自检
    ok = True
    for pid, cs in plans.items():
        data = load_json(os.path.join(PACKS, pid, "manifest.json"))
        ids = {c["clipId"] for c in data["clips"]}
        for e in data["clips"]:
            d = os.path.join(PACKS, pid, e["dir"])
            n = len(glob.glob(os.path.join(d, "*.webp")) + glob.glob(os.path.join(d, "*.png")))
            first = glob.glob(os.path.join(d, "f0001.*"))
            if n != e["count"]:
                print("  ✗ %s/%s 帧数 %d ≠ 声明 %d" % (pid, e["clipId"], n, e["count"]))
                ok = False
            if not first:
                print("  ✗ %s/%s 缺 f0001" % (pid, e["clipId"]))
                ok = False
        if len(ids) != len(data["clips"]):
            print("  ✗ %s 有重复 clipId" % pid)
            ok = False
    # 旧目录只剩已迁移完的残留时清掉
    old_dirs = glob.glob(os.path.join(ROOT, "frames_*"))
    print("自检：%s；旧顶层 frames_* 目录 %d 个" % ("通过" if ok else "有问题", len(old_dirs)))
    print("active pack = maid-dsh（改 packs/active.json 可切换）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
