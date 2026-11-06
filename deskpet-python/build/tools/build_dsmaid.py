"""把 material/DSMaid 的绿幕 mp4 批量转成桌宠帧（走 build_frames.build_clip）。

参数来源是 build/analyze_pack.py 的实测背景值（色相 / 饱和度 / 亮度），每段单独一套，
因为 11 段的绿幕不是一个色号（98°~151°），其中「待机」还是上绿下浅的亮度渐变。

用法：
    .venv\\Scripts\\python.exe build\\build_dsmaid.py            # 全部 11 段
    .venv\\Scripts\\python.exe build\\build_dsmaid.py dsh_idle   # 只重建指定段
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workshop"))   # build_frames 已迁到 workshop/
from build_frames import build_clip                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(os.path.dirname(ROOT), "material", "DSMaid")


def spec(cid, video, hue, sat, val, hue_in=14.0, hue_out=28.0, crop=None):
    params = dict(hue_ref=hue, hue_in=hue_in, hue_out=hue_out,
                  sat_lo=sat[0], sat_hi=sat[1], val_lo=val[0], val_hi=val[1],
                  erode=1, blur=1.0, floor=0.16, despill=0.7,
                  # 只留最大连通块：水印文字（Hailuo AI × MINIMAX 等）和零散光晕会被丢掉，
                  # 不用去硬编码每个片源的水印坐标
                  keep_frac=0.05, watermarks=[])
    s = dict(id=cid, video=os.path.join(SRC, video), params=params,
             fps=24, width=576, fmt="webp", quality=80)
    if crop:
        s["crop"] = crop
    return s


SPECS = [
    # id, 源文件, 背景色相, (sat_lo, sat_hi), (val_lo, val_hi)  —— 值来自 build/search_keyer.py
    # 待机是"上绿下浅"的亮度渐变，色相窗要比别人宽、饱和度下限要低，沿用第一轮验证过的值
    spec("dsh_idle", "待机.mp4", 135.0, (0.06, 0.13), (0.35, 0.55), 18.0, 34.0),
    spec("dsh_happy", "真开心.mp4", 148.0, (0.465, 0.485), (0.386, 0.506)),
    spec("dsh_work", "工作中.mp4", 135.0, (0.326, 0.346), (0.48, 0.60)),
    spec("dsh_unhappy", "不高兴.mp4", 124.0, (0.268, 0.288), (0.35, 0.55)),
    spec("dsh_proud", "得意状.mp4", 138.0, (0.261, 0.281), (0.49, 0.61), 12.0, 24.0),
    spec("dsh_think", "思考状.mp4", 98.0, (0.092, 0.112), (0.343, 0.463), 12.0, 26.0),
    spec("dsh_yawn", "打哈欠.mp4", 120.0, (0.172, 0.192), (0.35, 0.55)),
    spec("dsh_helpless", "无奈状.mp4", 141.0, (0.237, 0.257), (0.245, 0.365)),
    spec("dsh_phone", "看手机.mp4", 111.0, (0.174, 0.194), (0.35, 0.55)),
    # 吃米饭：两侧是"深绿色"遮幅（不是纯黑，所以按亮度阈值扫不到），并集 172/165，留 2px 余量
    spec("dsh_rice", "吃米饭.mp4", 151.0, (0.108, 0.128), (0.172, 0.292), 14.0, 28.0,
         crop=(174, 0, 665, 480)),
    # 炸毛：两侧纯黑遮幅，逐帧 167~172px 晃，取全片并集 172/172 后再留 2px 余量，
    # 只裁一次（不逐帧裁，否则脚底会随黑边宽度抖动）
    spec("dsh_shock", "炸毛.mp4", 96.0, (0.172, 0.192), (0.348, 0.468), 12.0, 26.0,
         crop=(174, 0, 654, 480)),
]

BATCH1 = {"dsh_idle", "dsh_happy", "dsh_work", "dsh_unhappy"}


def main():
    want = set(sys.argv[1:]) or {s["id"] for s in SPECS}
    total = 0
    for s in SPECS:
        if s["id"] not in want:
            continue
        if not os.path.exists(s["video"]):
            print("[跳过] 找不到 %s" % s["video"])
            continue
        print("== %s <- %s" % (s["id"], os.path.basename(s["video"])))
        r = build_clip(s)
        total += r["bytes"]
    print("合计 %.2f MB" % (total / 1e6))


if __name__ == "__main__":
    main()
