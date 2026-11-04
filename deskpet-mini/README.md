# 桌宠 · 蓝鲸女仆（deskpet-mini）

把一段绿幕视频抠成透明 PNG 序列，用 PyQt6 无边框窗口贴在桌面上循环播放。双击宠物会播一次动作段，播完自动交叉淡化回待机。

- 纯 Python，一个窗口，无托盘、无状态机、无网络
- 两段素材：待机循环 + 一次性动作
- 三档尺寸，拖到哪就待在哪

## 效果

桌面上（右下角，中档）：

![桌宠悬浮在桌面上](docs/screenshots/01-desktop.jpg)

双击后的动作段（播完自动回待机）：

![动作段](docs/screenshots/02-action.jpg)

两段素材的原始画面对比（角色同一，机位/缩放/绿幕不同）：

![素材对比](docs/screenshots/03-assets-compare.jpg)

抠像边缘放大看（棋盘格 = 透明区）：

![边缘细节](docs/screenshots/04-edge-zoom.jpg)

## 快速开始

前置：Windows 10/11 + Python 3.10 以上。仓库里**不含帧序列**（63.9MB，故意不入库），第一次跑要先重建，大约一两分钟。

```bat
cd deskpet-mini

:: 1) 装依赖
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple

:: 2) 重建帧序列（需要 pillow / numpy / scipy / imageio-ffmpeg）
.venv\Scripts\pip install pillow numpy scipy imageio-ffmpeg -i https://mirrors.cloud.tencent.com/pypi/simple
.venv\Scripts\python build\build_frames.py

:: 3) 启动
run.bat
```

也可以直接命令行启动：

```bat
.venv\Scripts\python pet.py
.venv\Scripts\python pet.py --width 500      :: 固定像素宽度，忽略档位
.venv\Scripts\python pet.py --log build\startup.log   :: 记录首帧时间/内存，用于测性能
```

默认中档：宽度 = 可用屏幕宽的 15%。启动位置在屏幕右下角。

## 操作

| 操作 | 效果 |
|---|---|
| 左键拖动 | 移动位置 |
| 双击 | 播一次动作段（约 5 秒），播完交叉淡化回待机 |
| 右键 → 尺寸：小 / 中 / 大 | 屏幕宽的 10% / 15% / 30% |
| 右键 → 退出 | 关闭 |
| `Esc` | 关闭（需先点一下宠物让窗口拿到焦点） |

没有任务栏图标，`run.bat` 用 `pythonw` 启动，不弹控制台窗口。

## 目录结构

```
zhiling/
├─ deskpet-mini/            主程序
│  ├─ pet.py                运行时代码（窗口、播放、拖动、菜单）
│  ├─ run.bat               双击启动
│  ├─ requirements.txt      运行时依赖
│  ├─ meta.json             画布尺寸、每段 fps/帧数/缩放/脚底锚点
│  ├─ assets/               两段源视频（原文件名保留）
│  ├─ build/                抠像与构建脚本
│  │  ├─ keyer.py           抠像核心（HSV 阈值 + 去溢色 + 连通域 + 羽化）
│  │  ├─ build_frames.py    视频 → PNG 序列 + meta.json
│  │  ├─ compare_clips.py   判断新素材能不能接（机位/绿幕/人物一致性）
│  │  ├─ key_test_action.py 单帧抠像试色卡
│  │  ├─ measure.py         干净进程下的启动耗时/内存
│  │  └─ test_all.py        端到端自测（双击、拖动、切档）
│  ├─ docs/screenshots/     效果截图（README 用）
│  ├─ frames_idle/          ← 生成产物，不入库（96 帧 / 4.4MB）
│  └─ frames_action/        ← 生成产物，不入库（121 帧 / 4.9MB）
└─ deskpet-demo/            早期网页原型（浏览器里跑的 canvas 版）
```

## 帧序列如何重建

```bat
.venv\Scripts\python build\build_frames.py
```

脚本在 `build\build_frames.py` 顶部写死了这些参数，改素材或改抠像效果就改这里：

| 常量 | 作用 |
|---|---|
| `IDLE_SRC` / `ACT_SRC` | 两段源视频路径（默认指向 `assets\`） |
| `IDLE_P` / `ACT_P` | 抠像参数：色相窗口 `hue_in/hue_out`、饱和度/亮度门槛、腐蚀、羽化、透明下限 `floor`、去溢色强度、要抹掉的水印矩形 |
| `ACT_SCALE` / `FOOT` | 动作段相对待机段的缩放，以及两段共用的脚底锚点（保证双击时人物不"跳"） |
| `CROP` / `OUT_W` / `CANVAS_H` | 待机段的裁切框、输出宽度、共同画布高度 |

产物：`frames_idle\f0000..f0095.webp`、`frames_action\f0000..f0120.webp`（576×530，带 alpha），外加刷新 `meta.json`。运行时只读这些帧文件，不再碰视频。

### 输出格式与体积

默认输出 **WebP（有损 q80 + alpha）**，体积约为 PNG 的 1/7。217 帧的 WebP 编码本身约 25 秒（单帧 110~130ms，`method=6`），抠像耗时另算：

| 格式 | 帧总体积 | 相对 PNG | 说明 |
|---|---|---|---|
| PNG（旧默认） | 63.90 MB | 100% | 无损，但半透明渐变让 PNG 几乎压不动，平均 300KB/帧 |
| **WebP q90** | 12.03 MB | 18.8% | 几乎无损 |
| **WebP q80（默认）** | **9.36 MB** | **14.6%** | 576px 下与 PNG 肉眼无差 |
| WebP q60 | 7.52 MB | 11.8% | 深色大面积区域能看到轻微色块 |

q80 的判断依据：同帧在 576px（磁盘原尺寸 1:1）、384px（大档）、192px（中档）三档显示宽度下与 PNG 对比，看不出差别；PSNR 39.3~41.9 dB（29dB 以下才明显）。想调就改 `build\build_frames.py` 顶部的 `OUT_FMT`（可改回 `"png"`）与 `WEBP_Q`。

**注意**：WebP 只省磁盘，不省内存——解码后同样是显示尺寸的 QImage，工作集与读 PNG 基本一样（实测 192px 53.3→54.0 MB，384px 63.9→65.9 MB）。播放器会优先找 `frames_*\f0000.webp`，没有就自动回退读 `.png`，老目录直接能用。

换角色/换视频的顺序建议：先跑 `build\compare_clips.py` 看新素材的绿幕色相和人物高度是否和现有一致，再调 `ACT_P`，用 `build\key_test_action.py` 出单帧试色卡确认边缘，最后 `build_frames.py` 全量。

## 实现要点

- **抠像**：HSV 色相距离做软阈值（不是简单绿通道做差），再叠饱和度/亮度门槛压掉白色和阴影；透明下限 `floor=0.16` 让背景残留变成极淡而不是硬边；溢色按比例从绿通道扣掉。
- **构建期降采样**：源 1280×720 → 输出 576 宽的序列，运行时按档位再 `scaled()`，避免每帧从大图缩。
- **懒加载**：后台线程解码，只在播放窗口附近缓存（预取 8 帧、保留 14 帧），首帧同步加载保证立刻可见。
- **切换**：双击 → 用 `QPainter.setOpacity` 交叉淡化 250ms，不进状态机。

实测（同一台机器，中档 192px 宽）：

| | 首帧可见 | 工作集内存 |
|---|---|---|
| 旧版（384px，96 帧全量预载） | 0.6~1.0 s | 110 MB |
| 现在（中档 192px） | 0.18 s | 55 MB |
| 现在（大档 384px） | 0.20 s | 67 MB |

## 素材来源

| 文件 | 来源 |
|---|---|
| `assets\jimeng-2026-11-04-5883-帮我生成桌面桌宠，绿幕背景，方便进行透明视频转码.mp4` | 即梦 AI 生成（1280×720，4.1s，待机） |
| `assets\HappyHorse 1.1_1793774480874.mp4` | HappyHorse AI 生成（1280×720，5.2s，动作） |

两段都是 **AI 生成的视频素材**，人物形象、视频本身的权利归生成工具/账号方所有，本仓库只存放用于技术演示的抠像结果。**商用或再分发前请自行核对对应平台的条款**（是否有商用授权、是否需要保留 AI 生成水印等）。动作段原片右上/右下带有平台水印，构建脚本在水印矩形内做了透明化处理。

## 已知限制

- **大档内存约 67MB**：3 倍宽度意味着 9 倍像素量，缓存帧数不变但每帧更大。想更省就用中档。
- **透明区域也接收点击**：窗口本身是矩形，抠像出的透明像素仍然会截获鼠标，所以宠物区域会挡住下面的窗口。做真正的鼠标穿透需要 `WA_TransparentForMouseEvents` 或按 alpha 手动过滤，会牺牲拖动/双击，暂未做。
- **待机循环首尾不无缝**：96 帧直接首尾相接，循环处有轻微跳帧（原片本来就首尾不闭合）。想彻底顺需要在 `build_frames.py` 里做首尾交叉淡化或乒乓播放。
- **只测了 Windows**：依赖 PyQt6 的 `FramelessWindowHint` + `WA_TranslucentBackground` 组合，macOS/Linux 未验证。
- **没有托盘图标、开关机自启**，退出只能右键菜单或 `Esc`。

## License

代码部分采用 **Apache License 2.0**（全文见仓库根目录 [LICENSE](../LICENSE)）：可自由使用、修改、商用，需保留版权声明与许可声明。上面"素材来源"里的两段视频不在代码许可范围内。
