"""把 v1 packs/<packId>/ 迁成 v2「主题索引 + 动画自描述」目录树（一次性，可重复运行）。

用法：
    python build/tools/migrate_packs_v2.py              # 只报告，不写盘
    python build/tools/migrate_packs_v2.py --apply     # 实际生成
    python build/tools/migrate_packs_v2.py --apply --force   # 覆盖已存在的 meta.json

安全约定：
- 旧 packs/<packId>/ 目录**一个字节都不动**（不删、不改名、不移动），供对照与回滚。
- 帧优先用 NTFS 硬链接（同盘、省 95MB），建不了再 copy2；旧文件始终完整可读。
- 默认 dry-run；只有显式 --apply 才落盘。
- 本脚本只放 build/tools/，不进项目根目录。
"""
import argparse
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.dirname(BASE)
ROOT = os.path.dirname(BUILD)
sys.path.insert(0, BASE)
import pack  # noqa: E402 - 复用 v2 的空主题模板与状态表

PACKS = pack.packs_root()
THEMES_DIR = pack.themes_dir()
ANIM_DIR = pack.anim_dir()
ACTIVE_JSON = pack.active_json_path()

# 旧 state-map 的 long_idle 两档 → v2 两个独立状态
LONG_IDLE_KEYS = ("long_idle_1", "long_idle_2")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                       # noqa: BLE001
        return {} if default is None else default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def old_packs():
    out = []
    for d in sorted(os.listdir(PACKS)):
        p = os.path.join(PACKS, d)
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "manifest.json")):
            out.append(d)
    return out


def link_or_copy(src, dst, report):
    if os.path.exists(dst):
        return "skip"
    try:
        os.link(src, dst)
        report["hardlink"] += 1
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        report["copy"] += 1
        return "copy"


def build_assets(pack_id, manifest, apply, force, report):
    """每段动画 → animations/<assetId>/meta.json + 帧。"""
    made = []
    canvas = manifest.get("canvas") or {"w": 576, "h": 530}
    license_obj = manifest.get("license") or {}
    for c in manifest.get("clips", []):
        cid = c.get("clipId")
        if not cid:
            continue
        dst_dir = os.path.join(ANIM_DIR, cid)
        meta_path = os.path.join(dst_dir, "meta.json")
        src_dir = os.path.join(PACKS, pack_id, c.get("dir") or ("frames/" + cid))
        meta = dict(c)                       # 保留 keyer/scale/offset/anchor/crop/sourcePath，工坊重抠要用
        meta.pop("clipId", None)
        meta["version"] = 2
        meta["assetId"] = cid
        meta["name"] = c.get("displayName") or cid
        meta["canvas"] = {"w": int(canvas.get("w") or 576), "h": int(canvas.get("h") or 530)}
        meta.setdefault("fps", (manifest.get("frames") or {}).get("fps", 24))
        meta.setdefault("format", (manifest.get("frames") or {}).get("format", "webp"))
        meta.setdefault("count", 0)
        meta.setdefault("loop", False)
        meta["origin"] = {"pack": pack_id, "clipId": cid, "manifest": "packs/%s/manifest.json" % pack_id}
        meta["license"] = license_obj
        if "dir" in meta:
            meta["legacyDir"] = meta.pop("dir")

        exists = os.path.exists(meta_path)
        if apply:
            os.makedirs(dst_dir, exist_ok=True)
            if force or not exists:
                write_json(meta_path, meta)
            if os.path.isdir(src_dir):
                for fn in sorted(os.listdir(src_dir)):
                    s = os.path.join(src_dir, fn)
                    if os.path.isfile(s) and fn != "meta.json":
                        link_or_copy(s, os.path.join(dst_dir, fn), report)
        made.append((cid, meta["name"], src_dir, exists))
    return made


def build_theme(pack_id, manifest, apply, force):
    """旧 state-map.json 的 rules → v2 states（clipIds[]）；无 state-map → 空映射。"""
    tid = pack_id
    default_idle = manifest.get("defaultIdle") or "idle"
    theme = pack.empty_theme(tid, name=manifest.get("role") or tid, idle_asset=default_idle)
    sm = read_json(os.path.join(PACKS, pack_id, "state-map.json"))
    rules = sm.get("rules") or []
    if "longIdleSec" in sm:
        theme["longIdleSec"] = sm["longIdleSec"]
    if "awaitingUserSec" in sm:
        theme["awaitingUserSec"] = sm["awaitingUserSec"]

    # 空映射：只保留 idle，其余 10 行 clipIds=[]（用户定稿）
    if not rules:
        theme["_migratedFrom"] = "empty-map（无 state-map.json）"
        if apply and (force or not os.path.exists(pack.theme_path(tid))):
            write_json(pack.theme_path(tid), theme)
        return theme, 0

    long_rules = sorted([r for r in rules if r.get("state") == "long_idle"],
                        key=lambda r: float(r.get("afterSec") or 0))
    used = 0
    for r in rules:
        st = r.get("state")
        if st not in pack.STATE_KEYS and st != "long_idle":
            continue
        if st == "long_idle":
            idx = long_rules.index(r) if r in long_rules else 0
            if idx >= len(LONG_IDLE_KEYS):
                continue
            st = LONG_IDLE_KEYS[idx]
        if st not in theme["states"]:
            continue
        cid = r.get("clipId") or ""
        dst = theme["states"][st]
        dst["clipIds"] = [cid] if cid else []
        dst["fallbackClipIds"] = list(r.get("fallbackClipIds") or [])
        for k in ("priority", "minPlayMs", "cooldownSec", "loop",
                  "interruptible", "enabled", "repeat", "afterSec"):
            if k in r:
                dst[k] = r[k]
        used += 1
    theme["_migratedFrom"] = "packs/%s/state-map.json（%d 条规则）" % (pack_id, used)
    if apply and (force or not os.path.exists(pack.theme_path(tid))):
        write_json(pack.theme_path(tid), theme)
    return theme, used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写盘（默认只报告）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 meta.json / theme.json")
    args = ap.parse_args()

    report = {"hardlink": 0, "copy": 0}
    packs = old_packs()
    if not packs:
        # 一次性迁移脚本：v1 树已被清理/归档后不能再跑 --apply，否则会用
        # active.json 的 pack 键重写文件、丢掉其它字段，且没有任何资产可迁。
        print("未发现 v1 素材包（packs/<id>/manifest.json 均不存在）。")
        print("v2 目录树已就位，本脚本是一次性迁移工具，已跳过，不会写 active.json。")
        print("如需重建 v1 源清单，请从 git 历史恢复 packs/<id>/manifest.json。")
        return
    print("v1 素材包: %s" % (", ".join(packs) or "（无）"))
    print("模式: %s%s" % ("APPLY" if args.apply else "DRY-RUN", " + force" if args.force else ""))
    for pid in packs:
        m = read_json(os.path.join(PACKS, pid, "manifest.json"))
        assets = build_assets(pid, m, args.apply, args.force, report)
        theme, rules = build_theme(pid, m, args.apply, args.force)
        bound = sum(1 for v in theme["states"].values() if v.get("clipIds"))
        print("  %-12s %2d 段动画 → animations/   |   主题 %s：%d 条规则 → %d/11 行有绑定"
              % (pid, len(assets), theme.get("name") or pid, rules, bound))
        for cid, name, src, exists in assets[:3]:
            print("      %-16s %-8s  src=%s%s" % (cid, name, os.path.relpath(src, ROOT),
                                                  "  (meta 已存在)" if exists else ""))
        if len(assets) > 3:
            print("      … 另 %d 段" % (len(assets) - 3))

    active = (read_json(ACTIVE_JSON).get("pack") or "maid-dsh")
    new_active = {"theme": active, "pack": active}
    if args.apply:
        write_json(ACTIVE_JSON, new_active)
        pack.invalidate_cache()
    print("active.json → %s" % json.dumps(new_active, ensure_ascii=False))
    print("帧: 硬链接 %d 个 / 复制 %d 个（旧 packs/<id>/ 原样保留）"
          % (report["hardlink"], report["copy"]))
    if not args.apply:
        print("\n这是 dry-run。确认无误后加 --apply 实际生成。")
    else:
        print("\n迁移完成。v1 目录未做任何改动，确认新树可用后再自行处置。")


if __name__ == "__main__":
    main()
