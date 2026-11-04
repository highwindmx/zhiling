# zhiling

蓝鲸女仆桌宠。仓库里有两版实现：

- **[deskpet-mini](deskpet-mini/)** —— 正式版。Windows 桌面悬浮窗（Python + PyQt6），绿幕视频抠成透明序列后贴在桌面上循环播放，可拖动、双击播一次动作、右键切小/中/大三档尺寸。
- **[deskpet-demo](deskpet-demo/)** —— 早期网页原型（单个 `index.html`，浏览器直接打开）。

用法、截图、帧序列重建方式和已知限制都在 **[deskpet-mini/README.md](deskpet-mini/README.md)**。

> 仓库**不含** 63.9MB 的透明帧序列（`frames_idle/`、`frames_action/`，已被 `.gitignore` 排除）。首次运行请按 README 执行 `python build\build_frames.py`，从 `assets/` 里的两段源视频重建。
