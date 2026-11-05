# 桌宠 · 蓝鲸女仆（deskpet-tauri）

Tauri 2 + 零构建前端的透明桌宠，与 [deskpet-mini](../deskpet-mini/)（PyQt6）**行为等价**：同一份素材、同一套尺寸/锚点换算、同样的拖动 / 双击轮播动作 / 原生右键菜单 / Esc 退出。这个目录本轮的目标是"跑起来 + 可测"，不含 DSH 对话桥接、托盘动画、多角色、AI 功能。

## 从零构建（`Cargo.toml` 在哪、要跑哪几条命令）

**先说位置：`Cargo.toml` 在 `deskpet-tauri\src-tauri\Cargo.toml`，不在 `deskpet-tauri\` 根目录。**
这是 Tauri 的固定布局：前端目录（`frontend/`）和 Rust crate（`src-tauri/`）平级，`Cargo.toml` 跟 crate 走。
所以直接 `cd deskpet-tauri && cargo build --release` 会报 `could not find Cargo.toml`，必须进 `src-tauri\` 或加 `--manifest-path`。

**前端不需要 `npm install` / `npm run build`**：`frontend/` 是零构建的 ES Module（没有 vite/webpack 编译步骤），
`package.json` 里的 `@tauri-apps/cli` 只在 `npm run build`（打 NSIS 安装包）或 `tauri dev` 时才用得到。
没有 Node 的环境照样能出 exe。

前置只有两样：Rust（stable，MSVC 工具链，含 VS Build Tools 的 C++ 桌面组件）+ Python 3（`prepare_assets.py` 只用标准库）。
另外素材前提：`deskpet-mini/frames_*` 已生成（帧序列不入库，见 [deskpet-mini/README.md](../deskpet-mini/README.md)）。

三条命令（在 `deskpet-tauri\` 下逐条执行）：

```bat
:: 1) 同步素材：把 deskpet-mini\frames_* 拷到 frontend-dist\assets，并生成合并版 clips.json
python build\prepare_assets.py
::    idle 96 / action 121 / action2 107 / action3 107 帧，canvas 576x530，共约 19.6MB

:: 2) 进 crate 目录再编译（.cargo/config.toml 的 crates.io 镜像只在 crate 目录内生效）
cd src-tauri
cargo build --release --target-dir %TEMP%\deskpet-tauri-target

:: 3) 运行产物
%TEMP%\deskpet-tauri-target\release\deskpet-tauri.exe
```

**为什么第 2 条要 `--target-dir %TEMP%`**：本仓库目录带 `Everyone Deny DeleteSubdirectoriesAndFiles` ACE，
cargo 增量构建时无法原子覆盖 `target\release\.fingerprint\*`，会直接报 `拒绝访问 (os error 5)`。
把 target 指到 `%TEMP%`（可写）就绕开了；不带这个参数时产物会落在 `src-tauri\target\release\deskpet-tauri.exe`（本机这条路会失败）。
想换位置：`set DESKPET_TARGET_DIR=D:\some\writable\dir`，`build.bat` 会读这个环境变量。

**`prepare_assets.py` 什么时候必须跑**：任何一次 `cargo build` 之前。原因有两条——
前端资源是**编译期**由 tauri-build 内嵌进 exe 的，运行时读不到 `../deskpet-mini/`；不跑这一步 `frontend-dist\` 不存在，编译会失败或内嵌不到素材（窗口起来是空的）。
换了素材（改 `clips.json`、重生成 `frames_*`）之后也要重跑，然后再 `cargo build`。

**一键脚本**：`build.bat`（在 `deskpet-tauri\` 根目录）等价于上面 1+2 两步，每步失败都会带原因停下、不吞错：

```bat
build.bat            :: release
build.bat debug      :: debug
```

`build.bat` 的产物同样在 `%TEMP%\deskpet-tauri-target\release\deskpet-tauri.exe`；它不会替你 `git push`，也不会打安装包（见「踩过的坑」第 8 条）。

## 运行

```bat
src-tauri\target\release\deskpet-tauri.exe
:: 或根目录 run.bat：给受限会话准备一个可写的 WebView2 用户目录
```

需要系统已安装 **WebView2 Runtime**（Win11 通常自带；本机为 156.0.4314.58）。窗口默认中档 = 屏幕可用宽 15%，贴在工作区右下角（逻辑边距 24 / 12）。

## 交互

| 操作 | 效果 |
| --- | --- |
| 左键拖动 | 原生拖动（`startDragging()`），停在哪待在哪 |
| 双击 | 待机时按 `priority` 播放下一个动作，播完交叉淡化回待机**续播位** |
| 右键 | 原生菜单：`尺寸：小/中/大`（当前档带 ✓）、`动作：真开心/工作中/不高兴`、`退出` |
| 点宠物旁边的透明区 | **穿透**：事件直达下面那个窗口，宠物不响应（见「空白处穿透」） |
| `Esc` | 退出 |

三档 = 屏幕可用宽的 10% / 15% / 30%，下限 140 逻辑像素（与 mini 的 `max(140, ...)` 一致）；切档保持窗口左上角，只有启动时贴右下角。

## 空白处穿透（点宠物=正常，点旁边透明区=直达下层窗口）

**做法**：window 级开关（Rust `set_ignore_cursor_events`，Win32 是 `WS_EX_TRANSPARENT`）+ 前端降采样 alpha 掩码判定指针是否在宠物身上，50ms 轮询一次光标（`cursor_local` 命令返回窗口内坐标），命中状态变化时才 `invoke("set_click_through")`。

**为什么不做 `WM_NCHITTEST` 子类化**：
1. Tauri 2 不暴露窗口过程，得自己 `SetWindowSubclass` 并把 WNDPROC 里的每个像素判定接到 JS 掩码上——要么每帧把掩码 IPC 到 Rust（24fps × ~8KB），要么在 Rust 里再实现一遍降采样，两份实现必然漂移；
2. 先在 PyQt6 里试过同一思路（`QWidget.nativeEvent` 收 `WM_NCHITTEST`），实测 **Qt6 在原生窗口层就把这条消息处理掉了**，`nativeEvent` 一次都收不到（同时 `super().nativeEvent()` 会 `0xC000041D` 崩），说明"框架级命中测试"这条路在两边都不稳。
`set_ignore_cursor_events` 是官方 API、语义就是"整个窗口不吃鼠标"，配合掩码只在指针越过轮廓时切换，代价是一次 IPC + 一次 `SetWindowLong`，没有逐像素跨进程通信。

**掩码**：`MASK_DIV=2`（192×177 → 96×88）、`MASK_ALPHA_MIN=8`；离屏 canvas 用 `drawImage` 缩小后 `getImageData` 取 alpha；**按帧缓存**（key = `clip:idx`，LRU 600），`MASK_REFRESH_MS=150` 节流，只有帧序号变了才重建。所以待机段第一圈 96 次构建之后就不再重建，稳态是 0 次/秒；重建一次 ≈ 一次 96×88 的 `drawImage` + 一次 `getImageData`（同口径在 mini 侧实测 0.95~1.12ms/次，见下）。

**实测**（构建产物 exe + 最大化 WinForms 对照窗口，真实光标 + `mouse_event` 注入，日志在 `build/bench-logs/`）：

| 检查 | 结果 |
| --- | --- |
| 宠物身体单击 | 底层窗口**收不到** `mousedown`（0 行新增） |
| 宠物身体双击 | 前端收到 `dblclick` 并切动作（`e2e-tauri.log`） |
| 宠物身体按下并拖动 | 窗口原点 `(1063,562) → (1018,526)`，`startDragging` 生效 |
| 紧邻透明区（身体右 2px 外）单击 | 底层窗口**收到** `mousedown`（0→1 行） |
| 窗口四角空白 | `WindowFromPoint` 返回下面的 IDE 窗口，不是宠物 |
| 边缘 ±2px | `edge+2` 归宠物；`edge-2` 已在掩码外——**偏保守**（宁可多算 2px 给宠物，也不误判穿透） |

`MASK_DIV=2` 的量化误差最多 2 逻辑像素，所以"1~2px 内不误判"是靠"向内取整"保证的：掩码按 2×2 块合并，块内只要有一个不透明像素整块算命中。

**局限**：轮询间隔 50ms，指针从透明区快速划到宠物身上时最坏有 50ms 的"这一段仍穿透"窗口（表现为第一下点击可能漏到下层窗口）；要消掉得改成 `WM_NCHITTEST` 或 `mouseenter` 级别的低延迟钩子，本轮没做。

## 与 deskpet-mini 的差异

| 维度 | deskpet-mini（PyQt6） | deskpet-tauri |
| --- | --- | --- |
| 素材 | `frames_*/` + `meta.json`，WebP q80 | **同一份**（构建时由 `build/prepare_assets.py` 拷入并内嵌进 exe） |
| 帧清单 | `clips.json`（顺序/类型/名字）+ `meta.json`（帧数/fps/格式） | `frontend-dist/assets/clips.json` 一份自包含清单（生成物，不手改） |
| 播放 | `QTimer` 24fps，后台线程解码，缓存 ±14 帧 | `requestAnimationFrame` 24fps，`ImageBitmap` **整段常驻** |
| 交叉淡化 | `QPainter` opacity 250ms | 双 canvas 交叉 + CSS opacity 250ms |
| 尺寸/锚点 | `_resize_to(max(140, 屏宽×比例))`，右下 -24/-12 | 同一算法，但物理像素定位在 Rust 侧做（避开 WebView CSS/DPI 误差） |
| 右键菜单 | Qt 原生 `QMenu` | Tauri 原生 `Menu` + `popup_menu`（不用 HTML 菜单，避免被 192px 窗口裁切） |
| 拖动 | `mouseMoveEvent` 里 `move()` | `startDragging()`（由窗口管理器接管） |
| 退出 | 菜单 / `Esc` | 菜单 / `Esc` |

`clips.json` 没有直接复用 mini 那份，原因：mini 的清单只放 id/name/type/loop/priority，帧数/fps/格式散在 `meta.json` 里，且 mini 是本地 `open()` 读；前端只能 `fetch` 一个文件，所以由脚本合并成一份等价清单（字段语义一致，顺序/优先级/名字全部来自 mini）。

## 实测对比（同口径）

测量脚本：[build/bench.py](build/bench.py)（体积/内存/CPU/首帧）、[build/bench-chart.ps1](build/bench-chart.ps1)（出图）。

```bat
python build\bench.py --seconds 60
powershell -NoProfile -File build\bench-chart.ps1
```

条件：同窗口尺寸（中档 15% = 192×177 逻辑像素，物理 288×266）、同素材、待机循环采样 60s、Windows 11 本机（逻辑 1280×800 / 物理 1920×1200，150%）。

| 指标 | deskpet-tauri | deskpet-mini | 说明 |
| --- | --- | --- | --- |
| 仅程序本体 | **22.8MB**（单 exe） | 19.6MB（`pet.py` + 4 段帧） | 接近 |
| 含运行时依赖 | 935.9MB（exe + WebView2 Runtime 913.1MB） | 244.9MB（本体 + PyQt6 225.3MB） | WebView2 是系统级预装，通常不算应用体积 |
| 含完整 Python venv | — | 569.6MB | venv 含 pip/setuptools 等非必需包 |
| 待机内存（含子进程） | 560.9MB 均值 / 573.2MB 峰值（8 进程） | **62.4MB 均值 / 65.2MB 峰值**（1 进程） | 相差约 9 倍 |
| ├ 主进程 | 32.4MB | 62.4MB（全部） | Tauri 壳本身比 Qt 进程小 |
| ├ WebView2 browser | 172.7MB | — | Chromium 浏览器进程基线 |
| ├ WebView2 gpu-process | 154.9MB | — | 合成/纹理 |
| ├ WebView2 renderer | 92.8MB | — | 页面 + 96 帧位图 |
| ├ WebView2 utility（网络/存储等） | 91.6MB | — | |
| ├ WebView2 crashpad-handler | 16.6MB | — | |
| 待机 CPU（单核） | **8.9%** | 18.9% | mini 每帧重新解码 + 平滑缩放 |
| 首帧（进程内自计时） | 1023ms（同机多轮 0.67~1.99s 波动） | **156ms** | Tauri = WebView2 初始化 + 首帧解码 |
| 首帧（spawn → 窗口可见） | 1087ms | 400ms | 外部 `EnumWindows` 轮询 |
| 稳定性 | 60s 存活、无白底/黑屏/崩溃 | 同左 | 两端截图见下 |

![体积与内存对比](docs/screenshots/bench-compare.png)

差异来源：

- **体积**：Tauri 单 exe 几乎把"程序本体"压到和 mini 持平（22.8 vs 19.6MB），差距来自 Rust 静态链接 + WebView2 内核不在包内；只有把 WebView2 Runtime 也算进去时才会冲到 936MB，而这部分在 Win11 上是系统预装的。
- **内存**：差距**不是**页面泄漏，而是 WebView2 的多进程基线——browser/gpu/renderer/utility/crashpad 五个进程即使只画一个 192px 的宠物也要 ~500MB；Qt 单进程 62MB。Tauri 主进程本身只有 32MB，比 mini 还小。
- **CPU**：Tauri 反而更低，因为帧位图整段常驻、绘制只是 canvas blit；mini 每帧 `QImage` 解码 + `SmoothTransformation` 缩放，这是它 18.9% 的主要来源。
- **首帧**：Tauri 慢约 0.9s，来自 WebView2 运行时初始化（本机冷启动 670~1030ms 波动）；mini 直接读一帧就能显示。

## 放桌面上的效果

窗口裁剪（透明、无白底、无边框）：

![透明效果](docs/screenshots/bench-tauri-idle.png)

整屏（右下角）：

![桌面](docs/screenshots/tauri-desktop.png)

原生右键菜单：

![右键菜单](docs/screenshots/tauri-menu.png)

## 踩过的坑

1. **cargo 增量构建 `拒绝访问 (os error 5)`**：仓库目录带 `Everyone Deny DeleteSubdirectoriesAndFiles`，cargo 覆盖 `.fingerprint` 失败。解法是 `--target-dir %TEMP%\deskpet-tauri-target`（本轮的实测也是这样构建的）。
2. **透明窗口白闪**：`tauri.conf.json` 里 `visible: false`，前端解出首帧、画到 canvas 之后才调 `show_window`；同时 `shadow: false`、`decorations: false`。本机实测**没有**出现 1px 白边，也没有白闪。
3. **透明区域仍然吃鼠标**：第一轮没做穿透时，矩形窗口会截获透明像素上的点击。穿透的做法与实测见上一节；顺带确认了一件事：`WindowFromPoint` 在 `WS_EX_TRANSPARENT` 的窗口上会**跳过**它，所以"点透明区"能不能落到下层，用这个 API 就能判定，不需要真的派发事件。
4. **WebView2 GPU 进程内存暴涨（791MB）**：一开始待机每循环 96 帧都 `createImageBitmap` + `close()`，Chromium 的纹理回收有延迟，GPU 进程从 553MB 涨到 873MB。改成**整段预载并保留**后 GPU 稳定在 ~155MB，renderer ~93MB。
5. **动作播完回待机卡顿数秒**：切段时把待机位图也释放了，回待机要重解 96 帧。改为切段只释放上一段动作、待机段常驻（`dropOtherClips([IDLE_ID, id])`），回待机是瞬时的。
6. **HTML 右键菜单被窗口裁切**：192px 宽的无边框窗口放不下菜单，改用 Tauri 原生 `Menu` + `popup_menu`（菜单可以溢出窗口，靠近屏幕底部时会自动向上翻）。
7. **真实鼠标注入时好时坏**：早先 `SetCursorPos` / `SendInput` 全部失效，只能退回 CDP 派发事件；穿透这一轮里注入恢复过一次（四项点击/拖动检查全 PASS，见上一节表格），之后再测又全部失效——判定是**会话被锁屏**（截屏只能拿到锁屏页、`SetCursorPos` 返回 0）。所以点击类证据只采到了那一次成功运行的日志，进程内行为另外用 `SendMessage(WM_LBUTTONDBLCLK)` 验证过。
8. **NSIS 安装包没打出来**：`npm run build` 需要从 GitHub 下载 NSIS 3.13 + `nsis_tauri_utils.dll`，受限网络下两次都 `timeout: global`；手工把 NSIS 与 dll 放进 `%LOCALAPPDATA%\tauri\NSIS` 后仍被 tauri-cli 判为"目录缺文件"并重下。所以本轮的安装包体积为 N/A——单文件 exe 拷走即可运行，不依赖安装包。

## 已知限制

- 无托盘、无单实例、无开机自启（与 mini 保持一致，连开两次会有两只）。
- 穿透靠 50ms 轮询，指针快速越过轮廓时最坏漏 50ms（见「空白处穿透」）；未做多角色 / AI / 自动更新（本轮范围外）。
- 待机内存受 WebView2 多进程基线影响，短期不会低于 ~450MB。
- 只在 Windows 11 本机验证；macOS / Linux 未测。
- 首帧 ~1s（本机观测 0.67~1.99s，波动主要来自 WebView2 运行时初始化），比 mini 慢；如需更快可见，可把首帧改成内联 data URL 或先显示静态占位图（本轮未做）。

## 目录结构

```
deskpet-tauri/
├─ build.bat                   一键：同步素材 → cargo build（失败可见、不吞错）
├─ run.bat                     启动（含 WebView2 用户目录兜底）
├─ frontend/                   前端源码（零构建，ES Module）
│  ├─ index.html               两层 canvas 的透明骨架
│  └─ src/main.js              播放器 / 缓存 / 拖动 / 双击 / 菜单 / 快捷键
├─ frontend-dist/              构建输入（不入库）：prepare_assets.py 产出
├─ src-tauri/                  Rust crate（Cargo.toml 在这里，不在项目根）
│  ├─ build.bat                仅 cargo build（老脚本，保留给「已在 crate 目录里」的场景）
│  ├─ src/main.rs              show_window / set_tier / show_menu / quit_app
│  ├─ tauri.conf.json          无边框/透明/置顶/跳任务栏/visible:false
│  └─ icons/                   程序图标
├─ build/
│  ├─ prepare_assets.py        从 mini 同步帧 + 生成 clips.json
│  ├─ bench.py                 体积/内存/CPU/首帧同口径测量
│  ├─ bench-chart.ps1          对比图（System.Drawing）
│  ├─ extract.py               源视频 → 无损透明帧（换素材时才用）
│  ├─ make_icon.py             生成图标
│  └─ probe/                   抠像试色卡
├─ tools/                      旧流程脚本（从 mp4 重建帧 / 早期内存测量，已被 bench.py 取代）
└─ docs/screenshots/           实测截图与对比图
```

## 许可证

代码沿用仓库根目录的 [Apache License 2.0](../LICENSE)；`material/` 与帧序列为 AI 生成素材，不在代码许可范围内。
