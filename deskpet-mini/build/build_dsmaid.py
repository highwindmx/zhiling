"""把 material/DSMaid 的绿幕视频接入成 clip 帧序列（第一批 4 段）。

只做「构建 + 写 meta.json」，**不改 clips.json**：新段是否替换默认待机、以及双击轮播
顺序，需要先在两版里实测通过再登记（见 docs/screenshots 与 README 的接入说明）。

用法（在 deskpet-mini 目录下）：
    .venv\\Scripts\\python.exe build\\build_dsmaid.py
    .venv\\Scripts\\python.exe build\\build_dsmaid.py --only dsh_work

每段的 hue_ref / 饱和度阈值都必须单独采样（这批视频的绿幕色相从 124° 到 148° 不等，
背景还有一路"上绿下浅"的亮度渐变，套用同一组阈值会把发色或裙摆一起抠掉）。
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_frames import build_clip

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSMAID = os.path.join(os.path.dirname(ROOT), "material", "DSMaid")

SPECS = [
    # id, 视频, hue_ref, (sat_lo, sat_hi)
    # 注：这批源片右下角有平台水印（千问 / MINIMAX / 即梦AI），大部分被 HSV 阈值顺带压掉，
    # 剩下一点点留在画布最右下角。要彻底清掉可给 params 加 watermarks=[(x0,y0,x1,y1)]（源图坐标），
    # 但框太大会切到裙摆，需要逐段试，本轮没做（见 README「已知限制」）。
    ("dsh_idle", "待机.mp4", 135.0, (0.06, 0.13)),
    ("dsh_work", "工作中.mp4", 136.0, (0.45, 0.60)),
    ("dsh_happy", "真开心.mp4", 148.0, (0.70, 0.85)),
    ("dsh_unhappy", "不高兴.mp4", 124.0, (0.35, 0.50)),
]


def spec(cid, video_name, hue, sat):
    video = os.path.join(DSMAID, video_name)
    if not os.path.exists(video):
        raise SystemExit("缺少视频：%s" % video)
    params = dict(hue_ref=hue, hue_in=18.0, hue_out=34.0, sat_lo=sat[0], sat_hi=sat[1],
                  val_lo=0.35, val_hi=0.55, erode=1, blur=1.0, floor=0.16,
                  despill=0.7, keep_frac=1.0)
    return dict(id=cid, video=video, params=params, fps=24, width=576, fmt="webp", quality=80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="只构建指定 id")
    args = ap.parse_args()
    for cid, video, hue, sat in SPECS:
        if args.only and cid not in args.only:
            continue
        print("=== %s（%s，hue=%.0f°, sat=%.2f~%.2f）" % (cid, video, hue, sat[0], sat[1]))
        build_clip(spec(cid, video, hue, sat))


if __name__ == "__main__":
    main()
