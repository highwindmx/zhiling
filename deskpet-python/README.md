# deskpet-python · 桌宠（主线）

透明、置顶、不抢焦点的桌面宠物。读取本目录下 `packs\<packId>\` 里的素材与状态映射，按 DSH 的
`status/token/done/error` 事件切动画。**Python 版是主线，Tauri 版只是打包形态。**

> 测试/验证截图不入库、不上传 GitHub；要留证据就放本地 `build\screenshots\`（已 ignore）。

## 安装

```bash
uv sync                 # 生成/更新 .venv（Python >= 3.11）
uv run python pet.py --width 300
```

- `.venv` 由 uv 管理，不要手动 `pip install` 进同一个环境。
- **仅当没有 uv** 的备选路径：`.venv\Scripts\pip.exe install -r requirements.txt`（`requirements.txt` 为 legacy，两条路径不要混用）。
- `uv.lock` 已入库，可复现。

## 运行

```bash
run.bat                              # 双击即可（优先 .venv\Scripts\pythonw.exe）
uv run python pet.py --width 300     # 前台跑，便于看日志
uv run python pet.py --pack maid-basic   # 临时用另一个 pack
```

- 右键菜单：尺寸（小/中/大）、动作列表（全部 15 段）、素材工坊…、关于素材…、退出。
- 双击宠物：轮播动作；Esc 退出需要先点一下宠物让它拿到焦点。
- `--verbose` 会在启动时打印 pack 画布与素材 license；`--log <file>` 记录首帧/动作时间点。

## 素材包

```
packs\<packId>\                # 本目录内（deskpet-python\packs\）
  manifest.json      # 画布、角色、license、每段 fps/anchor/scale/offset
  clips.json         # 菜单顺序、中文名、type、defaultIdle
  state-map.json     # 10 个状态 → clipId（可热加载，去抖 800ms）
  frames\<clipId>\f0000.webp ...
```

- **packs 位置**：默认 `deskpet-python\packs\`（随本目录一起，可整体搬走）。程序按 `deskpet-python\packs\` → 仓库根 `packs\` 的顺序找，第一个存在的就是它；老仓库不迁移也能跑。
- `canvas` 是 **pack 级**属性（新建默认 576×530，可改）；同一 pack 内所有 clip 必须同尺寸，跨 pack 不能混用帧。加载时校验首帧尺寸，不一致会退出并列出所有出问题的段。
- 切换 pack：改 `packs\active.json` 的 `{"pack": "..."}` 后**重启**宠物；窗口尺寸与右下边距按新 canvas 重算。
- 详细规范：本地 `docs\material-packs.md`（docs 不入库、不推 GitHub）。

## 素材工坊

```bash
uv run python build\tools\workshop.py     # 或双击 workshop.bat（优先 uv，找不到时降级 .venv）
```

三步：**导入**（拖入 mp4、多选、中文名 + 自动 ASCII `clipId`）→ **抠像**（默认只露「自动裁黑边」「取背景色」，色相/羽化等在"高级"里）→ **导出**（目标 pack、画布、fps、webp q80）。

- 默认值一次跑通：拖进新视频什么都不改，点生成即可用。
- 输出直接写 `packs\<packId>\frames\<clipId>\` 并合并 `manifest.json`；同 `clipId` 重抠会覆盖，旧文件备份到 `packs\<packId>\.backup\`。
- 工坊不是运行时依赖：桌宠只需要 `frames\` 目录。`build\cache` 可随时删除，重建代价实测约 44 秒/96 帧。

## 状态映射

`packs\<packId>\state-map.json`，mini 与 tauri 读同一份。默认表：

| 状态 | 动作 | clipId | 优先级 |
|---|---|---|---|
| 待机 | 待机 | `dsh_idle` | 0 |
| 长待机（10 分钟） | 累到瘫倒 / 打哈欠 | `dsh_tired` / `dsh_yawn` | 10 / 12 |
| 等你回应（done 后 30s） | 思考状 | `dsh_think` | 25 |
| 模型在忙 | 思考状 | `dsh_think` | 35 |
| 流式输出 | 工作中 | `dsh_work` | 30 |
| 完成 | 递交文件 | `dsh_handover` | 45 |
| 待你决策 | 举手提问 | `dsh_handup` | 55 |
| 出错 | 不高兴 | `dsh_unhappy` | 60 |
| 单击 | 得意状 | `dsh_proud` | 70 |
| 双击 | 炸毛 | `dsh_shock` | 85 |

- `needs_decision` **只认 DSH 的 `attention` 事件**（`kind/options/timeoutSec`），没有事件就不播。启发式已否决，别再加。
- 另有 15 段动作可手动换映射：真开心 `dsh_happy`、无奈状 `dsh_helpless`、看手机 `dsh_phone`、吃米饭 `dsh_rice`、举手提问 `dsh_handup`、高兴跳起 `dsh_cheer` 等。
- 设计细节与 DSH 改动清单：本地 `docs\action-conversation-mapping.md`（docs 不入库、不推 GitHub）。

## 已知限制

- 透明区域仍会挡住鼠标（Qt 的 `WA_TransparentForMouseEvents` 全窗生效，不能只对透明像素生效；已用掩码命中测试近似，未根本解决）。
- 首帧 333ms、6s 工作集 57.8MB（300px 宽，本机实测；比上一轮 203ms 多了启动时的首帧尺寸校验）。
- mini 与 tauri 各有一份同义动作（两边 `clips.json` 语义一致，但帧不共享）。
- 部分素材有水印残留，工坊第 2 步只能"挡"不能"去"。

## 排错

- **uv 找不到**：`workshop.bat` 会明确提示并降级到 `.venv\Scripts\python.exe`；两者都没有就报错退出。
- **黑窗/没有素材**：`packs\active.json` 指向的 pack 不存在，或 `frames\` 缺失；看 `--verbose` 输出。
- **`.venv` 被占用**：先退出正在跑的桌宠，再 `uv sync`。
- **改了 `active.json` 不生效**：pack 在启动时读取，必须重启宠物。
- **tauri 的 `target` 写入被拒**：用 `--target-dir %TEMP%` 或结束旧进程后重试。
