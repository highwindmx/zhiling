#!/usr/bin/env python3
"""deskpet-tauri 的两件事：编译期暂存前端、运行期准备素材目录（素材不进 exe）。

- 前端暂存（--frontend）：把 frontend/index.html + frontend/src 复制到 frontend-dist/。
  这是 tauri.conf.json 的 frontendDist，会被编译进 exe（只有几十 KB 的 HTML/JS）。
  这一步是编译前必须的，因为它就是 exe 里的页面本体。
- 素材准备（--assets）：把 deskpet-mini 的 clips.json / meta.json / frames_* 复制到
  deskpet-tauri/assets/。这一步**不是**编译前必须的：exe 运行时按
  DESKPET_ASSETS 或「exe 同级的 assets/」读盘，换素材不用重新编译。

clips.json 与 meta.json 原样复制（不重命名、不合并），保证 mini 与 tauri 读到的字段
语义完全一致；打包发行时 assets/ 就是素材目录本身（见 docs/material-packs.md）。

用法：
    python build/prepare_assets.py             # 前端 + 素材都做（素材缺帧只警告）
    python build/prepare_assets.py --frontend  # 只暂存前端（编译前必须）
    python build/prepare_assets.py --assets    # 只准备素材（缺帧直接报错）
"""
import argparse
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ROOT = os.path.dirname(PROJECT)
MINI = os.path.join(ROOT, "deskpet-mini")
SRC_FRONTEND = os.path.join(PROJECT, "frontend")
DIST = os.path.join(PROJECT, "frontend-dist")        # tauri.conf.json 的 frontendDist
ASSETS = os.path.join(PROJECT, "assets")             # exe 同级的运行期素材目录
PACKS = os.path.join(ROOT, "packs")


def active_pack():
    """packs/active.json → 第一个带 clips.json 的 pack（与 pet.py / 工坊同一套规则）。"""
    p = os.path.join(PACKS, "active.json")
    try:
        with open(p, encoding="utf-8") as f:
            pid = (json.load(f) or {}).get("pack")
        if pid and os.path.isdir(os.path.join(PACKS, pid)):
            return pid
    except Exception:
        pass
    if os.path.isdir(PACKS):
        for d in sorted(os.listdir(PACKS)):
            if os.path.exists(os.path.join(PACKS, d, "clips.json")):
                return d
    return None


PACK_ID = active_pack()
STATE_MAP_SRC = os.path.join(PACKS, PACK_ID, "state-map.json") if PACK_ID else ""


def stage_frontend():
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST, exist_ok=True)
    shutil.copy2(os.path.join(SRC_FRONTEND, "index.html"), DIST)
    shutil.copytree(os.path.join(SRC_FRONTEND, "src"), os.path.join(DIST, "src"))
    size = sum(os.path.getsize(os.path.join(b, f))
               for b, _d, fs in os.walk(DIST) for f in fs)
    print(f"frontend-dist: {size/1e3:.1f} KB（会打进 exe）")
    return True


def prepare_assets(strict):
    meta_path = os.path.join(MINI, "meta.json")
    clips_path = os.path.join(MINI, "clips.json")
    for p in (meta_path, clips_path):
        if not os.path.exists(p):
            msg = f"缺少 {p}"
            if strict:
                raise SystemExit(msg)
            print(f"[警告] {msg}，跳过素材准备（exe 运行时会要求 assets/ 存在）")
            return False

    meta = json.load(open(meta_path, encoding="utf-8"))
    meta_clips = meta.get("clips", {})
    clips = json.load(open(clips_path, encoding="utf-8"))
    clips_by_id = {c["id"]: c for c in clips.get("clips", []) if c.get("id")}
    ids = list(clips_by_id)
    print(f"active pack = {PACK_ID}（packs/active.json）")

    if os.path.isdir(ASSETS):
        shutil.rmtree(ASSETS)
    os.makedirs(ASSETS, exist_ok=True)
    shutil.copy2(clips_path, os.path.join(ASSETS, "clips.json"))
    shutil.copy2(meta_path, os.path.join(ASSETS, "meta.json"))

    total = 0
    missing = []
    for cid in ids:
        # dir 以 clips.json 为准（由 manifest 派生，工坊新加的 clip 也在里面）；meta.json 只作兜底
        d = clips_by_id[cid].get("dir") or meta_clips.get(cid, {}).get("dir", "frames_" + cid)
        # 帧目录两种位置都认：packs/<pack>/frames/<clipId>（新格式）/ deskpet-mini/frames_*（旧格式）
        src = os.path.join(PACKS, PACK_ID or "", d) if PACK_ID else ""
        if not src or not os.path.isdir(src):
            src = os.path.join(MINI, d)
        if not os.path.isdir(src):
            missing.append(cid)
            continue
        dst = os.path.join(ASSETS, d)
        shutil.copytree(src, dst)
        size = sum(os.path.getsize(os.path.join(b, f))
                   for b, _dd, fs in os.walk(dst) for f in fs)
        total += size
        print(f"{cid:14s} -> {d:22s} {size/1e6:6.2f} MB")

    if os.path.exists(STATE_MAP_SRC):
        shutil.copy2(STATE_MAP_SRC, os.path.join(ASSETS, "state-map.json"))
        print(f"state-map.json <- {os.path.relpath(STATE_MAP_SRC, ROOT)}")
    else:
        print(f"[警告] 没有 {os.path.relpath(STATE_MAP_SRC, ROOT)}，运行时将用内置默认待机")

    print(f"assets: {len(ids) - len(missing)}/{len(ids)} 段，合计 {total/1e6:.1f} MB -> {ASSETS}")
    if missing:
        msg = "以下 clip 没有帧目录（先跑 deskpet-mini/build/build_*.py）：" + ", ".join(missing)
        if strict:
            raise SystemExit(msg)
        print("[警告] " + msg)
    if meta.get("canvas"):
        print(f"canvas={meta['canvas']['w']}x{meta['canvas']['h']}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frontend", action="store_true", help="只暂存前端")
    ap.add_argument("--assets", action="store_true", help="只准备素材")
    args = ap.parse_args()
    only_frontend = args.frontend and not args.assets
    only_assets = args.assets and not args.frontend
    if not only_assets:
        if not stage_frontend():
            raise SystemExit(1)
    if not only_frontend:
        prepare_assets(strict=only_assets)


if __name__ == "__main__":
    main()
