# deskpet-python · 桌宠（Python 版，主线）

## 1. 这是什么

一只「活泛的」透明桌面宠物：素材来自 **即梦 AI / MiniMax Design 生成的短视频**（`material\DSMaid\*.mp4`，共 15 段），
用**素材工坊**把绿幕抠掉、得到透明帧序列，再用主题映射 `themes\<id>.json` 把 DSH 的状态事件**绑定**到具体动画，
最后由 `pet.py` 在桌面上透明置顶地播放。**Python 版是主线，Tauri 版只是另一种打包形态。**

整条素材链路是因果关系，不是功能罗列：

```
短视频（绿幕 mp4，15 段）
   │  ① 素材工坊 · 裁黑边：整片统一裁一次（逐帧裁会让脚底抖）
   │  ② 素材工坊 · 取绿幕色：四角取样 → hue_ref / sat 闸门
   │  ③ 素材工坊 · 抠像：色相 + 饱和度 + 亮度三闸门 → alpha，去溢色、软地板、连通块清理
   │  ④ 素材工坊 · 生成 WebP：抠好的帧按脚底锚点对齐、缩放到 pack 画布（576×530）
   ▼
packs\animations\<assetId>\f0000.webp …        （透明帧序列，不入库、可随时重建）
   │  ⑤ 状态绑定：themes\<id>.json 把 idle / busy / done / error … 指向 clipIds[]
   ▼
pet.py / deskpet-tauri.exe  →  桌面上会动的宠物
```

换句话说：**视频是原料，工坊是加工厂，packs 是成品仓库，主题映射是「什么情况播哪段」的接线图，pet.py 是放映机。**
任何一环改了都得往下游重新生成，详见 §3 与 §6 的「工坊调了、桌宠没变」。

> 完整素材包不随代码入库。首次启动找不到素材时会自动把 `sample_packs\sample`（仅 idle 一段）装到 `%LOCALAPPDATA%\DeskPet\packs`，保证先出画面；完整包安装、放置方式与常见问题见仓库根 [README](../README.md) 的「获取与安装素材包」。

## 2. 项目文件夹结构

```
zhiling\
├─ material\DSMaid\              原始素材：15 段绿幕 mp4（即梦 / MiniMax Design 导出），只读参考
├─ deskpet-python\               ← 本文档所在，Python 主线
│  ├─ pet.py                     桌宠主程序：透明窗口、动作调度、右键菜单、托盘、状态输入
│  ├─ state_map.py              状态映射加载与仲裁（DSH 事件 → clipId）
│  ├─ run.bat / run.vbs         双击入口（run.vbs 完全无黑窗；run.bat 会闪一下 cmd）
│  ├─ config.json               旧版本机偏好（已停用）；首次运行合并进 %LOCALAPPDATA%\DeskPet\config.json
│  ├─ pyproject.toml / uv.lock 依赖与锁定版本（Python ≥ 3.11）
│  ├─ packs\                   成品素材包（可整体搬走）
│  │  ├─ active.json            可选的兼容镜像（v2 以主题文件里的 active 标记为准）
│  │  ├─ themes\<id>.json       主题映射：13 状态 → clipIds[] + 调度参数（运行时可热加载）
│  │  └─ animations\<assetId>\  meta.json（名称/画布/fps/帧数）+ f0000.webp…（**不入库**）
│  ├─ workshop\                素材工坊与抠像核心（GUI + 命令行工具）
│  │  ├─ workshop.py           工坊 GUI
│  │  ├─ keyer.py             抠像核心 key_frame()
│  │  ├─ build_frames.py       抽帧 + 抠像 + 对齐 + 写 WebP
│  │  ├─ _accept.py / test_workshop.py  工坊验收 / 测试
│  │  └─ presets\             工坊参数预设（*.json，可入库）
│  ├─ tests\                   纯逻辑测试（state_map / dsh_bridge 映射），`pytest -q` 运行
│  ├─ v2_constants.py         跨端共享常量（13 状态 / 阈值 / 端口），与 Tauri 前端 shared.js 对齐
│  └─ build\
│     ├─ tools\                命令行工具（全部入库）
│     │  ├─ pack.py            读写 pack 结构
│     │  ├─ preview_key.py     单帧预览（改参数不重跑整段）
│     │  ├─ dsh_bridge.py      DSH 事件桥：订阅 ws://127.0.0.1:55471 → POST /state
│     │  └─ tray_probe.py      托盘最小探针（10 秒自检，区分系统/Qt 与 pet.py 问题）
│     └─ cache\                抽帧缓存（**不入库**，可整删，重建约 44 秒/96 帧）
└─ deskpet-tauri\               Tauri 2 版：单 exe + 外置 assets\（见 §5）
```

**不入库的目录**（`.gitignore` 已覆盖，删了都能重建）：

| 目录 | 原因 |
|---|---|
| `.venv\` | 由 `uv sync` 重建 |
| `build\cache\` | 抽帧/探针中间产物，删了从 `material\` 重抽 |
| `packs\*\frames\`、`packs\*\.backup\`、`packs\.legacy\` | 生成物，体积大；由 manifest + 源视频可完整重建 |
| `docs\`、`deskpet-python\docs\`、`deskpet-tauri\docs\` | 设计文档与验证截图，本地保留，不推 GitHub |
| `**\build\screenshots\`、`screenshots\` | 测试截图一律不入库、不上传 |

## 3. 构建：从零到能跑

```powershell
# 0) 只装一次 uv（已有可跳过）
winget install --id=astral-sh.uv -e

cd D:\Share\Scripts\Explore\zhiling\deskpet-python
uv sync                                    # 创建 .venv 并装依赖（PyQt6 / Pillow / numpy / scipy / imageio-ffmpeg / pypinyin）

# 1) 做素材：打开素材工坊
uv run python workshop\workshop.py
# 或双击 workshop.bat（优先 uv，回退 .venv）

# 2) 确认启用的 pack
notepad packs\active.json                  # {"pack": "maid-dsh"}

# 3) 运行
uv run python pet.py --width 300
```

素材工坊四步，每步都有明确产出：

| 步骤 | 你在界面上做什么 | 这一步产出什么 |
|---|---|---|
| ① 裁黑边 | 勾「自动裁黑边」（默认开）。工具扫全片 1fps 探针帧，取近黑/深色均匀边缘带的**并集**裁一次 | 一个整片共用的 `crop`（如吃米饭 `[174, 0, 665, 480]`），写进 `manifest.json` 的 `clips[].crop` |
| ② 取绿幕色 | 点「取背景色」。在**裁边后**的画面四角取样，回填 `hue_ref`，并在背景饱和度偏低时自动把 `sat_lo / sat_hi` 压到背景饱和度之下 | `hue_ref`（吃米饭实测 99.2°）与一组能真正抠掉背景的饱和度闸门（实测 0.13 / 0.18） |
| ③ 抠像 | 切「抠像后 / alpha」看效果，视需要调「高级参数」（色相内/外、饱和度、软地板、去溢色、羽化） | 每帧的 alpha 通道：`bg = min(kh, ks, kv)`，再去溢色、软地板、连通块清理留下主体 |
| ④ 生成 WebP | 选目标 pack、画布（576×530）、fps、质量（q80）；点生成 | `packs\<packId>\frames\<clipId>\f0000.webp …` + 合并后的 `manifest.json / clips.json` |

- 第 ④ 步的帧会按脚底锚点对齐并缩放进 576×530 画布，同一 pack 内**所有段必须同画布**，跨 pack 不能混用帧。
- 工坊不是运行时依赖：桌宠只读 `frames\`。同 `clipId` 重抠会覆盖旧帧（旧帧进 `packs\<packId>\.backup\`）。
- 不想用 GUI 时可以走命令行：`uv run python build\tools\pack.py --help`。
- 抽帧依赖随 `uv sync` 一起装好（`imageio-ffmpeg` 自带二进制），无需配置；找不到时回落到 `PATH` 的 `ffmpeg`。两者都没有时工坊弹窗说明“抽帧不可用、仍可浏览已有素材”，并提示 `uv sync` 重装依赖或安装 ffmpeg 到 `PATH`。共享配置里已有的 `ffmpegPath` 不读、不写、不删除。

## 4. 运行过程

```powershell
run.vbs                                 # 推荐：双击无任何黑窗（wscript 隐藏启动 pythonw）
run.bat                                 # 备用：会闪约 0.5s 的 cmd 窗口（bat 自身的启动开销）
uv run python pet.py --width 300        # 前台跑，便于看日志
uv run python pet.py --pack maid-basic  # 临时换 pack（不写 active.json）
uv run python pet.py --verbose --log build\cache\pet.log
```

`pythonw` 没有控制台，所以默认日志固定写 `build\cache\pet.log`（随目录走、已 gitignore，排障时直接打开）；
托盘诊断另写 `build\cache\pet_tray.log`。未捕获异常（含子线程）也会进 `pet.log`。`run.vbs` 也支持参数：
`wscript run.vbs --state-port 8765`。

运行期行为要点：

- **透明置顶**：无边框、置顶、不抢焦点；画布 576×530，按 `--width` 缩放（`--width 300` 时窗口约 192×177）。
- **空白穿透**：透明区域用掩码命中测试近似「点穿到桌面」；已知限制见 §7。
- **尺寸档位 / 默认尺寸**：右键或托盘「尺寸」小/中/大（设置里不再提供尺寸项）；选择写入 `%LOCALAPPDATA%\DeskPet\config.json` 的 `tier`（与 Tauri 版共用，`DESKPET_CONFIG` 可覆盖路径），下次启动沿用。
- **右键菜单**：`动作 ▸`（按 工作 / 情绪 / 特殊 分组，名字过长截断，悬停看完整名+clipId）、`尺寸 ▸`、`设置…`、`退出`。素材工坊与「关于素材」不在桌宠右键里。
- **双击轮播**：双击宠物循环播下一段动作；Esc 退出（需先点一下让窗口拿到焦点）。
- **设置对话框**（右键「设置…」）：只有两个页签；「恢复上次配置 / 保存配置 / 取消」**紧跟页签、左对齐**，
  其中「恢复上次配置」用描边浅色的次要按钮样式；没有未保存的配置改动时「保存配置」为禁用；
  关窗（X 或取消）时若仍有未保存的配置改动会先确认。说明性文字收在控件 tooltip 里。
  - **状态与动画**：顶部是**主题选择器**（当前主题下拉 + 「重命名主题…」+「新建主题…」直达工坊新建对话框），
    其下是**可排序状态列表**（13 行，`long_idle_1` / `long_idle_2` 各自一行）、左右分栏预览与多段动作池。
    **越靠上越优先**；拖拽或「上移/下移」调整顺序，保存时按顺序等距重算 `priority`（从 100 开始每档 −10），
    写回 `packs\themes\<id>.json`。
    **备份只保留单份**：每次保存前把现有文件覆盖到 `packs\themes\<id>.json.bak`，不再按时间戳累积；
    「恢复上次配置」读这份 `.bak` 回填界面，确认后再点「保存配置」才生效。
    每行显示缩略图 + 状态名 + 当前段；**没有「启用」勾选框**——动作池为空即该状态不绑定（到达时保持当前画面），
    旧主题里的 `enabled` 字段读取时忽略、保存时不再写出。
    选中行可增删/排序动作池（带缩略图与段数）、预览播放、
    「＋ 用视频做一段新动作」（带当前状态进工坊，生成完自动绑回）。番茄钟的 `focus` / `focus_break` 也在这里绑定。
    底部「素材与工具」只保留**素材目录**（只读路径 + 「浏览…」+「恢复默认」，写入 `config.json:packsDir`）；ffmpeg 不再暴露配置项，抽帧依赖随 `uv sync` 装好。
    状态列表下方的**排序行**左侧是「提醒态常驻」勾选框（`config.json:remindHold`），右侧是
    「上移状态 / 下移状态 / 恢复默认排序」（同一行）；完成 / 出错 / 待你决策 未点击时持续播放、不回 idle，
    它不是状态行，不参与拖拽排序与 `priority` 重算。
  - **提醒与连接**：番茄钟总开关、专注 / 休息时长（默认 25 / 5 分钟）；DSH 链路指示灯 + 「重新检测」（`:55471` → `dsh_bridge.py` → `/state`，不新增端口探测）。
- **托盘**：托盘图标（`assets\icon.ico`，紫色圆角矩形 `#6D4BD6` + 黑体「宠」字，多尺寸 16–256）常驻；右键出菜单（显示/隐藏、动作 ▸、尺寸 ▸、番茄钟 ▸、素材工坊…、设置…、退出），**双击图标 = 显示/隐藏**。
  白字是默认（紫底对比度更高）；想要黑字就用 `assets\icon_black_text.ico` 覆盖 `icon.ico`，或改 `build\tools\make_icon.py` 顶部常量后重跑脚本。
  隐藏只停动画、不退进程（`setQuitOnLastWindowClosed(False)`）；真正退出走托盘「退出」或 Esc。
  **看不到图标时按顺序查**：① 点任务栏右下角 `^`（Win11 默认把新图标折叠进溢出菜单）；
  ② 设置 → 个性化 → 任务栏 → 其他系统托盘图标，把「知灵 · 桌宠」设为开；③ 看 `build\cache\pet_tray.log` 的
  `available / visible / icon_actual16 / geometry`；④ 仍不行就跑 `build\tools\tray_probe.py`（10 秒自检）——
  探针里也看不到就是系统/Qt 层问题，与 pet.py 无关。
- **状态映射**：`packs\themes\<id>.json`，运行中每 800ms 检查一次文件变更并**热加载**（改完不用重启）；
  改 `clipIds` / `priority` 即时生效。改当前启用主题（`active` 标记）必须**重启**。
- **实测数字**（本机 `--log`，`--width 300`）：首帧绘制 **104 ms**、6 秒工作集 **56.9 MB**、峰值 **58.8 MB**、15 段动作全部通过首帧尺寸校验。
  注：用户此前一次实测为 156 ms / 62 MB，属批次与环境差异，以你本机 `--log` 输出为准。

### 4.1 让 DSH（或任何脚本）驱动桌宠

桌宠默认只读主题映射（`themes\<id>.json`），不会自己知道 DSH 在干什么；要联动必须把状态**发进来**。两种零依赖方式，启动时二选一或都给：

```powershell
uv run python pet.py --state-port 8765 --log build\cache\pet.log
uv run python pet.py --state-file build\cache\state.json --log build\cache\pet.log
```

状态名必须是主题映射 `themes\<id>.json` 里已有的键（`streaming` / `needs_decision` / `done` / `error` / `busy` …）。`priority` 不传就用映射表里的值；传了只影响这一次仲裁，低优先级不会打断正在播的高优先级动作。`timeoutSec` 可选，到点没收到新状态就回落 `idle`。

```powershell
# PowerShell：流式输出 → 工作中；需要用户决策 → 举手
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/state -ContentType 'application/json' `
  -Body '{"state":"streaming","priority":30}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/state -ContentType 'application/json' `
  -Body '{"state":"needs_decision","priority":55,"timeoutSec":120}'
```

```javascript
// Node 18+：同样的两个调用
fetch("http://127.0.0.1:8765/state", {method: "POST", headers: {"Content-Type": "application/json"},
       body: JSON.stringify({state: "streaming", priority: 30})});
```

```powershell
# 不想开端口：写文件（内容变化才应用，纯文本也认）
Set-Content -Path build\cache\state.json -Encoding UTF8 -Value '{"state":"streaming"}'
Set-Content -Path build\cache\state.json -Encoding UTF8 -Value 'needs_decision'
```

未知状态只写日志、不切画面（`state_in[http] recv state='dancing' ... ok=False`）。

### 4.2 自动接 DSH：`dsh_bridge.py`（依赖 DSH 侧插件）

DSH 真正在跑的时候，事件不是从桌宠这一端来的：仓库里的 `dsh-deskpet-bridge`（DSH 插件）把
`welcome / status(running|idle) / token / done / error` 广播到 `ws://127.0.0.1:55471`。桌宠本身不订阅 WebSocket，
所以**默认不会自动跟 DSH 变**；需要这个桥常驻：

```powershell
# 1) 桌宠开状态入口（run.vbs 无黑窗）
wscript run.vbs --state-port 8765
# 2) 桥：DSH 事件 → POST http://127.0.0.1:8765/state
.venv\Scripts\python.exe build\tools\dsh_bridge.py
# 3) 先确认两端在不在（不改任何状态）
.venv\Scripts\python.exe build\tools\dsh_bridge.py --probe
```

映射：`status running→busy`、`status idle→idle`、`token→streaming`（整轮期间用 `BRIDGE_TTL_BUSY_SEC=120` 兜底，工具跑再久也不掉 idle）、`done→done`、`error→error`（10s 回落）。
前置条件只有两个：DSH 已加载该插件（55471 在监听）、桌宠带了 `--state-port`。桥只连本机回环、无第三方依赖。
`--probe` 会直接打印是「DSH 没广播」还是「桌宠没开端口」。不装插件就必须 DSH 侧自己发 HTTP 或写状态文件（见 4.1）。

插件本体：仓库外 `C:\Users\<你>\.dsh\profiles\desktop\node_modules\@local\dsh-deskpet-bridge`
（指向仓库 `zhiling\dsh-deskpet-bridge`，后者已 gitignore）。它在 `profiles\desktop\package.json` 里登记为依赖 + bundle；
不装/未启用时 55471 不会监听，桥会一直重连——不影响桌宠本身，只是没有自动状态。
改完 profile 必须**重启 DSH NEXT** 才生效。另：曾经同时登记的 `@local/dsh-probe` 指向已不存在的目录
（broken junction），会让 bundle 链在它之后中断；已清理该 junction 与两条登记（备份在 `build\cache\dsh-profile-package.backup-20261105.json`）。

## 5. 状态映射怎么改

`packs\themes\<id>.json` 的 `states` 是对象，一个键一个状态（共 13 个；`long_idle_1` / `long_idle_2` 各自独立，
另有番茄钟的 `focus` / `focus_break`）：

| 状态 | 触发 | 动作 | 优先级 |
|---|---|---|---|
| `idle` | 无事 | 待机 `dsh_idle` | 0 |
| `long_idle_1` | 10 分钟无操作 | 累到瘫倒 `dsh_tired` | 10 |
| `long_idle_2` | 30 分钟无操作 | 打哈欠 `dsh_yawn` | 12 |
| `awaiting_user` | done 后 30s 没回应 | 思考状 `dsh_think` | 25 |
| `streaming` | 模型流式输出 | 工作中 `dsh_work` | 30 |
| `busy` | 模型在忙 | 思考状 `dsh_think` | 35 |
| `done` | 完成 | 递交文件 `dsh_handover` | 45 |
| `needs_decision` | 只认 DSH `attention` 事件 | 举手提问 `dsh_handup` | 55 |
| `error` | 出错 | 不高兴 `dsh_unhappy` | 60 |
| `click` | 单击 | 得意状 `dsh_proud` | 70 |
| `double_click` | 双击 | 炸毛 `dsh_shock` | 85 |
| `focus` | 番茄钟专注 | 专注动作（可不绑） | 32 |
| `focus_break` | 番茄钟休息 | 休息动作（可不绑） | 22 |

改法：把某状态的 `clipIds` 换成 `animations\` 下任意 assetId（共 15 段：另有 `dsh_happy` 真开心、`dsh_helpless` 无奈、`dsh_phone` 看手机、`dsh_rice` 吃米饭、`dsh_cheer` 高兴跳起 等），
或用空数组清空某状态的 `clipIds`（空池 = 不绑定，到达时保持当前画面）。`needs_decision` 只认真实事件，启发式已被否决，不要再加。

**为什么要 priority（一句话版）**：动作不是「来了就立刻换」，因为两个状态可能在 1 秒内撞车——
比如正在 `streaming`（输出中，正在播 `dsh_work`）时突然收到 `needs_decision`（要你确认）。
priority 就是这时候的排队规则：数值大的先播、小的被拒且**不会打断**正在播的高优先级动作；等它播完/冷却结束再轮到下一个。
`enabled` 字段已废弃：设置里既不显示也不写出；「先别用这条映射」现在用**空动作池**表示（到达时保持当前画面）。

在设置面板里不需要填数字：**列表越靠上 priority 越大**。保存时按列表顺序等距重算（100/90/80…/10），
`long_idle_1` / `long_idle_2` 各自独立；若你手工改过 `themes\<id>.json` 且数值不等距，打开设置会**按数值从大到小**展示，
保存时再等距重写，并在日志里打一行 `settings priority old→new` 供回滚。

## 6. Tauri 版

- 定位：单 exe + 外置素材目录，体积与内存更小；素材不放 exe 内，运行时解析顺序：`DESKPET_PACKS` → 设置里的「素材目录」→ `%LOCALAPPDATA%\DeskPet\packs` → exe 同级 `assets\`（缺素材会显式报错）。
- **托盘：已实现**（`Cargo.toml` 启用 `tray-icon`，`main.rs` 里 `TrayIconBuilder` 构建状态/尺寸/番茄钟/工坊/设置/退出菜单）。
- 环境变量：`$env:DESKPET_PACKS = "D:\...\deskpet-python\packs"`，然后直接运行编译出的 `deskpet-tauri.exe`（或 `cd deskpet-tauri\src-tauri && cargo run --release --target-dir "$env:TEMP\deskpet-target"`）。
- **免 ffmpeg**：素材工坊改用 Windows Media Foundation 解码（前置对齐的话不装 ffmpeg 也能用 Tauri 版工坊）；仅支持 mp4/mov 的 H.264/HEVC，webm/VP9/AV1 会提示转码或改用本 Python 版工坊。
- 编译已验证（用户实测可产出可运行 exe）。
- 运行期（透明、穿透、播放）**未做自动化验证**，只做过人工确认。
- NSIS 安装包**尚未产出**。

## 7. 授权

- **代码**：GPL-3.0（见仓库根 `LICENSE`）。
- **素材**（`material\` 下的 mp4 与 `packs\*\frames\` 里的帧）：`all-rights-reserved`，**不适用 GPL**；仅限本机自用，不得商用、不得随代码一起再分发。

## 8. 已知限制与排错

**已知限制**

- 透明区域仍会挡住鼠标：Qt 的 `WA_TransparentForMouseEvents` 是整窗生效，只能用掩码命中测试近似，未根本解决。
- 工坊的「高级参数」有一组固定项（`val_lo/val_hi/erode/keep_frac`）不暴露在 UI 里；`erode` 目前固定为 1，想在成品里用 0 得改代码或直接改 manifest 后走命令行重建。
- 预览会先把长边缩到 560 再抠像，因此预览的边缘与全分辨率成品会有细微差异（验收以 `preview_key.py` 的全分辨率指标为准）。
- 部分素材自带水印，工坊只能「盖掉」（`watermarks` 区域 alpha 归零），不能真正去除。

**排错**

- **看不到托盘图标**：先点任务栏 `^` 展开（Win11 默认折叠）→ 设置 → 个性化 → 任务栏 → 其他系统托盘图标 里打开；
  再查 `build\cache\pet_tray.log`（`available=True visible=True actual16=16x16` 说明 Qt 已注册，问题在系统折叠区）；
  最后跑 `build\tools\tray_probe.py`，10 秒内探针也看不到就是系统/Qt 层问题，不是 pet.py。
  启动多个 pet.py 会在 `pet_tray.log` 里出现 `WARN another pet instance`，先退出多余的实例。
- **双击 run.bat 闪黑窗**：bat 自身必然创建 cmd 窗口（本机实测约 0.45–0.85s），改用 `run.vbs`（wscript 隐藏启动，无任何窗口）。
- **DSH 在跑但桌宠不动**：桌宠默认不订阅 DSH。先 `dsh_bridge.py --probe`：55471 未监听 = 插件没加载，
  8765 未监听 = 桌宠没带 `--state-port`；两者都在再启动 `dsh_bridge.py`（见 §4.2）。
- **黑窗 / 没有素材**：`packs\active.json` 指的 pack 不存在，或 `frames\` 缺失；看 `--verbose` 输出。
- **改了 `active.json` 不生效**：主题切换只在启动时读取，必须重启；`themes\<id>.json` 才会热加载。
- **`.venv` 被占用**：先退出正在跑的桌宠，再 `uv sync`。
- **工坊里调了参数但桌宠没变**：工坊改的是「素材生成」，桌宠读的是「已经生成好的帧」——
  要在工坊里点了**生成**（重建该段全部帧），再重启 `pet.py`（或至少切一次动作让进程重新读帧）。只调参数不生成，桌宠不会变。
- **想快速试参数、不想重跑整段**：
  ```powershell
  uv run python build\tools\preview_key.py --clip dsh_rice --frame 62 --despill 1.0
  ```
  默认取 `manifest.json` 里该段的 `keyer`，命令行同名参数覆盖它；输出在 `build\cache\probe\preview\`，并打印可见绿 / 绿雾 / 亮白被啃 / 边框 alpha 四项指标。
- **tauri 的 `target` 写入被拒**：用 `--target-dir %TEMP%` 或结束旧进程后重试。
