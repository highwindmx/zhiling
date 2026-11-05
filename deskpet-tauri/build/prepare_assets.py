#!/usr/bin/env python3
"""deskpet-tauri 的两件事：编译期暂存前端、运行期准备素材目录（素材不进 exe）。

- 前端暂存（--frontend）：把 frontend/index.html + frontend/src 复制到 frontend-dist/。
  这是 tauri.conf.json 的 frontendDist，会被编译进 exe（只有几十 KB 的 HTML/JS）。
  这一步是编译前必须的，因为它就是 exe 里的页面本体。
- 素材准备（--assets）：把当前 pack 的 clips.json / manifest.json / frames/<clipId> 复制到
  deskpet-tauri/assets/。这一步**不是**编译前必须的：exe 运行时按
  DESKPET_ASSETS 或「exe 同级的 assets/」读盘，换素材不用重新编译。

clips.json 与 manifest.json 原样复制（不重命名、不合并），保证 mini 与 tauri 读到的
画布/字段语义完全一致；打包发行时 assets/ 就是素材目录本身（见 docs/material-packs.md）。

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
MINI = os.path.join(ROOT, "deskpet-python")
SRC_FRONTEND = os.path.join(PROJECT, "frontend")
DIST = os.path.join(PROJECT, "frontend-dist")        # tauri.conf.json 的 frontendDist
ASSETS = os.path.join(PROJECT, "assets")             # exe 同级的运行期素材目录
# packs 跟 Python 版一起放在 deskpet-python/packs/；老仓库在仓库根，按优先级回退
PACKS = next(
    (p for p in (os.path.join(MINI, "packs"), os.path.join(ROOT, "packs")) if os.path.isdir(p)),
    os.path.join(MINI, "packs"))


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


def image_size(path):
    """只读文件头求宽高（不依赖 Pillow：prepare_assets 只用标准库）。

    支持 webp（VP8X / VP8 / VP8L）与 png（IHDR）。
    """
    with open(path, "rb") as f:
        data = f.read(64)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1)
        if chunk == b"VP8 ":
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if chunk == b"VP8L":
            b = data[21:25]
            w = ((b[1] & 0x3F) << 8 | b[0]) + 1
            h = ((b[3] & 0x0F) << 10 | b[2] << 2 | (b[1] & 0xC0) >> 6) + 1
            return w, h
        raise ValueError("未知 WebP 块 %r" % chunk)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    raise ValueError("不认识的图片格式")


def prepare_assets(strict):
    meta_path = os.path.join(MINI, "meta.json")
    clips_path = os.path.join(MINI, "clips.json")
    manifest_src = os.path.join(PACKS, PACK_ID, "manifest.json") if PACK_ID else ""
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
    manifest = None
    if manifest_src and os.path.exists(manifest_src):
        manifest = json.load(open(manifest_src, encoding="utf-8"))
        shutil.copy2(manifest_src, os.path.join(ASSETS, "manifest.json"))
        print(f"manifest.json <- {os.path.relpath(manifest_src, ROOT)}"
              f"（canvas={manifest.get('canvas', {}).get('w')}x{manifest.get('canvas', {}).get('h')}）")
        lic = manifest.get("license") or {}
        print(f"license: {lic.get('kind', 'unknown')} / {lic.get('source') or '（未填写来源）'}")
        if lic.get("kind", "unknown") == "unknown":
            print("[提示] 该 pack 的 license.kind=unknown：本地运行不受影响，发布前请补全")
    else:
        print("[警告] 没有 manifest.json，tauri 侧画布回退 meta.json（旧 576×530）")

    total = 0
    missing = []
    canvas = ((manifest or {}).get("canvas") or meta.get("canvas") or {})
    cw, chh = int(canvas.get("w", 0)), int(canvas.get("h", 0))
    bad_size = []
    for cid in ids:
        # dir 以 clips.json 为准（由 manifest 派生，工坊新加的 clip 也在里面）；meta.json 只作兜底
        d = clips_by_id[cid].get("dir") or meta_clips.get(cid, {}).get("dir", "frames_" + cid)
        # 帧目录两种位置都认：packs/<pack>/frames/<clipId>（新格式）/ deskpet-python/frames_*（旧格式）
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
        # 画布校验（只读首帧文件头）：不一致的段全部列出来，不静默缩放
        if cw and chh:
            try:
                first = sorted(os.listdir(dst))[0]
                try:                               # 有 Pillow 就用（能识别更多格式）
                    from PIL import Image
                    with Image.open(os.path.join(dst, first)) as im:
                        got = im.size
                except ImportError:                # 没有 Pillow 时退回标准库文件头解析
                    got = image_size(os.path.join(dst, first))
                if got != (cw, chh):
                    bad_size.append((cid, "%dx%d" % got))
            except Exception as e:                  # noqa: BLE001
                bad_size.append((cid, "读不了首帧：%s" % e))
        print(f"{cid:14s} -> {d:22s} {size/1e6:6.2f} MB")

    if bad_size:
        msg = ("帧尺寸与 canvas=%dx%d 不一致：" % (cw, chh)
               + "，".join("%s=%s" % x for x in bad_size))
        if strict:
            raise SystemExit(msg)
        print("[警告] " + msg)

    if os.path.exists(STATE_MAP_SRC):
        shutil.copy2(STATE_MAP_SRC, os.path.join(ASSETS, "state-map.json"))
        print(f"state-map.json <- {os.path.relpath(STATE_MAP_SRC, ROOT)}")
    else:
        print(f"[警告] 没有 {os.path.relpath(STATE_MAP_SRC, ROOT)}，运行时将用内置默认待机")

    print(f"assets: {len(ids) - len(missing)}/{len(ids)} 段，合计 {total/1e6:.1f} MB -> {ASSETS}")
    if missing:
        msg = "以下 clip 没有帧目录（先跑 deskpet-python/build/build_*.py）：" + ", ".join(missing)
        if strict:
            raise SystemExit(msg)
        print("[警告] " + msg)
    if meta.get("canvas"):
        print(f"canvas={meta['canvas']['w']}x{meta['canvas']['h']}（旧 meta.json）")
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
