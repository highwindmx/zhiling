# zhiling

蓝鲸女仆桌宠。仓库里有两版实现：

- **[deskpet-python](deskpet-python/)** —— 正式版。Windows 桌面悬浮窗（Python + PyQt6），绿幕视频抠成透明序列后贴在桌面上循环播放，可拖动、双击播一次动作、右键切小/中/大三档尺寸。
- **[deskpet-tauri](deskpet-tauri/)** —— Tauri 2 版。同一套素材与交互（无边框/透明/置顶/跳任务栏、三档尺寸、双击轮播、原生右键菜单、Esc 退出），用 Rust + WebView2 实现，构建产物是一个 3.2MB 的单文件 exe（帧与 mp4 外置在 `assets\`）。

> 早期的网页原型 `deskpet-demo/`（单个 3.6MB `index.html`）已移除；需要查阅可执行 `git log --diff-filter=D -- deskpet-demo/` 从历史里取出。

用法、截图、帧序列重建方式和已知限制都在 **[deskpet-python/README.md](deskpet-python/README.md)**。

> 仓库**不含**透明帧序列（`packs/<packId>/frames/`，已被 `.gitignore` 排除）：DSMaid 11 段合计 53.3MB，从 `material/` 里的绿幕 mp4 用工坊重新生成，或从 Release 附件下载。首次运行请按 README 执行 `uv sync` + `uv run python build\migrate_packs.py`（或从 Release 解压 `packs/`）。

## 两个实现的对比

两版**行为等价**：同一套 pack 素材（`deskpet-tauri/build/prepare_assets.py --assets` 从 `deskpet-python` 当前 active pack 同步）、同窗口尺寸（中档 = 屏幕可用宽 15%）、同样的右下角锚点与三档换算、同样的双击轮播与原生右键菜单。下表是同一台机器、同一素材、待机 60s 的同口径实测（脚本 `deskpet-tauri/build/bench.py`）：

| 指标 | deskpet-tauri（Tauri/WebView2） | deskpet-python（PyQt6） |
| --- | --- | --- |
| 仅程序本体 | **3.2MB**（单 exe，素材外置） | 19.6MB（`pet.py` + 4 段帧） |
| 含运行时依赖 | 76.1MB（exe 3.2 + assets 72.9） | 244.9MB（+ PyQt6 225.3MB） |
| 待机内存（含子进程） | 549.8MB 均值 / 593.2MB 峰值（8 进程） | **64.3MB 均值 / 65.2MB 峰值**（1 进程） |
| 待机 CPU（单核） | **7.3%~13.6%** | 18.5% |
| 首帧可见 | **1240ms** 进程内（3 次 1188/1344/1456；外置多一次磁盘读取） | **203ms** 进程内 |

![体积与内存对比](deskpet-tauri/docs/screenshots/bench-compare.png)

结论：

- **体积**：外置后 Tauri 本体从 22.8MB 降到 3.2MB，但分发要带 `assets\`（72.9MB）；"单文件拷走"变成"exe + 一个目录"，换来换素材不用重编译。
- **内存**：差距不是页面泄漏，而是 WebView2 的多进程基线——browser / gpu / renderer / utility / crashpad 常驻约 500MB；单看主进程 Tauri 32.4MB，比 python 版的 64.3MB 还小。
- **CPU / 首帧**：Tauri 用整段常驻位图换来更低 CPU（7.3%~13.6% vs 18.5%），但 WebView2 初始化 + 外置素材多一次磁盘读取让它首帧约 1.2s。
- **取舍建议**：桌面常驻、内存敏感、要换素材系列的机器用 python 版（帧在磁盘上直接替换）；需要单一可执行分发、不想装 Python/PyQt 的场景用 tauri。两版交互一致，素材由 prepare_assets 从同一份 pack 同步。

实现细节、构建方式、踩坑记录见 **[deskpet-tauri/README.md](deskpet-tauri/README.md)**。

> 仓库**不含**透明帧序列（`packs/<packId>/frames/`，已被 `.gitignore` 排除）：DSMaid 11 段合计 53.3MB。首次运行按 README 从 `material/` 的源视频重建，或从 Release 附件下载 `packs/`。

## License

代码部分采用 **Apache License 2.0**（全文见 [LICENSE](LICENSE)）：可自由使用、修改、商用，需保留版权声明与许可声明。

`deskpet-python/assets/` 下的两段视频为 AI 生成素材，版权归生成平台/账号方所有，不在上述代码许可范围内，商用或再分发前请自行核对平台条款。
