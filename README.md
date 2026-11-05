# zhiling

蓝鲸女仆桌宠。仓库里有两版实现：

- **[deskpet-mini](deskpet-mini/)** —— 正式版。Windows 桌面悬浮窗（Python + PyQt6），绿幕视频抠成透明序列后贴在桌面上循环播放，可拖动、双击播一次动作、右键切小/中/大三档尺寸。
- **[deskpet-tauri](deskpet-tauri/)** —— Tauri 2 版。同一套素材与交互（无边框/透明/置顶/跳任务栏、三档尺寸、双击轮播、原生右键菜单、Esc 退出），用 Rust + WebView2 实现，构建产物是一个单文件 exe。
- **[deskpet-demo](deskpet-demo/)** —— 早期网页原型（单个 `index.html`，浏览器直接打开）。

用法、截图、帧序列重建方式和已知限制都在 **[deskpet-mini/README.md](deskpet-mini/README.md)**。

> 仓库**不含**透明帧序列（`frames_idle/`、`frames_action/`，已被 `.gitignore` 排除）：重建后是 WebP，约 9.4MB（PNG 则为 63.9MB）。首次运行请按 README 执行 `python build\build_frames.py`，从 `assets/` 里的两段源视频重建。

## 两个实现的对比

两版**行为等价**：同一份 4 段 WebP 素材（`build/prepare_assets.py` 从 mini 同步）、同窗口尺寸（中档 = 屏幕可用宽 15%）、同样的右下角锚点与三档换算、同样的双击轮播与原生右键菜单。下表是同一台机器、同一素材、待机 60s 的同口径实测（脚本 `deskpet-tauri/build/bench.py`）：

| 指标 | deskpet-tauri（Tauri/WebView2） | deskpet-mini（PyQt6） |
| --- | --- | --- |
| 仅程序本体 | **22.8MB**（单 exe，素材内嵌） | 19.6MB（`pet.py` + 4 段帧） |
| 含运行时依赖 | 935.9MB（+ WebView2 Runtime 913.1MB，Win11 通常预装） | 244.9MB（+ PyQt6 225.3MB） |
| 待机内存（含子进程） | 560.9MB 均值 / 573.2MB 峰值（8 进程） | **62.4MB 均值 / 65.2MB 峰值**（1 进程） |
| 待机 CPU（单核） | **8.9%** | 18.9% |
| 首帧可见 | ~1023ms 进程内 / 1087ms 外部观测 | **156ms 进程内 / 400ms 外部观测** |

![体积与内存对比](deskpet-tauri/docs/screenshots/bench-compare.png)

结论：

- **体积**：两版本体几乎持平（22.8 vs 19.6MB）；Tauri 的优势是"一个 exe 拷走就能跑"，代价是这台机器必须已有 WebView2 Runtime。
- **内存**：差距不是页面泄漏，而是 WebView2 的多进程基线——browser / gpu / renderer / utility / crashpad 常驻约 500MB；单看主进程 Tauri 32.4MB，比 mini 的 62.4MB 还小。
- **CPU / 首帧**：Tauri 用整段常驻位图换来更低 CPU（8.9% vs 18.9%），但 WebView2 初始化让它首帧慢约 0.9s。
- **取舍建议**：桌面常驻、内存敏感的机器继续用 mini；需要单文件分发、不想装 Python/PyQt 的场景用 tauri。两版交互一致，素材由同一脚本同步，可以并行维护。

实现细节、构建方式、踩坑记录见 **[deskpet-tauri/README.md](deskpet-tauri/README.md)**。

> 仓库**不含**透明帧序列（`frames_idle/`、`frames_action/`，已被 `.gitignore` 排除）：重建后是 WebP，约 9.4MB（PNG 则为 63.9MB）。首次运行请按 README 执行 `python build\build_frames.py`，从 `assets/` 里的两段源视频重建。

## License

代码部分采用 **Apache License 2.0**（全文见 [LICENSE](LICENSE)）：可自由使用、修改、商用，需保留版权声明与许可声明。

`deskpet-mini/assets/` 下的两段视频为 AI 生成素材，版权归生成平台/账号方所有，不在上述代码许可范围内，商用或再分发前请自行核对平台条款。
