# sample-pack（自带最小示例素材）

首次运行且 `%LOCALAPPDATA%\DeskPet\packs` 里没有任何主题时，程序会把这里的文件按原样释放到
`%LOCALAPPDATA%\DeskPet\packs`（已存在的一律跳过，不覆盖用户数据），保证「至少能出画面」。

- `themes/sample.json`：示例主题（id `pack`/theme 均为 `sample`），只绑定 `idle`。
- `animations/sample_idle/meta.json`：一段 128×128 的单帧待机画面。
- `active.json`：当前主题镜像。

这一整套会在编译期被嵌入 exe（`build.rs` 遍历本目录生成 `SAMPLE_FILES`），所以运行时不依赖
这个目录是否存在。当前示例画面用程序图标（`src-tauri/icons/128x128.png`）作为占位帧：
仓库里不放第三方动画帧，保持仓库干净。想换成真正的待机动画，只需把
`animations/sample_idle/f0000.png … fNNNN.png|webp` 和 `meta.json`（`count`/`format` 对应上）
放进本目录，然后重新编译即可，无需改代码。

完整素材包请安装到 `%LOCALAPPDATA%\DeskPet\packs`，详见仓库 README 的
「获取与安装素材包」一节。
