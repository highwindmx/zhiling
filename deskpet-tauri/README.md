# 智灵桌宠 · Tauri 版

透明置顶的桌面桌宠：Rust（Tauri 2）+ 手写静态前端，**不使用 npm / vite**，`frontend/` 里的
HTML/CSS/JS 直接作为 `frontendDist` 打包进 exe（几十 KB）；动画素材**运行期外置**，不塞进 exe。

行为语义以 Python 版 `deskpet-python`（`pet.py` + `state_map.py`）为准，素材格式为 v2
（`themes/` + `animations/` + `active.json`）。

## 目录

```
deskpet-tauri/
├─ frontend/                 # 手写前端（无构建步骤，就是 frontendDist）
│  ├─ index.html             # 主窗口：两层 canvas + 提示条
│  ├─ settings.html          # 设置窗
│  ├─ workshop.html          # 素材工坊窗
│  ├─ base.css               # 设置/工坊窗共用的基础样式
│  └─ src/{main.js, settings.js, workshop.js, shared.js}
├─ src-tauri/
│  ├─ src/main.rs            # 窗口/托盘/设置窗/工坊窗/主题读写/外部状态入口
│  ├─ src/dsh.rs             # DSH 广播订阅（只读，断线自动重连）
│  ├─ src/workshop.rs        # 素材工坊（抠像/裁边/导出）后端
│  ├─ tauri.conf.json        # frontendDist=../frontend
│  ├─ capabilities/default.json   # 窗口 main + settings + workshop
│  └─ icons/                 # 全尺寸图标（16–512 PNG + icon.ico + Square*Logo/StoreLogo）
├─ build/prepare_assets.py   # 准备运行期素材（纯标准库；不碰前端）
└─ assets/                   # 运行期素材（--assets 产出，已忽略）
```

## v2 素材布局（运行期外置）

exe 读取素材（packs）的优先级（第一个存在的胜出）：

1. 环境变量 `DESKPET_PACKS`（绝对路径）
2. 设置窗「状态与动画」页里的 `packsDir`（存在 `%LOCALAPPDATA%\ZhiLing\config.json`）
3. `%LOCALAPPDATA%\ZhiLing\packs`（默认位置，与 exe 放哪无关）
4. exe 同级的 `assets/`（旧发布形态，兼容）

```
packs/
├─ themes/maid-basic.json      # 一个主题 = 13 个状态 × 动作池
├─ themes/maid-dsh.json
├─ animations/<assetId>/       # 一段动画一个目录
│  ├─ meta.json                # id/name/fps/count/canvas/format/loop/priority/type
│  └─ f0000.webp … fNNNN.webp  # 帧（webp 为主，也兼容 png/jpg）
└─ active.json                 # {"theme": "<id>", "pack": "<id>"} 当前主题镜像
```

主题文件要点（`themes/<id>.json`）：

- `states.<state>.clipIds`：**顺序轮播池**。1 段时按 `loop` 决定是否循环；多段时每段播完推进游标。
- `states.<state>.fallbackClipIds`：池内素材缺失时的兜底。
- 每状态还可带 `enabled` / `priority` / `afterSec` / `minPlayMs` / `cooldownSec` /
  `loop` / `interruptible` / `repeat`（`repeat` 仅对 `awaiting_user`、`needs_decision` 生效）。
- 状态集合固定 13 个：`idle`、`long_idle_1`、`long_idle_2`、`awaiting_user`、`busy`、`streaming`、
  `done`、`needs_decision`、`error`、`click`、`double_click`、`focus`、`focus_break`
  （没有绑定的状态合法，行为是保持当前画面；`focus` / `focus_break` 未绑定时自动回落 `busy` / `idle`）。
- 「使用中」主题由各主题文件里的 `active` 标记裁定（mtime 兜底保证唯一），`active.json` 只是镜像。
- 素材里已不再有旧的 `clips.json` / `manifest.json` / 状态映射文件（旧格式已在 Python 侧删除）；
  画布尺寸、帧数、fps、格式全部来自每段动画自己的 `meta.json`。

## 术语

- **素材包**：`packs` 根目录那一整包（`config.json` + `themes/` + `animations/` + 素材），可以包含多个主题。
- **主题**：一套「状态 → 动画」映射（`themes/<id>.json` + `animations/<id>/`）。界面与提示一律称「主题」，不再用「系列」。

## 获取与安装素材包

素材**不进代码仓库**，而是单独打包上传分发（例如附在 release 的 zip 里）：仓库里只有程序、
前端和构建脚本，clone 下来直接跑是看不到动画的。

安装步骤：

1. 下载素材压缩包（zip）。
2. 解压到 **`%LOCALAPPDATA%\ZhiLing\packs`**（在资源管理器地址栏粘贴
   `%LOCALAPPDATA%\ZhiLing\packs` 回车即可到达；目录不存在就先新建 `ZhiLing\packs`）。
   首次运行若检测到旧 `%LOCALAPPDATA%\DeskPet` 有内容，会自动迁移到 `ZhiLing`（见根 README「共用规范」）。
3. 解压完直接双击 `ZhiLing.exe`，exe 放在哪个目录都可以。

> **不要解出 `packs\packs\...` 多套一层。** 压缩包里应该直接是 `themes\`、`animations\`、
> `active.json`；解压后 `%LOCALAPPDATA%\ZhiLing\packs\themes\*.json` 必须能直接看到。
> 若多套了一层，等于素材根目录里没有 `themes\`，启动会弹「启动失败」框。

素材也可以放在别处：设置窗「状态与动画」页的「素材目录」里浏览选择一个目录（写进
`config.json` 的 `packsDir`），或用环境变量 `DESKPET_PACKS` 指定；两者都优先于默认位置。
为兼容旧发布形态，exe 同级的 `assets\` 仍会被识别（解析顺序见上一节）。

解压后的目录结构（`%LOCALAPPDATA%\ZhiLing\packs`）：

```
packs/
├─ themes/                     # 【必需】至少一个 *.json（否则自检拦下）
│  ├─ maid-basic.json          # 一个主题 = 13 个状态 × 动作池
│  └─ maid-dsh.json
├─ animations/                 # 【必需】目录本身必须存在（可先为空）
│  └─ <assetId>/               # 主题里 clipIds 引用的每一段
│     ├─ meta.json             # 【必需】id/name/fps/count/canvas/format…
│     └─ f0000.webp … fNNNN.webp   # 【必需】帧，数量要与 meta.json 的 count 一致
└─ active.json                 # 【可选】当前主题镜像 {"theme": "...", "pack": "..."}
                               # 缺失时按 themes/*.json 里的 active 标记裁定，切主题会重建
```

要点：

- `themes/*.json` 与 `animations/<assetId>/` 的对应关系是**按 id 引用**：主题里写了
  `clipIds: ["dsm_idle"]`，就必须有 `animations/dsm_idle/meta.json` + 帧；缺素材的那段会被跳过，
  池为空的状态保持当前画面（`focus` / `focus_break` 未绑定则回落 `busy` / `idle`）。
- 帧格式以 webp 为主，也兼容 png/jpg；画布尺寸、帧数、fps 全部来自 `meta.json`，不写死在程序里。
- 设置面板里改过的主题会写回 `packs/themes/<id>.json`，自动备份到 `packs/.backup/`。

### 验证装好了没

1. 双击 `ZhiLing.exe`：能看到桌宠窗口（首次约 0.9s），待机动画在动；
2. 托盘/右键 →「设置」，在「状态与动画」页看到主题下拉里有素材包里的主题，左侧 13 个状态
   带首帧缩略图与绑定数量；
3. 用 `DESKPET_LOG=1` 临时打开日志，之后 `%LOCALAPPDATA%\ZhiLing\deskpet.log` 里应出现
   `packs_root=...\ZhiLing\packs source=local exists=true themes=true animations=true`，
   随后有 `boot theme=... assets=... idle=...` 与 `clip_start id=... loop=...`（默认关闭日志时不会写这些）。

### 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 弹「智灵桌宠 · 启动失败」，日志 `fatal`，退出码 1 | 素材根目录里没有 `themes\` / `animations\` / 主题 JSON | 多为解压层级错（`packs\packs\themes`），把内层内容移到 `packs\` 根下 |
| 有桌宠但不动 / 待机空白 | `idle` 池为空：主题引用的动画目录缺失或 `meta.json` 的 id 对不上 | 核对 `animations/<assetId>/`，日志里看 `clip_start` 是否出现 |
| 设置窗看不到主题 | `packsDir` 指向了错误目录（如指向了 `packs\themes`） | 设置 →「状态与动画」页把「素材目录」改成 `...\ZhiLing\packs` 再保存 |
| 改了设置但重启又变回去 | `%LOCALAPPDATA%\ZhiLing\config.json` 不可写或被独占 | 检查文件权限；程序用「临时文件 + 原子替换」写回，权限不足会在日志里报错 |
| 换了素材但没生效 | 素材根被缓存到进程内 | 重启 exe；改 `packsDir` 保存后会自动重载 |

## 准备素材

```powershell
python build/prepare_assets.py              # 只准备素材（等同 --assets）
python build/prepare_assets.py --packs      # 素材生成到 %LOCALAPPDATA%\ZhiLing\packs（默认位置）
python build/prepare_assets.py --packs D:\MyPacks   # 生成到指定 packs 根（直接含 themes/animations/active.json）
python build/prepare_assets.py --assets     # 只准备素材到仓库内 assets/（旧布局/调试用）
python build/prepare_assets.py --assets --out <exe 所在目录>  # 素材生成到 <该目录>/assets
```

- `--packs` 不带值时写 `%LOCALAPPDATA%\ZhiLing\packs`；带值时该参数就是**素材根本身**
  （脚本直接在其中生成 `themes/`、`animations/`、`active.json`，不会再套一层）。
- `--out` 传的是**父目录**，脚本在其中生成 `assets/`（只重建这个 `assets/` 子目录，绝不清空父目录）。
- 源目录默认 `../deskpet-python/packs/`，找不到则回退仓库根的 `packs/`。
- `--assets` / `--packs` 会**整体重建**目标目录（设置面板里改过的主题会被覆盖，备份在 `.backup/`）；
  为防误删，目标是盘符根目录或目录里含 `*.exe` 时脚本会直接拒绝。
- 换主题/换素材不需要重新编译，重启 exe 即可（或保存一次 `packsDir` 触发重载）。

## 运行期配置与日志

- 配置与 Python 版**共用同一个文件**：`%LOCALAPPDATA%\ZhiLing\config.json`（与素材目录无关，
  换素材不丢设置）。键名与 Python 侧一致：

  | 键 | 类型 | 默认 | 说明 |
  | --- | --- | --- | --- |
  | `tier` | string | `"中"` | 尺寸档位：`小` / `中` / `大` |
  | `remindHold` | bool | `true` | 完成 / 出错 / 待决策时持续播放，直到点击桌宠 |
  | `packsDir` | string | `""` | 素材根目录；空串 = 按解析顺序取默认位置 |
  | `pomodoro.enabled` | bool | `true` | 番茄钟总开关（关掉会停掉正在跑的计时） |
  | `pomodoro.workMin` | number | `25` | 专注时长（1~180 分钟） |
  | `pomodoro.breakMin` | number | `5` | 休息时长（1~60 分钟） |

- 写入方式是「读-改-写」：先读现有文件 → 深度合并本次修改 → 写 `config.json.tmp` 并 `sync`
  → 原子重命名替换。文件里 Python 版写入的其它键（含嵌套对象里的未知键）原样保留，不会被清掉。
- 旧版 `deskpet-python\config.json` 的迁移由 Python 侧负责；Tauri 版只读共用文件，不做迁移。
  旧用户目录 `%LOCALAPPDATA%\DeskPet` 由本版启动时一次性迁移到 `ZhiLing`（失败则继续用旧目录）。
- **日志默认关闭**（生产不写常规日志），只保留致命错误（`fatal` / `panic` / 启动失败）与一次性迁移说明。
  排障时用统一开关临时打开：

  | `DESKPET_LOG` | 行为 |
  | --- | --- |
  | 未设置 / `0` / `off` / `false` / `no` | 关闭（默认） |
  | `1` / `on` / `true` / `yes` | 打开，写 `%LOCALAPPDATA%\ZhiLing\deskpet.log` |
  | 其它非空值 | 当作日志文件路径并打开（保留原有 `DESKPET_LOG=<路径>` 语义） |

  打开后按大小轮转：单文件上限 **2 MB**，归档固定为 `deskpet.log.1`、只保留 1 份，
  不按时间戳累积；轮转失败只告警一次，不影响启动。打开时才记录启动 pid/版本、`config_path`、
  素材根与来源、主题、状态切换、`clip_start`、菜单、外部输入等常规事件。
- **启动失败不再静默**：素材根缺失（无 `assets/`、`themes/*.json` 或 `animations/`）时弹原生
  错误框「智灵桌宠 · 启动失败」、日志写 `fatal ...`、退出码 `1`；WebView2 运行时缺失或
  Tauri 启动失败同样弹框 + 写日志。`/state` 端口被占用只记一行 `state_http bind ... 失败`，不退出。
- 素材通过自定义协议 `deskpet://localhost/`（Windows 映射为 `http://deskpet.localhost/`）读取，
  只放行 `json/webp/png/jpg/jpeg/svg/mp4` 后缀并禁止 `..`。

## 外部状态入口

两种来源都会以 `state-in` 事件投递到前端状态机。

1. **HTTP**：默认 `http://127.0.0.1:8765/state`，**只绑 127.0.0.1**。

   ```powershell
   # 查询
   curl http://127.0.0.1:8765/state
   # {"ok":true,"playing":"dsm_idle","state":"idle"}

   # 投递状态（三种 body 都接受）
   curl -X POST http://127.0.0.1:8765/state -H "Content-Type: application/json" -d '{"state":"done","message":"编译完成"}'
   curl -X POST http://127.0.0.1:8765/state -d 'done'
   ```

   `POST` body 为 JSON 对象时支持 `state` / `message` / `priority` / `ttlSec` 等字段；
   也接受纯字符串或整段文本。`DESKPET_STATE_PORT=0` 关闭服务，改端口同理。

2. **状态文件**：`DESKPET_STATE_FILE=<路径>` 时每 500ms 轮询，**内容变化**（mtime+size）才投递；
   启动时首读只作基线，不会补播历史状态。

## 交互

- 左键拖动移动窗口（`startDragging`）；短按（<400ms、位移 <4px）触发 `click` 状态。
- 双击触发 `double_click`：该状态有绑定就按池轮播；零绑定且当前是 `idle` 时回退到
  「按优先级轮播一段动作」（v2 里 11 条规则恒存在，不做回退的话双击会像没反应）。
- 右键：原生菜单（HTML 菜单会被无边框窗口裁切），只有 `尺寸 ▸` 与 `隐藏`；打开菜单算一次
  用户输入，会清掉提醒态常驻。
- 空白穿透：前端按 alpha 掩码 50ms 轮询光标位置，命中不透明像素才接收事件（`set_ignore_cursor_events`）；
  hover 命中时页面把光标设为手型。
- `Esc` 退出。
- 完成 / 出错 / 待决策（`HOLD_STATES`）在「提醒状态常驻」开启时一直播对应状态动画，直到用户
  点击桌宠或从托盘/右键操作；关闭时按 `ttl`/`afterSec` 回落到 `idle`。

## 托盘

- 菜单：显示/隐藏、`尺寸 ▸`（小/中/大三档，屏幕宽度 10% / 15% / 30%）、`主题 ▸`（切换即写
  `active` 标记 + `active.json` 镜像并热重载）、`番茄钟 ▸`、动作工坊、设置、退出。
- `番茄钟 ▸`：第一行是状态（如「专注中 · 剩余 24:59」），下面是「开始专注 / 暂停（暂停后变
  「继续」）/ 停止」，到点后第一行变成「专注结束 · 点击开始休息」，点桌宠或再点开始进入下一阶段。
  总开关与时长在设置窗「提醒」页。
- 双击托盘图标切换显示/隐藏。
- 隐藏后窗口从任务栏/桌面消失但进程仍在；从托盘恢复会暂停→恢复播放。

## 番茄钟

- 默认 25 分钟专注 + 5 分钟休息，时长与开关写在 `config.json` 的 `pomodoro.{enabled,workMin,breakMin}`。
- 计时用单调时钟（`Instant` deadline + 剩余量），系统时间被改不会漂移；500ms 刷新一次剩余时间。
- 阶段切换推外部状态：专注 `focus`、休息 `focus_break`（与 Python 侧键名一致）。
  主题里没绑这两个状态的动画时自动回落 `busy` / `idle`，日志有 `外部状态 focus 未绑定 → 回落 busy`。
- 阶段结束推 `done` 提醒（常驻，直到点击桌宠），点击后进入下一阶段（专注 ↔ 休息）。

## 设置窗

`frontend/settings.html` + `src/settings.js`，深色、顶栏只有**两个页签**：

- 顶部：标题 + 页签 **状态与动画 / 提醒与连接**；右侧保留「尺寸」下拉与「提醒状态常驻」勾选
  （全局开关，不随页签切换）。「恢复上次配置 / 保存配置 / 取消」固定在底部。
- **状态与动画**页：最上方是「主题」下拉 + 「重命名主题…」+「新建主题…」（先选主题，再看/改该主题的绑定）；
  下面是 13 个状态列表（可拖拽或 ↑↓ 调整顺序，顺序即优先级，保存时按 `(n-1-i)*10` 重算 `priority`），
  带首帧缩略图与绑定数量；右栏是选中状态的动作池（每段可预览、上移、下移、移除；「从动作库添加」
  弹窗，「新建动画」打开本版内置工坊）。底部「素材与工具」只有**素材目录**（只读路径 + 浏览/恢复默认，
  写入 `config.json:packsDir`）。
- **提醒与连接**页：番茄钟总开关、专注 / 休息时长、当前计时；DSH 链路状态灯、地址、最近事件、
  订阅端口（默认 55471，0 = 关闭）与「重新检测」；对外 HTTP 入口地址 / 端口与最近状态。
- 底部保存：写 `packs/themes/<id>.json`（保留未知字段、强制 `version: 2`、备份到 `packs/.backup/`），
  同时把 `tier` / `remindHold` / `packsDir` / `pomodoro` / `dshPort` 合并进共用 `config.json`。

## 构建

前置：Rust 工具链、Python 3（仅用于 `prepare_assets.py`；运行时不需要）。

```powershell
cd src-tauri
cargo build --release --target-dir "$env:TEMP\deskpet-target"
# 或正式打包安装器：cargo tauri build --target-dir "$env:TEMP\deskpet-target"
```

### 图标

`src-tauri/icons/` 由 `build/make_icons.py` 生成（与 Python 版 `make_icon.py` 同一套参数：`#6D4BD6`
圆角矩形 + 黑体白色「灵」字），每个尺寸独立渲染（小尺寸按倍数超采样后缩回，不放大糊图），
颜色/像素与 `deskpet-python/assets/icon.ico` 的对应尺寸逐像素一致（16–256 全档已比对）。

```powershell
uv run --project ..\deskpet-python python build\make_icons.py
```

产物：`16/20/24/32/48/64/128/256/512.png`、`128x128@2x.png`(256)、`icon.png`(512)、
`Square*Logo.png`、`StoreLogo.png`、`icon.ico`（内嵌 16/20/24/32/48/64/128/256 共 8 档，
托盘与资源管理器共用）。`tauri.conf.json` 的 `bundle.icon` 已列全这些文件。

### 本仓库的 ACL 坑（重要）

`src-tauri` 及其子目录带 `Everyone: Deny DeleteSubdirectoriesAndFiles`，`tauri-build` 在
`gen/schemas` / 权限文件生成时会报 `拒绝访问 (os error 5)`；**仅把 `--target-dir` 指到临时目录
并不够**。已验证可行的绕行办法：把编译必需文件复制到仓库外再构建。

需要复制的最小集合：

```
src/  capabilities/  icons/  Cargo.toml  Cargo.lock  build.rs  tauri.conf.json
gen/schemas/          # 若存在
frontend/             # 前端源（直接是 frontendDist，跟随复制即可）
```

```powershell
$dst = "$env:TEMP\deskpet-build"
robocopy . $dst /MIR /XD target assets .backup
cd $dst\src-tauri
cargo build --release --target-dir "$env:TEMP\deskpet-build-target"
```

### 构建完怎样双击就能跑

exe **不内嵌素材**（只内嵌前端），但素材也不再要求放在 exe 同级：装到默认位置
`%LOCALAPPDATA%\ZhiLing\packs` 后，exe 放哪个目录都能跑。

```powershell
# 1. 构建（用临时 target 避开 ACL；产物在 $env:TEMP\deskpet-build-target\release\ZhiLing.exe）
cargo build --release --manifest-path "$env:TEMP\deskpet-build\src-tauri\Cargo.toml" `
  --target-dir "$env:TEMP\deskpet-build-target"
# 2. 素材装到默认位置（不带值时就是 %LOCALAPPDATA%\ZhiLing\packs）
python build\prepare_assets.py --packs
# 3. 把 exe 拷到任意目录（这里以 D:\ZhiLing 为例），双击即可
New-Item -ItemType Directory -Force D:\ZhiLing | Out-Null
Copy-Item "$env:TEMP\deskpet-build-target\release\ZhiLing.exe" D:\ZhiLing\
```

运行所需的只有两处：

```
%LOCALAPPDATA%\ZhiLing\packs\    # 素材（必需）
├─ themes\*.json
├─ animations\<assetId>\meta.json + fNNNN.webp
└─ active.json                   # 可选，缺失会自动重建
D:\ZhiLing\ZhiLing.exe      # 程序，放哪都行
```

起来后会先看到桌宠窗口（约 0.9s），托盘图标在 Win11 默认收进「隐藏的图标」溢出区，
点任务栏托盘的小箭头才看得到（可拖出来固定）。素材缺失时会被自检拦下：弹
「智灵桌宠 · 启动失败」框、退出码 1、日志记 `fatal`（先查 `packs_root=... source=...` 那行）。
如果确实想沿用旧形态，也可以把素材生成到 exe 同级：`python build\prepare_assets.py --assets --out D:\ZhiLing`
（解析顺序里 exe 同级 `assets\` 排最后兜底）。

### 改了前端却像没生效

`frontend/` 直接就是编译期嵌入的 `frontendDist`：改完 `frontend/` 直接用 `cargo build` 重编即可，
**不需要再跑任何「暂存前端」步骤**（早期版本有 `frontend-dist/` 中间副本，现已去掉）。但要注意——
cargo 并不总能感知 `frontend/` 目录变化，所以有时要强制本 crate 重编，确保输出里出现
`Compiling deskpet-tauri`：

```powershell
# 任选其一，确保输出里出现 Compiling deskpet-tauri
(Get-Item src-tauri\build.rs).LastWriteTime = Get-Date
# 或 cargo clean -p deskpet-tauri --target-dir ...
```

否则 exe 里跑的还是旧 JS（典型症状：页面白屏、控制台报旧版本的 `SyntaxError`、`__deskpetPick` 未定义）。

### 换了图标却像没生效

`icons/icon.ico` 由 `tauri-build` 写进 `resource.rc`（`32512 ICON "<绝对路径>"`）再编译成 `resource.lib`
链接进 exe。但构建脚本的 `rerun-if-changed` 只有 `tauri.conf.json`、`capabilities/`、`sample-pack/`
（见 `target\release\.fingerprint\deskpet-tauri-*\run-build-script-build-script-build.json`），
**`icons/` 不在其中**：只改图标、不动其它文件时 `cargo build` 会复用旧的 `resource.lib`。

```powershell
# 换图标后强制本 crate 的构建脚本重跑（任选其一）
(Get-Item src-tauri\build.rs).LastWriteTime = Get-Date
Remove-Item src-tauri\target\release\build\deskpet-tauri-*\out\resource.lib -Force
```

另外注意展示层：主窗口是 `skipTaskbar: true` + `decorations: false`，任务栏和标题栏都不显示图标，
**唯一可见的图标是托盘图标**，取自编译期嵌入的 `icons/32x32.png`（`main.rs` 的 `default_window_icon()`），
进程启动时注册——旧实例不退出就不会换图。资源管理器里文件图标仍显示旧的，则是 shell 图标缓存：
`ie4uinit.exe -show`，必要时重启 explorer。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `DESKPET_PACKS` | 素材根目录绝对路径（优先于设置里的 `packsDir` 与默认位置） |
| `DESKPET_STATE_PORT` | `/state` HTTP 端口，默认 `8765`，`0` 关闭 |
| `DESKPET_STATE_FILE` | 外部状态文件路径，500ms 轮询（内容变化才投递） |
| `DESKPET_WORKSHOP` | 已废弃：Tauri 版内置素材工坊（src/workshop.rs + workshop.html），不再外拉 `workshop.bat` |
| `DESKPET_LOG` | 日志开关：未设置/`0`/`off` = 关闭（默认）；`1`/`on`/`true` = 打开并写 `%LOCALAPPDATA%\ZhiLing\deskpet.log`；其它值 = 日志文件路径 |

## 与 Python 版的差异

- **DSH 桥接已移植**：`src/dsh.rs` 是只读 WebSocket 订阅（与 `dsh-deskpet-bridge` 同一份广播），
  断线指数退避自动重连；设置窗的「DSH 链路」状态灯就是它的前端。改 `dshPort` 保存后已会自动重连。
- **素材工坊已内置**：`src/workshop.rs` + `workshop.html` 是原生窗口，抠像/裁边/导出全部在 Rust 侧完成，
  不再外拉 Python 版 `workshop.bat`。托盘/设置里的「素材工坊」直接开本窗。
- 双击回退只在 `idle` 且 `double_click` 零绑定时生效（Python 旧逻辑是恒真判断，v2 下会静默）。
- 其余状态机语义（池游标轮播、多段逐段推进、单段 loop、冷却/占用回滚、TTL、两级 long idle、
  `done → awaiting_user`、提醒态常驻、`force` 清除、按池内是否在播补齐）与 `state_map.py` 对齐。

## 排错

- 启动黑屏/无动画：先用 `DESKPET_LOG=1` 打开日志，再看 `%LOCALAPPDATA%\ZhiLing\deskpet.log`（或 `DESKPET_LOG` 指定的文件）里的
  `packs_root=... source=... exists=... themes=... animations=...`，再确认是否出现
  `clip_start id=... loop=...` 与 `boot theme=... assets=... idle=...`；没有 `idle`
  说明待机池为空。出现 `fatal 找不到素材目录` 说明素材根解析错了（见「获取与安装素材包」）。
- 设置窗打开白屏：确认 `frontend/settings.html` 存在（直接是 frontendDist 源），且
  `capabilities/default.json` 的 `windows` 含 `settings`。
- 构建报 `LNK1104`：上一次运行的应用进程仍占用 exe，结束进程或换新的 `--target-dir`。
