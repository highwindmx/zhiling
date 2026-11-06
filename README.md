# zhiling · 桌面桌宠（DSMaid）

一只住在桌面右下角的绿幕抠像女仆：透明窗口、不抢焦点、按对话状态播动画。当前主线是 Python/Qt 版。

> 测试/验证截图不入库、不上传 GitHub。要留证据就放本地 `build\screenshots\`（已 ignore）。

## 30 秒上手

```bash
cd deskpet-python
uv sync
uv run python pet.py --width 300
```

没有 uv 时用 pip（仅当没有 uv）：`.venv\Scripts\pip.exe install -r requirements.txt`，再 `run.bat`。

## 目录结构

- `deskpet-python\` —— 主线：PyQt6 透明窗口、状态映射、素材工坊
- `deskpet-python\packs\` —— 素材包（v2：`themes/<id>.json` 与 `animations/<id>/meta.json` 进 git，帧外置）
- `material\` —— 原始绿幕 mp4（即梦/海螺生成，不入库）
- `docs\` —— 素材包规范、状态映射设计（**只在本地维护，不入库、不推 GitHub**）
- `deskpet-tauri\` —— Tauri 版（单 exe + 外部 `assets\`）

## 素材

帧（`packs\animations\<id>\` 里的帧）和 mp4 都**不进 git**：按 `manifest.json` 记录的参数从 `material\` 重建，或从 Release 附件取。素材目录不再写死：`DESKPET_PACKS` → 设置里的「素材目录」（`config.json: packsDir`）→ `%LOCALAPPDATA%\DeskPet\packs` → 就地（`deskpet-python\packs`、仓库根 `packs`），换素材不需要改代码、不需要重编译。

配置统一到 `%LOCALAPPDATA%\DeskPet\config.json`，Python 版与 Tauri 版**共用同一份**（`DESKPET_CONFIG` 可整体覆盖路径）。旧 `deskpet-python\config.json` 首次运行会合并进新文件：新文件已有键优先、旧文件只补缺失键，迁移后旧文件改名为 `config.json.migrated`。写入一律「读-改-写 + 临时文件原子替换」，不认识的键（例如 Tauri 后续新增的）原样保留。

## 抽帧依赖（ffmpeg）

素材工坊抽帧 / 生成动画用 ffmpeg，但**不需要配置**：

- Python 版直接用 `imageio-ffmpeg` 随 `uv sync` 一起装好的二进制；找不到时回落到 `PATH` 里的 `ffmpeg`。
- 两者都没有时，工坊会弹窗说明“抽帧不可用、仍可浏览已有素材”，并提示执行 `uv sync` 重装依赖，或把 ffmpeg 安装到 `PATH`；点「生成」也会明确报错，不会静默失败。
- 共享 `config.json` 里如果已有 `ffmpegPath` 或环境变量 `DESKPET_FFMPEG`，Python 版**不读、不写、不删除**，原样保留。

## 获取与安装素材包

`packs\` 不随代码入库，单独打包为 zip 随 Release 附件分发（或从素材盘拷贝）。只克隆代码直接启动是没有完整素材的，但**随包带了一份最小示例**，首启不会完全空白。

### 首次启动：随包最小示例

仓库内置 `deskpet-python\sample_packs\sample\`（约 3.7 MB，只含 idle 一段，packId=`sample`）。首次启动如果按解析顺序找不到任何可用素材，会自动把它复制到 `%LOCALAPPDATA%\DeskPet\packs`：逐文件复制，**已存在的一律跳过，绝不覆盖用户数据**，保证至少能出一个待机画面。

- 设置 →「状态与动画」会标注“当前使用随包示例素材”，旁边有「查看安装说明」。
- 装完整素材包后，示例主题会被完整主题取代：直接覆盖同一目录，或把完整包放到别处在「状态与动画」页底部「素材与工具」→「素材目录」里指向它。示例文件不会自动删除，但解析优先用你指定的目录 / 完整主题。
- 如果连示例包都复制不了（目录只读、权限不足等），首次会弹**一次**提示框：`选择素材目录…`（选中后写入 `packsDir` 并继续启动）/ `查看安装说明`（打开本 README，失败则开仓库页面）/ `退出`。该提示框是 Qt 窗口，`pythonw` + `run.vbs` 下同样可见；提示过后写标记 `deskpet-python\build\cache\first_run.prompted`，之后不再每次启动都弹，日志会写明原因。

### 安装（推荐）

1. 下载 `deskpet-packs-<版本>.zip`。
2. 解压到 `%LOCALAPPDATA%\DeskPet\packs`（`DeskPet` 目录不存在时先建）。
3. 解压后该目录应**直接**能看到 `active.json`、`themes\`、`animations\`。
   ⚠ 不要多套一层：出现 `packs\packs\active.json`、`packs\deskpet-packs\active.json` 都算解压层级错误。
4. 启动 `run.vbs`（或 Tauri 版 exe）。

### 其它两种放置方式

- **脚本同级**：放到 `deskpet-python\packs\`，适合便携 / 绿色版，整个目录可搬走。
- **任意路径**：设置 →「状态与动画」页底部「素材与工具」→「素材目录」→「浏览…」选择 `packs` 所在目录（写入 `config.json:packsDir`）。

解析顺序按实际实现：`DESKPET_PACKS` 环境变量 → 设置里的 `packsDir` → `%LOCALAPPDATA%\DeskPet\packs` → 就地（`deskpet-python\packs`、仓库根 `packs`）。不存在的候选会跳过并记一条日志回落，不会崩。

### 目录结构

```
packs\
├─ active.json                # 可选：当前启用主题 id（缺失时自动选第一个主题）
├─ themes\
│  ├─ maid-dsh.json           # 必需（至少 1 个）：name + states{13 状态 → clipIds[]}
│  └─ <theme>.json
└─ animations\
   ├─ dsm_idle\
   │  ├─ meta.json            # 必需：fps / canvas / format / offset 等
   │  └─ 0001.webp …          # 必需：帧序列（被主题引用的段缺帧会启动失败）
   └─ <assetId>\              # 未被任何主题引用的动画可以只留 meta.json
```

### 验证

启动桌宠 → 右键「设置…」→「状态与动画」页：能看到**主题下拉**且 13 行状态有绑定，即素材加载成功；待机动画正常循环。也可看 `deskpet-python\build\cache\pet.log` 里的 `packs_root=…（来源 …）` 是否为预期目录。

### 常见问题

- **解压层级错**：`packs_root` 指向的目录里没有 `themes\` → 设置页主题下拉为空、13 行全是 0 段；桌宠会因 idle 段缺帧直接退出（`pythonw` 无控制台，表现为“双击没反应”）。
- **素材目录指错**：设置「状态与动画」页的素材目录框会标黄，tooltip 里写明“目录不存在，已回落到 …”；日志里是 `config:packsDir 指向的素材目录不存在：…`；实际使用回落后的目录。
- **缺 `active.json`**：不会报错，自动选第一个主题（有 `active: true` 的优先，其次按兜底顺序）。若 `themes\` 里一个主题都没有，桌宠启动即退出、工坊仍能打开但主题下拉为空。
- **首次启动会生成什么**：没有完整素材时，会把 `sample_packs\sample` 复制到 `%LOCALAPPDATA%\DeskPet\packs`（约 3.7 MB，仅 idle）；`config.json` 只在第一次保存设置或切换尺寸时写入（内容是本次修改的键）。示例包只保证能出画面，不包含完整动作，按上面的安装步骤换成完整 `packs` 即可。

## 待办 / 未实现

以下为计划项 / 状态存档；标的「已实现」条目已落地，未标的仍是计划项，不要按已完成引用。

- **设置分页（已实现）**：设置窗口只有两页——「状态与动画」（含主题下拉、13 行状态绑定、底部「素材与工具」）与「提醒与连接」（番茄钟、提醒常驻/方式、DSH 链路）。默认尺寸不在设置里，仍走托盘/右键菜单。
- **双击桌宠唤起程序**：双击时打开设置里配置的程序（如 DSH）；该程序已在运行时优先激活已有窗口，而不是再起一个实例。设置项需要「程序路径」+ 开关。
- **文件拖放到桌宠按后缀打开**：把文件拖到桌宠上，按后缀查设置里的映射并用指定程序打开；可执行后缀（`.exe/.bat/.cmd/.ps1` 等）默认拒绝，必须在白名单里显式放行。
- **番茄钟（已实现）/ 待办提醒（未实现）**：番茄钟已映射到独立状态键 `focus` / `focus_break`（主题未绑定时回落 `busy` / `idle`）；配置与 Tauri 同构——`pomodoro.enabled` / `pomodoro.workMin` / `pomodoro.breakMin`（分钟，默认 25 / 5）；到点提醒复用「提醒态常驻」（`done`），点击桌宠解除并接下一阶段。待办清单仍计划单独存 `todos.json`。
- **DSH 选项提醒（Python 侧已实现）**：旁听 `session/event` 里的 `ask_user_question` 工具调用，从参数取结构化的 `questions[].options`；桌宠只把状态切到提醒态（待决策）并保持常驻，**不代答**；用户点桌宠时唤起 DSH，由用户自己在原有界面上选。明确**不接管** `user-questions/request` waterfall（不抢 answerer、不影响 Web 卡片），也**不代答审批**（`approval/request` 没有事后补答通道，抢答等于绕开一致审计）。
  - 实现位置：桥 `dsh-deskpet-bridge/index.js`（`question-asked` / `question-cleared`）→ `build/tools/dsh_bridge.py`（`needs_decision` / 回落）。回放验证通过，尚未用真实 DSH 端到端验证。

## License

代码：**GNU GPL v3.0**（全文见 `LICENSE`）。

素材（`deskpet-python\packs\`、`material\` 与生成帧）**不适用 GPL**，各自独立授权（当前两个 pack 为 `all-rights-reserved`，来源见各自 `manifest.json` 的 `license`）。不要把代码协议套到素材上，也不要把不同来源的素材混进同一个单一许可的 pack。

## deskpet-tauri

单 exe（约 3MB）+ 外部素材目录（帧和视频**不在 exe 内**）。托盘、双击设置、番茄钟、素材工坊均已实现。

**构建（v2 零前端构建）**：
```
cd deskpet-tauri
python build\prepare_assets.py --packs         # 生成素材到 %LOCALAPPDATA%\DeskPet\packs（运行时外置）
cd src-tauri && cargo build --release --target-dir "$env:TEMP\deskpet-target"
```
`frontend/` 直接作为 `frontendDist` 编译期嵌进 exe（见下「改了前端却像没生效」）。`--target-dir` 指到 `%TEMP%` 是绕开仓库内 `src-tauri` 的 `Everyone:Deny` ACL 锁（`os error 5 拒绝访问`）。运行用 `deskpet-tauri.exe`（双击即可，exe 放哪都能跑），素材装到 `%LOCALAPPDATA%\DeskPet\packs`。

**素材目录解析顺序**（`packs`，含 `themes\*.json` / `animations\<id>\meta.json + fNNNN.webp` / `active.json`）：
`DESKPET_PACKS` → 设置里的「素材目录」→ `%LOCALAPPDATA%\DeskPet\packs` → exe 同级 `assets\`（仅在其它位置都不存在时兜底的旧布局兼容）。
生成素材到默认位置：`python build\prepare_assets.py --packs`。

**配置与环境变量**：默认 `%LOCALAPPDATA%\DeskPet\config.json`（与 Python 版共用同一份）。可用的覆盖变量：`DESKPET_PACKS`（素材目录，优先级最高）、`DESKPET_CONFIG`（配置文件路径）、`DESKPET_LOG`（日志路径，默认 `%LOCALAPPDATA%\DeskPet\deskpet.log`）、`DESKPET_STATE_PORT`（本机状态端口，默认 8765，`0` = 关闭）。

**免 ffmpeg**：Tauri 版用系统自带 Windows Media Foundation 解码，不随包分发 ffmpeg，也不读 `DESKPET_FFMPEG` / `ffmpegPath`。仅支持 Media Foundation 能解的容器/编码（mp4 / mov 的 H.264 / HEVC）；webm / VP9 / AV1 等会明确提示「先转成 mp4，或用 Python 版素材工坊」，不会静默产出坏帧。导出 WebP 用内置 libwebp（q80 / method 6 / alpha_quality 80，与 Python 版同口径）。

`Cargo.toml` 已启用 `tray-icon`，托盘菜单（状态/尺寸/番茄钟/工坊/设置/退出）在 `main.rs` 构建。NSIS 安装包尚未产出。

实测：首帧 1240ms、内存 549.8MB（60s 待机口径）。

### 分享 / 分发（单文件）

**结论：`target\release\deskpet-tauri.exe` 一个文件就能分发**（实测 release 目录里只有 `deskpet-tauri.exe` 与 `deskpet_tauri.pdb`，后者是调试符号，不用发；没有需要一起带的 `.dll`）。

1. **编译前先确认前端已就位**：`frontend/` 直接作为编译期嵌进 exe 的 `frontendDist`（无需再跑任何「暂存前端」步骤，早期 `frontend-dist/` 中间副本已删除）。跳过前端目录会打出旧界面。
2. **只改了前端、cargo 说 "Finished" 却没重编**：`cargo` 未必重新嵌入，`touch src-tauri\build.rs`（或 `cargo clean -p deskpet-tauri --target-dir "$env:TEMP\deskpet-target"`）再编译。判断是否真的更新：看 exe 的修改时间。
3. **素材包单独分发**：帧与视频不进 exe。把 packs 解压到目标机的 `%LOCALAPPDATA%\DeskPet\packs`（含 `themes\*.json`、`animations\<id>\meta.json + fNNNN.webp`、`active.json`）。首次启动找不到素材会弹明确提示，不会静默黑屏。
4. **目标机依赖**：只需 `Microsoft Edge WebView2 Runtime`（Win10 1803+ / Win11 多数已自带，缺失时启动会弹提示并给安装指引）。实测 exe 不导入 `VCRUNTIME140.dll` / `MSVCP140.dll` / `WebView2Loader.dll`；C 运行时走 Windows 自带的 Universal CRT（`api-ms-win-crt-*`），因此**不需要装 VC++ 运行库**。若你的构建环境改成了动态 CRT、目标机又是精简系统，可用 `RUSTFLAGS="-C target-feature=+crt-static"` 重编（取舍：exe 变大约 1~2MB，换来零运行库依赖）。
5. **没有代码签名**：别人下载后首次运行会出现 SmartScreen「Windows 已保护你的电脑」，需点「更多信息 → 仍要运行」。要消掉这个提示只能买代码签名证书，或在企业内网用白名单策略。
6. **安装包（可选）**：需要 `cargo tauri build`（先装 `tauri-cli`），产出 NSIS/MSI。当前仓库只验证了裸 exe，安装包未产出。

`[profile.release]` 已按常规压缩配好（`strip = true`、`lto = true`、`codegen-units = 1`、`panic = "abort"`、`opt-level = "s"`），当前 exe **3.79 MB**（隔离目录 release 构建口径）。

## 详细文档

`docs\` 只在本地、不进 git（历史里曾提交过，若已重写历史则一并消失）。本机阅读：

- `deskpet-python\README.md` —— 安装、运行、工坊、状态映射、排错（随仓库入库）
- `docs\material-packs.md` —— 素材包规范（画布、license、分发、工坊流程）
- `docs\action-conversation-mapping.md` —— 10 状态映射与 DSH 事件契约
