"""生成两段素材的透明帧序列：待机(idle) + 动作(action)，构建期就缩到最终显示尺寸上限。"""
import glob, json, os, re, subprocess, sys
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from keyer import key_frame

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)                  # deskpet-python/
sys.path.insert(0, os.path.join(ROOT, "build", "tools"))
import pack                                  # ffmpeg 统一走 pack.ffmpeg_exe()，工坊/桌宠同一套解析
OUT_W = 576                      # 磁盘上一档：大档宽度；运行时再按所选档位缩小
OUT_FMT = "webp"                 # 输出格式：webp(有损+alpha，约为 png 的 1/7) / png
WEBP_Q = 80                      # 有损质量；q80 在 576px 下与 png 肉眼无差
ALPHA_Q = 80                     # alpha 平面质量，100 会让 method=6 慢 20 倍
IDLE_SRC = os.path.join(ROOT, "assets",
                        "jimeng-2026-11-04-5883-帮我生成桌面桌宠，绿幕背景，方便进行透明视频转码.mp4")
ACT_SRC = os.path.join(ROOT, "assets", "HappyHorse 1.1_1793774480874.mp4")

IDLE_P = dict(hue_in=18, hue_out=36, sat_lo=0.10, sat_hi=0.16, erode=1, blur=1.0,
              keep_frac=1.0, floor=0.16, despill=0.6)
ACT_P = dict(hue_ref=148.0, hue_in=20.0, hue_out=34.0, sat_lo=0.30, sat_hi=0.55,
             val_lo=0.30, val_hi=0.45, erode=1, blur=1.0, floor=0.16, despill=0.75,
             keep_frac=1.0, watermarks=[(0, 0, 230, 90), (1080, 630, 1280, 720)])
# 动作段缩放：头部白区宽比 1.357、可见高度比 1.228。取 1.30，并把画布顶部加高 30px，
# 否则放大后的呆毛会顶到画布上沿被切掉
ACT_SCALE = 1.26
CANVAS_H = 760                   # 画布高度：原 720 + 顶部 30px 透明余量
FOOT = (414, 759)                # 画布内脚底锚点
CROP = (284, 0, 1110, 720)       # 原图裁切范围，宽 827

ACTION_RAW = os.path.join(BASE, "..", "cache", "raw", "action")
IDLE_RAW = os.path.join(BASE, "..", "cache", "raw", "idle24")
LAST_CMD = []                   # 最近一次 ffmpeg 命令，排错/回报用


def ffmpeg_path():
    """imageio-ffmpeg 自带二进制 → PATH；找不到时抛带 uv sync / PATH 指引的 RuntimeError，
    不静默失败。配置里的 ffmpegPath / DESKPET_FFMPEG 不再参与。"""
    return pack.ffmpeg_exe()

VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")


def validate_source(src):
    """ffmpeg 之前先校验输入，别把目录/缺文件直接变成 CalledProcessError 堆栈。"""
    if not src:
        raise RuntimeError("没有源视频：请在 manifest 里补 source/sourcePath，或指定一个 mp4")
    if os.path.isdir(src):
        raise RuntimeError("输入是目录，需要 mp4 等视频文件：%s\n"
                           "（frames 目录是生成结果，不能当源视频）" % src)
    if not os.path.exists(src):
        raise RuntimeError("源视频不存在：%s\n请重新指定素材文件" % src)
    if not os.path.isfile(src):
        raise RuntimeError("源路径不是普通文件：%s" % src)
    ext = os.path.splitext(src)[1].lower()
    if ext not in VIDEO_EXTS:
        raise RuntimeError("不是视频文件（%s）：%s" % (ext or "无扩展名", src))


def extract(src, dst, fps=24, prefix="r%04d"):
    """用 ffmpeg 把视频抽成等比透明帧序列（PNG）。

    prefix 控制输出帧命名：bootstrap（main）用 "r"/"a" 这类短前缀；
    生产路径（build_clip）用默认 "r%04d"。两处共用同一抽帧逻辑，避免重复。
    已存在 *.png 时直接复用，不重抽（排错/二次预览省时间）。
    """
    validate_source(src)
    ff = ffmpeg_path()
    os.makedirs(dst, exist_ok=True)
    existing = sorted(glob.glob(os.path.join(dst, "*.png")))
    if existing:
        return existing
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-vf", "fps=%d" % fps, "-pix_fmt", "rgb24",
           os.path.join(dst, prefix + "%04d.png")]
    LAST_CMD[:] = cmd
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, errors="ignore")
    except subprocess.CalledProcessError as e:
        tail = " | ".join((e.stderr or "").strip().splitlines()[-3:]) or "无 stderr"
        raise RuntimeError("ffmpeg 抽帧失败（退出码 %s）：%s\n命令：%s"
                           % (e.returncode, tail, " ".join(cmd)))
    return sorted(glob.glob(os.path.join(dst, "*.png")))


def resize_rgba(im, size):
    """带 alpha 的高质量缩放：先把颜色乘上 alpha，避免边缘出现暗边。"""
    a = np.asarray(im).astype(np.float32)
    al = a[..., 3:4] / 255.0
    pm = np.concatenate([a[..., :3] * al, a[..., 3:4]], axis=2)
    s = np.asarray(Image.fromarray(pm.astype(np.uint8), "RGBA").resize(size, Image.LANCZOS)).astype(np.float32)
    a2 = np.maximum(s[..., 3:4] / 255.0, 1e-3)
    rgb = np.clip(s[..., :3] / a2, 0, 255)
    return Image.fromarray(np.concatenate([rgb, s[..., 3:4]], 2).astype(np.uint8), "RGBA")


def key_clip(files, params):
    # key_frame 内部已做连通块清理（keep_frac 控制保留比例），这里不再二次 clean
    return [key_frame(np.asarray(Image.open(p).convert("RGB")), **params)
            for p in files]


def save_clip(frames, out_dir, canvas_size):
    if os.path.isdir(out_dir):
        for f in glob.glob(os.path.join(out_dir, "*.png")) + glob.glob(os.path.join(out_dir, "*.webp")):
            os.remove(f)
    os.makedirs(out_dir, exist_ok=True)
    w = OUT_W
    h = round(canvas_size[1] * w / canvas_size[0])
    for i, im in enumerate(frames):
        p = os.path.join(out_dir, f"f{i:04d}.{OUT_FMT}")
        if OUT_FMT == "webp":
            resize_rgba(im, (w, h)).save(p, "WEBP", quality=WEBP_Q, method=6,
                                         alpha_quality=ALPHA_Q, exact=False)
        else:
            resize_rgba(im, (w, h)).save(p, optimize=True)
    return w, h


MARGIN = 0.985                   # 自动贴合画布时留的边距


def alpha_bbox(rgba, thresh=8):
    """前景外接框 [x0,y0,x1,y1]（右/下为闭区间，和旧代码一致）；全透明返回 None。"""
    a = np.asarray(rgba)[:, :, 3]
    ys, xs = np.where(a > thresh)
    if not len(ys):
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]


def fit_bbox(bbox, out_w, oh, margin=MARGIN):
    """主体框 → 画布缩放/偏移。生成与工坊预览共用这一份，保证预览和成品同一位置。"""
    l, t, r, b = bbox
    scale = min(out_w * margin / (r - l + 1), oh * margin / (b - t + 1))
    ox = round((out_w - (r - l + 1) * scale) / 2 - l * scale)
    oy = round(oh - 2 - b * scale)
    return scale, ox, oy


def compose_canvas(rgba, scale, ox, oy, out_w, oh):
    """抠像结果 → 画布（缩放 + 脚底居中 + 留白）。工坊预览也调这个函数，不再各写一套 resize。"""
    small = rgba.resize((max(1, round(rgba.width * scale)),
                         max(1, round(rgba.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (out_w, oh), (0, 0, 0, 0))
    canvas.alpha_composite(small, (ox, oy))
    return resize_rgba(canvas, (out_w, oh))


def extract_frames(src, dst, fps):
    validate_source(src)
    ff = ffmpeg_path()
    os.makedirs(dst, exist_ok=True)
    if glob.glob(os.path.join(dst, "*.png")):
        return sorted(glob.glob(os.path.join(dst, "*.png")))
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-vf", "fps=%d" % fps, "-pix_fmt", "rgb24",
           os.path.join(dst, "r%04d.png")]
    LAST_CMD[:] = cmd
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, errors="ignore")
    except subprocess.CalledProcessError as e:
        tail = " | ".join((e.stderr or "").strip().splitlines()[-3:]) or "无 stderr"
        raise RuntimeError("ffmpeg 抽帧失败（退出码 %s）：%s\n命令：%s"
                           % (e.returncode, tail, " ".join(cmd)))
    return sorted(glob.glob(os.path.join(dst, "*.png")))


def probe_src_fps(src):
    """用 ffmpeg -i 的 stderr 读源帧率（只为日志里如实写「30fps→24fps」）。"""
    try:
        ff = ffmpeg_path()
        r = subprocess.run([ff, "-hide_banner", "-i", src], capture_output=True, text=True,
                           errors="ignore")
        m = re.search(r"(\d+(?:\.\d+)?)\s+fps", r.stderr)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def build_clip(spec, log=print):
    """按 spec 生成一个新 clip：抽帧 → 抠像 → 自动缩放贴合画布 → 存盘 → 写 meta。

    spec 字段：id, video, params(dict), fps?, width?, fmt?, quality?, crop?,
              out_dir?（给了就写到这里，不写 deskpet-python/meta.json，供 pack 用）
    画布比例固定 826:760（与 frames_idle/frames_action 一致）。
    """
    cid = spec["id"]
    src = spec["video"]
    validate_source(src)
    params = dict(spec["params"])
    raw_params = dict(params)       # 源帧坐标的原样参数，回传 meta.keyer 用（便于复抠）
    fps = int(spec.get("fps", 24))
    out_w = int(spec.get("width", OUT_W))
    fmt = spec.get("fmt", OUT_FMT)
    qual = int(spec.get("quality", WEBP_Q))
    # 画布尺寸：pack 模式用 spec 里声明的 width/height（画布是 pack 级属性）；
    # 缺省才回退旧素材的固定比例，避免画布在不同 clip 之间漂移
    if spec.get("height"):
        oh = int(spec["height"])
    else:
        oh = round(out_w * CANVAS_H / (CROP[2] - CROP[0]))
    # crop：整片统一裁一次（黑边这类逐帧会变化的边，绝不能逐帧各裁，否则脚底会抖）
    crop = tuple(spec["crop"]) if spec.get("crop") else None
    # 工坊的「矩形擦除」以源帧坐标传给 params.watermarks，而 key_frame 拿到的是
    # 已裁切的帧。这里减去左上偏移，与 workshop.render / _fit_bbox_cached 完全同口径，
    # 否则会出现「预览里切掉了、生成出来还在」。
    if crop and params.get("watermarks"):
        cl, ct = int(crop[0]), int(crop[1])
        wm = []
        for (x0, y0, x1, y1) in params["watermarks"]:
            a, b_, c_, d = max(0, x0 - cl), max(0, y0 - ct), max(0, x1 - cl), max(0, y1 - ct)
            if c_ > a and d > b_:
                wm.append((a, b_, c_, d))
        params["watermarks"] = wm

    src_fps = probe_src_fps(src)
    if src_fps and abs(src_fps - fps) > 0.5:
        log("源片 %.2ffps → 目标 %dfps：抽帧时统一重采样（锚点按目标帧率算，不会抖）"
            % (src_fps, fps))

    raw_dir = spec.get("raw_dir") or os.path.join(BASE, "..", "cache", "raw", cid)
    files = extract(src, raw_dir, fps=fps)
    if not files:
        raise RuntimeError("抽帧为空：%s" % src)
    log("抽到 %d 帧（%d fps）" % (len(files), fps))

    boxes = []
    for p in (files[0], files[len(files) // 2], files[-1]):
        im = Image.open(p).convert("RGB")
        if crop:
            im = im.crop(crop)
        rgba = key_frame(np.asarray(im), **params)
        bb = alpha_bbox(rgba)
        if bb:
            boxes.append(bb)
    if not boxes:
        raise RuntimeError("抠像后没有前景，检查色相参考（日志里的背景色相）")
    l = min(b[0] for b in boxes)
    t = min(b[1] for b in boxes)
    r = max(b[2] for b in boxes)
    b = max(b[3] for b in boxes)
    scale, ox, oy = fit_bbox((l, t, r, b), out_w, oh)
    log("主体框 %s，缩放 %.3f，偏移 %s" % ([l, t, r, b], scale, [ox, oy]))

    out_dir = spec.get("out_dir") or os.path.join(ROOT, "frames_" + cid)
    os.makedirs(out_dir, exist_ok=True)
    for f in glob.glob(os.path.join(out_dir, "*.png")) + glob.glob(os.path.join(out_dir, "*.webp")):
        os.remove(f)
    n = 0
    for i, p in enumerate(files):
        rgb = Image.open(p).convert("RGB")
        if crop:
            rgb = rgb.crop(crop)
        rgba = key_frame(np.asarray(rgb), **params)
        canvas = compose_canvas(rgba, scale, ox, oy, out_w, oh)
        dst = os.path.join(out_dir, "f%04d.%s" % (n, fmt))
        if fmt == "webp":
            canvas.save(dst, "WEBP", quality=qual, method=6, alpha_quality=ALPHA_Q, exact=False)
        else:
            canvas.save(dst, optimize=True)
        n += 1
        if n % 20 == 0:
            log("  %d/%d" % (n, len(files)))

    foot = [round((l + r) / 2 * scale + ox), oh - 2]
    total = sum(os.path.getsize(os.path.join(out_dir, x)) for x in os.listdir(out_dir))
    info = {"id": cid, "count": n, "bytes": total, "width": out_w, "height": oh,
            "scale": round(scale, 4), "offset": [ox, oy], "foot": foot,
            "fps": fps, "format": fmt, "out_dir": out_dir,
            "crop": list(crop) if crop else None}

    def _jsonable(v):
        if isinstance(v, (list, tuple)):
            return [_jsonable(x) for x in v]
        if isinstance(v, (int, float, str, bool)) or v is None:
            return v
        return str(v)
    # 把本次真正生效的抠像参数带回调用方，写进 meta.json（回溯/复抠用）
    info["params"] = {k: _jsonable(v) for k, v in raw_params.items()}
    if spec.get("out_dir"):
        # pack 模式：清单由调用方写进 manifest，这里不碰 deskpet-python/meta.json
        log("frames_%s：%d 帧，%.2f MB，%dx%d（scale %.3f，offset %s，脚底 y=%d）"
            % (cid, n, total / 1e6, out_w, oh, scale, [ox, oy], foot[1]))
        return info

    meta_path = os.path.join(ROOT, "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}
    meta.setdefault("canvas", {"w": out_w, "h": oh, "src_w": CROP[2] - CROP[0], "src_h": CANVAS_H})
    meta.setdefault("clips", {})[cid] = {
        "dir": "frames_" + cid, "fps": fps, "count": n, "format": fmt,
        "scale": round(scale, 4), "offset": [ox, oy], "foot": foot,
        "source": os.path.basename(src),
        "crop": list(crop) if crop else None,
        "keyer": {k: str(v) for k, v in params.items()},
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    log("frames_%s：%d 帧，%.2f MB，%dx%d" % (cid, n, total / 1e6, out_w, oh))
    return info


def main():
    idle_files = extract(IDLE_SRC, IDLE_RAW, prefix="r")
    act_files = extract(ACT_SRC, ACTION_RAW, prefix="a")
    print("待机帧", len(idle_files), "动作帧", len(act_files))

    idle_keyed = key_clip(idle_files, IDLE_P)
    act_keyed = key_clip(act_files, ACT_P)

    cw, ch = CROP[2] - CROP[0], CANVAS_H
    # 待机段：原裁切图贴到画布底部（顶部让出 30px 透明余量，与动作段共用同一套锚点）
    idle_canvas = []
    for im in idle_keyed:
        c = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        c.alpha_composite(im.crop(CROP), (0, ch - (CROP[3] - CROP[1])))
        idle_canvas.append(c)

    # 动作段：按 1.30 缩放后贴到同一张画布，脚底对齐 FOOT
    canvas = []
    for im in act_keyed:
        c = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        s = im.resize((round(im.width * ACT_SCALE), round(im.height * ACT_SCALE)), Image.LANCZOS)
        # 动作段角色脚底在缩放前 y=642、中心 x=617.5
        ox = round(FOOT[0] - 617.5 * ACT_SCALE)
        oy = round(FOOT[1] - 642 * ACT_SCALE)
        c.alpha_composite(s, (ox, oy))
        canvas.append(c)

    w, h = save_clip(idle_canvas, os.path.join(ROOT, "frames_idle"), (cw, ch))
    save_clip(canvas, os.path.join(ROOT, "frames_action"), (cw, ch))
    print("输出尺寸", w, "x", h)

    # 合并写入：只更新 idle / action 两条，别把工坊或 --spec 生成的其它 clip 抹掉
    meta_path = os.path.join(ROOT, "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}
    meta.setdefault("canvas", {"w": w, "h": h, "src_w": cw, "src_h": ch, "crop": list(CROP)})
    clips = meta.setdefault("clips", {})
    clips["idle"] = {"dir": "frames_idle", "fps": 24, "count": len(idle_canvas), "format": OUT_FMT,
                     "foot": list(FOOT), "source": os.path.basename(IDLE_SRC),
                     "keyer": {k: str(v) for k, v in IDLE_P.items()}}
    clips["action"] = {"dir": "frames_action", "fps": 24, "count": len(canvas), "format": OUT_FMT,
                       "scale": ACT_SCALE,
                       "offset": [round(FOOT[0] - 617.5 * ACT_SCALE),
                                  round(FOOT[1] - 642 * ACT_SCALE)],
                       "foot": list(FOOT), "source": os.path.basename(ACT_SRC),
                       "keyer": {k: str(v) for k, v in ACT_P.items()}}
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    total = sum(os.path.getsize(os.path.join(ROOT, d, x)) for d in ("frames_idle", "frames_action")
                for x in os.listdir(os.path.join(ROOT, d)))
    print("帧总大小 %.1f MB（%s）" % (total / 1e6, OUT_FMT))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--spec":
        with open(sys.argv[2], encoding="utf-8") as f:
            build_clip(json.load(f))
    else:
        main()

