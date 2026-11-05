#!/usr/bin/env python3
"""把 deskpet-mini 的 frames_* 帧序列同步到 frontend/assets/，并生成 clips.json。

Tauri 的前端资源会被整个内嵌进 exe，运行时读不到 ../deskpet-mini/，所以这里在
构建前做一次同步。这样两版用的是同一批 WebP q80 帧（576x530），体积/内存/画质
对比才是同口径的。

为什么不用 deskpet-mini/clips.json 的名字：那份配置里的显示名是旧的
（action=挥手、action2=打字、action3=抱臂），但底层素材实际是
真开心 / 工作中 / 不高兴。本脚本沿用 mini 的 meta.json（目录、帧数、fps、画布）
和 priority 顺序，只把显示名改成与实际动作一致的叫法。

用法：
    python build/prepare_assets.py
需要 deskpet-mini/frames_idle|action|action2|action3 已生成。
"""
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ROOT = os.path.dirname(PROJECT)
MINI = os.path.join(ROOT, "deskpet-mini")
SRC_FRONTEND = os.path.join(PROJECT, "frontend")
DIST = os.path.join(PROJECT, "frontend-dist")      # tauri.conf.json 的 frontendDist
DST = os.path.join(DIST, "assets")

# id 与 mini 的 frames 目录一一对应；name/priority 以实际动作为准。
CLIPS = [
    {"id": "idle", "dir": "frames_idle", "name": "待机", "type": "idle", "loop": True, "priority": 0},
    {"id": "action", "dir": "frames_action", "name": "真开心", "type": "action", "loop": False, "priority": 1},
    {"id": "action2", "dir": "frames_action2", "name": "工作中", "type": "action", "loop": False, "priority": 2},
    {"id": "action3", "dir": "frames_action3", "name": "不高兴", "type": "action", "loop": False, "priority": 3},
]


def main():
    with open(os.path.join(MINI, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    canvas = [meta["canvas"]["w"], meta["canvas"]["h"]]

    # 手写前端（index.html + src/）原样复制到暂存目录，避免把 frontend/assets
    # 里遗留的旧素材一起内嵌进 exe。
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST, exist_ok=True)
    shutil.copy2(os.path.join(SRC_FRONTEND, "index.html"), DIST)
    shutil.copytree(os.path.join(SRC_FRONTEND, "src"), os.path.join(DIST, "src"))

    os.makedirs(DST, exist_ok=True)
    out = {"canvas": canvas, "clips": {}}
    total = 0
    for spec in CLIPS:
        src = os.path.join(MINI, spec["dir"])
        if not os.path.isdir(src):
            raise SystemExit(f"找不到 mini 帧目录：{src}\n请先在 deskpet-mini 里生成 frames_*")
        dst = os.path.join(DST, spec["dir"])
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        files = [n for n in os.listdir(dst) if n.startswith("f") and n.endswith(".webp")]
        count = len(files)
        size = sum(os.path.getsize(os.path.join(dst, n)) for n in files)
        total += size
        m = meta["clips"][spec["id"]]
        out["clips"][spec["id"]] = {
            "dir": spec["dir"],
            "fps": m.get("fps", 24),
            "count": count,
            "format": "webp",
            "name": spec["name"],
            "type": spec["type"],
            "loop": spec["loop"],
            "priority": spec["priority"],
        }
        print(f"{spec['id']:8s} {spec['name']:4s} {count:4d} frames {size/1e6:6.2f} MB -> {dst}")

    with open(os.path.join(DST, "clips.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"canvas={canvas[0]}x{canvas[1]}  total={total/1e6:.2f} MB")
    print(f"wrote {os.path.join(DST, 'clips.json')}")


if __name__ == "__main__":
    main()
