#!/usr/bin/env python3
"""deskpet-tauri 的运行期素材准备脚本（纯标准库，不碰前端）。

- 前端：frontend/ 就是 tauri.conf.json 的 frontendDist，直接被编译进 exe
  （只有几十 KB 的 HTML/JS）。改完前端直接重编即可，没有中间的暂存副本，
  也不会出现「改了前端却像没生效」——见 README 的「改了前端如何生效」。
- 素材准备（--assets）：把 v2 素材树整体复制到 deskpet-tauri/assets/：
      themes/*.json                 主题（11 状态 × clipIds 动作池）
      animations/<assetId>/meta.json + 帧（webp/png）
      active.json                   当前主题镜像（源里没有就由运行时按 active 标记裁定）
  这一步**不是**编译前必须的：exe 运行时按 DESKPET_ASSETS 或「exe 同级的 assets/」
  读盘，换主题/换素材不用重新编译。

v2 不再有 clips.json / manifest.json / meta.json（旧格式已由 Python 侧删除），
画布、帧数、fps、格式全部来自每段动画自己的 meta.json。

注意：本脚本会整体重建 assets/（包括设置面板写过的主题文件），
重新运行前请先备份；备份也会落在 assets/.backup/ 里（设置面板保存时自动写）。

用法：
    python build/prepare_assets.py             # 只准备素材
    python build/prepare_assets.py --assets    # 只准备素材（缺元数据直接报错）
"""
import argparse
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
ROOT = os.path.dirname(PROJECT)
MINI = os.path.join(ROOT, "deskpet-python")
SRC_FRONTEND = os.path.join(PROJECT, "frontend")    # 即 tauri.conf.json 的 frontendDist
ASSETS = os.path.join(PROJECT, "assets")             # exe 同级的运行期素材目录
# v2 packs 跟 Python 版一起放在 deskpet-python/packs/；老仓库在仓库根，按优先级回退
PACKS = next(
    (p for p in (os.path.join(MINI, "packs"), os.path.join(ROOT, "packs")) if os.path.isdir(p)),
    os.path.join(MINI, "packs"))
THEMES = os.path.join(PACKS, "themes")
ANIM = os.path.join(PACKS, "animations")
DEFAULT_CANVAS = (576, 530)


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


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _aspect(canvas):
    if isinstance(canvas, (list, tuple)) and len(canvas) >= 2:
        return int(canvas[0] or DEFAULT_CANVAS[0]), int(canvas[1] or DEFAULT_CANVAS[1])
    c = canvas or {}
    return int(c.get("w") or DEFAULT_CANVAS[0]), int(c.get("h") or DEFAULT_CANVAS[1])


def scan_assets():
    """animations/*/meta.json → 校验列表（不复制）。"""
    out = []
    if not os.path.isdir(ANIM):
        return out
    for aid in sorted(os.listdir(ANIM)):
        d = os.path.join(ANIM, aid)
        if not os.path.isdir(d) or aid.startswith("."):
            continue
        meta = _read_json(os.path.join(d, "meta.json"))
        if not meta:
            out.append((aid, None, "缺 meta.json"))
            continue
        fmt = (meta.get("format") or "webp").lower()
        count = int(meta.get("count") or 0)
        frames = sorted(f for f in os.listdir(d)
                        if os.path.isfile(os.path.join(d, f)) and not f.startswith("."))
        if not frames:
            out.append((aid, meta, "没有帧文件"))
            continue
        if count <= 0:
            count = len(frames)
        expected = f"f0000.{fmt}"
        first = expected if expected in frames else frames[0]
        try:
            got = image_size(os.path.join(d, first))
        except Exception as e:                       # noqa: BLE001 - 只警告，不中断
            out.append((aid, meta, "首帧读不了：%s" % e))
            continue
        want = _aspect(meta.get("canvas"))
        if got != want:
            out.append((aid, meta, "首帧 %dx%d 与 canvas %dx%d 不一致"
                        % (got[0], got[1], want[0], want[1])))
            continue
        out.append((aid, meta, ""))
    return out


def active_theme_id():
    """与 pack.py 一致：themes/*.json 的 active 标记；多个为真取最近写入。"""
    best, best_m = "", -1
    if not os.path.isdir(THEMES):
        return ""
    for name in sorted(os.listdir(THEMES)):
        if not name.endswith(".json"):
            continue
        p = os.path.join(THEMES, name)
        d = _read_json(p)
        if d.get("active") is True:
            try:
                m = os.stat(p).st_mtime_ns
            except OSError:
                m = 0
            if m >= best_m:
                best, best_m = (d.get("id") or name[:-5]), m
    return best


def prepare_assets(strict):
    if not os.path.isdir(THEMES):
        msg = f"缺少 {THEMES}（v2 主题目录）"
        if strict:
            raise SystemExit(msg)
        print(f"[警告] {msg}，跳过素材准备")
        return False

    scanned = scan_assets()
    if not scanned:
        msg = f"{ANIM} 下没有任何动画资产"
        if strict:
            raise SystemExit(msg)
        print(f"[警告] {msg}，跳过素材准备")
        return False
    bad = [(aid, why) for aid, _m, why in scanned if why]
    if bad:
        msg = "素材校验失败：" + "；".join("%s=%s" % x for x in bad)
        if strict:
            raise SystemExit(msg)
        print("[警告] " + msg)

    if os.path.isdir(ASSETS):
        # 只清空「安全的素材目录」：不能是盘符根，也不能里面有 exe（避免把运行目录删掉）
        root = os.path.abspath(ASSETS)
        if os.path.dirname(root) == root:
            raise SystemExit(f"拒绝清空盘符根目录：{root}")
        if any(n.lower().endswith(".exe") for n in os.listdir(root)):
            raise SystemExit(f"拒绝清空含 exe 的目录：{root}")
        shutil.rmtree(ASSETS)
    os.makedirs(ASSETS, exist_ok=True)

    # 主题：整体复制（保留用户手改与未知字段）
    shutil.copytree(THEMES, os.path.join(ASSETS, "themes"))
    theme_ids = sorted(n[:-5] for n in os.listdir(THEMES) if n.endswith(".json"))
    active = active_theme_id()
    print(f"themes: {len(theme_ids)} 个（{'/'.join(theme_ids)}），active={active or '（未标记，运行时取第一个）'}")

    # 动画：整目录复制，跳过 .backup / __pycache__
    os.makedirs(os.path.join(ASSETS, "animations"), exist_ok=True)
    total = 0
    good = 0
    for aid, _meta, why in scanned:
        if why:
            continue
        src = os.path.join(ANIM, aid)
        dst = os.path.join(ASSETS, "animations", aid)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".backup", "__pycache__"))
        size = sum(os.path.getsize(os.path.join(b, f))
                   for b, _dd, fs in os.walk(dst) for f in fs)
        total += size
        good += 1
        print(f"{aid:16s} -> animations/{aid:16s} {size/1e6:6.2f} MB")
    print(f"assets: {good}/{len(scanned)} 段，合计 {total/1e6:.1f} MB -> {ASSETS}")

    src_active = os.path.join(PACKS, "active.json")
    if os.path.isfile(src_active):
        shutil.copy2(src_active, os.path.join(ASSETS, "active.json"))
        print(f"active.json <- {os.path.relpath(src_active, ROOT)}")
    elif active:
        # 源里没有镜像时补一份，运行时/设置都能看到唯一 active
        with open(os.path.join(ASSETS, "active.json"), "w", encoding="utf-8") as f:
            json.dump({"theme": active, "pack": active}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"active.json 已生成（active={active}）")
    return True


def main():
    global ASSETS
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", action="store_true", help="只准备素材")
    ap.add_argument("--out", help="素材输出父目录：素材写到 <目录>/assets（通常传 exe 所在目录）；默认 <项目>/assets")
    ap.add_argument("--packs", nargs="?", const=os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DeskPet", "packs"),
                    help="素材输出目录（packs 根，直接包含 themes/animations/active.json）；不带值时写 %%LOCALAPPDATA%%\\DeskPet\\packs")
    args = ap.parse_args()
    if args.packs:
        ASSETS = os.path.abspath(args.packs)
        print(f"packs 输出目录 -> {ASSETS}")
    elif args.out:
        ASSETS = os.path.join(os.path.abspath(args.out), "assets")
        print(f"素材输出目录 -> {ASSETS}")
    only_assets = bool(args.assets)
    prepare_assets(strict=only_assets)


if __name__ == "__main__":
    main()
