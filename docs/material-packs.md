# 素材包（Material Pack）规范

桌宠的"素材"不再是一个扁平 `frames_<clipId>` 目录，而是 **pack**：一个可替换、可分发、可多角色并存的素材包。两版桌宠（Qt 的 `deskpet-python`、Tauri 的 `deskpet-tauri`）读同一份 pack，字段语义不许分叉。

---

## 1. 目录结构

```
packs/
├─ active.json                     # 当前启用的 pack（{"pack": "maid-dsh"}
├─ maid-dsh/                       # packId（ASCII，目录名就是 id）
│  ├─ manifest.json                # pack 级 + 段级元数据（进 git）
│  ├─ clips.json                   # 段级运行时清单（由 manifest 生成，进 git）
│  ├─ state-map.json               # 状态 → 动画映射（进 git，用户可改）
│  ├─ frames/                      # 帧序列（不进 git，走 Release 附件）
│  │  ├─ dsh_idle/f0001.webp ... f0150.webp
│  │  ├─ dsh_work/f0001.webp ...
│  │  └─ dsh_shock/f0001.webp ...
│  ├─ posters/                     # 可选：菜单/商店用的封面图（png/webp，不进 git）
│  │  └─ dsh_idle.png
│  ├─ source/                      # 可选：原始绿幕 mp4（不进 git，仅本地留档）
│  │  └─ 炸毛.mp4
│  ├─ .backup/                     # 重抠时的旧帧备份（不进 git，可回滚）
│  │  └─ dsh_shock_20261105-1410/
│  └─ LICENSE.txt                  # 可选：素材许可原文
├─ maid-basic/
│  ├─ manifest.json
│  ├─ clips.json
│  └─ frames/{idle,action,action2,action3}/
└─ .legacy/                        # 迁移前的旧清单备份（不进 git）
```

要点：

- **packId 是 ASCII**（`maid-dsh`、`neko`），目录名即 id；中文只出现在 `role` / `displayName`。
- `frames/<clipId>/` 里的文件名固定 `f%04d.webp`（从 `f0001` 开始，1 基）；也认 `.png`（老素材）。
- `clips.json` 与 `manifest.json` 都由 `build/pack.py` 写，**手改优先改 manifest**，再跑 `python build/pack.py sync` 同步。
- 切换角色 = 改 `packs/active.json` 一行；不复制帧、不重命名。

### 迁移（老仓库）

老仓库把帧放在 `deskpet-python/frames_<clipId>/`。迁移脚本幂等、可先干跑：

```bat
uv run python build\migrate_packs.py --dry-run   :: 打印计划
uv run python build\migrate_packs.py             :: 移动（不复制）
```

- 分段：`dsh_*` → `packs/maid-dsh`，其余（`idle/action/action2/action3`）→ `packs/maid-basic`
- 旧 `meta.json` / `clips.json` / 旧 manifest 先备份到 `packs/.legacy/<文件名>.<时间戳>`
- 自检：帧数 == `count`、`f0001.*` 存在、manifest 能被两版解析
- **不迁移也能跑**：程序先找 `packs/`，找不到任何 pack 才回退旧 `frames_*`，并在启动日志里提示"你还在用旧目录，建议运行 migrate_packs.py"

---

## 2. manifest 字段

```json
{
  "version": 1,
  "packId": "maid-dsh",
  "charId": "maid-dsh",
  "role": "蓝鲸女仆（DSMaid）",
  "canvas": { "w": 576, "h": 530 },
  "frames": { "format": "webp", "quality": 80, "fps": 24 },
  "defaultIdle": "dsh_idle",
  "license": {
    "source": "DSMaid / 即梦（绿幕 mp4）",
    "license": "AI 生成，未授权商用",
    "author": "DSMaid",
    "url": ""
  },
  "clips": [
    {
      "clipId": "dsh_shock",
      "displayName": "炸毛",
      "type": "action",
      "loop": false,
      "priority": 0,
      "dir": "frames/dsh_shock",
      "fps": 24,
      "count": 110,
      "scale": 1.095,
      "offset": [0, 0],
      "anchor": { "foot": [0.5, 0.996] },
      "tags": ["reaction", "angry"],
      "crop": [174, 0, 654, 480],
      "source": "material/DSMaid/炸毛.mp4",
      "keyer": { "hue_ref": 136.0, "keep_frac": 0.05 }
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 是 | 结构版本，当前 1；读到更高版本直接拒绝并提示升级 |
| `packId` / `charId` | 是 | ASCII 稳定键，等于目录名；`charId` 预留给"同一角色多个皮肤" |
| `role` | 是 | 角色显示名（可中文），菜单分组/设置页显示 |
| `canvas` | 是 | **pack 级统一画布**（默认 576×530）。所有 clip 都按这个画布缩放和锚点对齐；逐段不同会导致换动作时角色跳动 |
| `frames.format` / `quality` | 是 | `webp`（默认 q80）或 `png`；pack 级统一 |
| `frames.fps` | 是 | 默认 24；源 30fps 由工坊在抽帧时重采样，日志会写明 |
| `defaultIdle` | 是 | 默认待机 clipId（默认 `dsh_idle`）。**不要靠"两个 type=idle"表达**，一个字段就够；换待机只改这一行 |
| `license` | 是（可为空串） | 来源/许可/作者/链接，为将来商用留位。空字段不阻断运行，但分发前应填 |
| `clips[].clipId` | 是 | ASCII 键（`[0-9A-Za-z_-]`），唯一；菜单/配置/状态映射都引用它 |
| `clips[].displayName` | 是 | 中文显示名（菜单里看到的文字） |
| `clips[].type` | 是 | `idle` 或 `action` |
| `clips[].loop` | 是 | 待机 true；动作一般 false |
| `clips[].priority` | 是 | 0~100，动作相对排序（状态映射里还有自己的 priority，二者不同层） |
| `clips[].dir` | 是 | 相对 pack 根，默认 `frames/<clipId>` |
| `clips[].fps` / `count` | 是 | 声明值，用于自检；`count` 与目录内帧数不一致会在启动日志报警 |
| `clips[].scale` / `offset` | 否 | 生成时的缩放/位移（整数像素），默认 `1.0 / [0,0]` |
| `clips[].anchor.foot` | 否 | **脚底锚点**，归一化 `[x, y]`（0~1，相对画布）。缺省 `[0.5, 0.996]`；也支持 `head` / `face` 框（`{"head":[x,y], "face":[x,y,w,h]}`） |
| `clips[].tags` | 否 | 检索/推荐用标签 |
| `clips[].crop` | 否 | 生成时整片统一裁边 `[l, t, r, b]`，仅记录，运行时不裁 |
| `clips[].source` / `keyer` | 否 | 溯源与抠像参数，方便重抠复现 |

---

## 3. 不兼容与容错

原则：**能降级就降级并提示，不能降级才拒绝**；任何情况下都不黑窗静默退出。

| 情况 | 处理 | 日志/报错文案（原样） |
|---|---|---|
| 缺整个 `packs/` | 回退旧 `frames_*`；也没有则显示红框提示 + 缩窗，不加载动画 | `找不到素材目录：<路径>` / `packs/ 里没有可用素材包，回退旧 frames_*；建议运行 migrate_packs.py` |
| `manifest.json` 损坏（JSON 语法错） | 拒绝该 pack，尝试其它 pack；全坏则红框报错 | `manifest.json 解析失败（第 N 行）：<原因>；已跳过 packs/<id>` |
| `version` 高于支持值 | 拒绝该 pack | `manifest version=2 高于本程序支持的 1，请升级桌宠` |
| `canvas` 缺失/非法 | 用默认 576×530，并提示（可能锚点偏移） | `canvas 缺失，按 576×530 载入；若角色位置异常请检查 manifest` |
| `defaultIdle` 指向不存在的 clip | 回退到第一个 `type=idle`，再回退第一个 clip | `defaultIdle=dsh_idle 不存在，已回退到 <id>` |
| 某 clip 帧目录缺失 | 跳过该 clip（菜单不显示），其余照常 | `跳过 dsh_shock：缺 frames/dsh_shock/` |
| 某 clip 帧数 < 声明的 `count` | 按实际帧数播放，警告 | `dsh_shock 实际 108 帧，manifest 声明 110；按 108 播放` |
| 某 clip 尺寸 ≠ canvas | **拒绝该 clip**（不缩放，避免错位/糊） | `跳过 dsh_shock：帧尺寸 640×720 与画布 576×530 不一致` |
| `fps` 与 pack 不一致 | 警告，仍按该 clip 的 fps 播放（速度可能不同步） | `dsh_shock fps=30 与 pack 的 24 不一致，按 30 播放` |
| alpha 丢失（整帧不透明） | 仍然播放，但提示"透明区会挡下层" | `dsh_shock 疑似没有 alpha（全帧不透明），检查抠像导出` |
| `anchor` 缺失 | 用默认脚底锚点 | `dsh_shock 缺 anchor.foot，用默认 [0.5, 0.996]` |
| `clipId` 重复 | 后者被忽略 | `clipId dsh_work 重复，已忽略第 2 条` |
| `displayName` 缺失 | 用 `clipId` 显示 | 不报错，菜单显示 `dsh_work` |
| `state-map.json` 里引用了不存在的 clipId | 只废该条规则，其余生效 | `state-map 第 3 条无效：clipId=nope 不存在，已忽略` |

---

## 4. 接入流程：从绿幕 mp4 到菜单里的一个动作

工坊（`deskpet-python\build\workshop.py`）就是 **素材包编辑器**：它现在的输出已经是 `packs/<packId>/frames/<clipId>/` + 合并 `manifest.json`，不再写独立的 `frames_<clipId>`。不需要额外做"导出为 pack"按钮——工坊的导出目标本来就是 pack；要换系列就"新建 pack"。

```
① 视频拖进工坊  →  ② 预览/裁边/抠像  →  ③ 选目标 pack，点开始生成
                                              ↓
                         packs/<packId>/frames/<clipId>/f0001.webp ...
                         packs/<packId>/manifest.json 更新（scale/offset/anchor/fps/count）
                         packs/<packId>/clips.json 同步（active pack 时 deskpet-python/clips.json 也同步）
                                              ↓
                         右键菜单出现「动作：<displayName>」
                                              ↓
                         想让它自动出现 → 编辑 packs/<packId>/state-map.json（热加载，1s 内生效）
```

**逐步骤（含界面位置）**：

1. **拿到素材**：即梦/海螺生成绿幕 mp4，通常 1080×1920 或带黑边/水印。放进 `material/<系列>/`（该目录已 gitignore）。
2. **打开工坊**：宠物右键「素材工坊…」，或双击 `deskpet-python\workshop.bat`。
   > 截图占位：`docs/screenshots/workshop-ui.png`（三步布局总览）
3. **① 导入**：拖入一个或多个 mp4（支持多选排队）；列表会显示时长/分辨率/fps；填中文显示名（如「炸毛」），`clipId` 自动生成 `zha_mao`（装不上 pypinyin 就 `clip_01` 递增），灰色只读；要手改去「高级参数」。
4. **② 抠像**：默认 `自动裁黑边` 已开（裁掉区域在预览里画半透明红遮罩，能看出它裁了什么）；`取背景色（点四角取样）` 一键取绿幕色相；左边 `原片 / 抠像后 / alpha` 三视图 + 棋盘格，帧滑杆挑帧细看。
   > 截图占位：`docs/screenshots/workshop-step2.png`（裁边遮罩 + alpha 视图）
5. **③ 导出**：选 `目标 pack`（已有 pack 或「新建素材包」——弹窗只问 packId / 角色名 / 画布，画布默认 576×530 并提示为什么别改）。点 `开始生成`。
   导出页不提供画布/帧率/格式/质量——它们是 pack 级固定值（576×530 / 24fps / WebP q80）；30fps 源片会在日志里明确写"源片 30fps → 24fps，抽帧时统一重采样"。
   结果摘要会给出 `N 帧 / MB / 脚底锚点 y / 残留绿边 % / scale / offset`，并并排显示"新素材 vs 当前 pack 待机帧"，方便看角色有没有突然变大。
   > 截图占位：`docs/screenshots/workshop-step3.png`
6. **菜单里出现**：重启桌宠（或 tauri 热加载 clips；mini 需重启）→ 右键菜单「动作：炸毛」。所有 `type=action` 的 clip 都会出现在菜单里（这就是上一轮"无奈/吃米饭看不到"的原因：没登记到 clips.json）。
7. **自动触发（可选）**：编辑 `packs/<packId>/state-map.json`，把某个状态的 `clipId` 指到新动作；保存即热加载（去抖 800ms，解析失败保留上一版并提示）。

重抠同一段：改参数后仍用同一 `clipId` 生成 → **覆盖**旧帧，旧帧移到 `packs/<packId>/.backup/<clipId>_<时间戳>/`（留最近 3 份，可手动回滚），不产生 `_v2` 目录。

**工坊不属于运行时依赖**：桌宠运行只需要 `packs/<packId>/frames/`。Tauri 版是单 exe、不带 Python，所以它的菜单里点「素材工坊…」只在能找到 `workshop.bat` 时才拉起，否则提示"该功能需要 Python 版工具，请直接双击 deskpet-python\workshop.bat"（可通过 `DESKPET_WORKSHOP` 环境变量指定路径）。我们没有把工坊重写成 Rust。

---

## 5. 多角色切换与数据隔离

- **切换**：改 `packs/active.json`（`{"pack":"maid-basic"}`），或未来加设置页下拉。切换后窗口保持当前档位，按新 pack 的 `canvas` 重新计算尺寸与缩放。
- **窗口尺寸**：画布比例固定 576:530；宽度由档位决定（屏幕宽 10%/15%/30%），高度按比例算。**不按 pack 各自设窗口大小**，否则切角色会跳。
- **缩放/锚点**：全在 pack 的 `manifest.json` 里（`scale`/`offset`/`anchor.foot`），运行时按同一画布对齐脚底，所以换 pack 不需要改代码。
- **状态映射**：`state-map.json` **按 pack 分文件**（`packs/<packId>/state-map.json`），每个 pack 自己一套；互不影响，也不做隐式继承（新 pack 没写就全用默认 `defaultIdle`）。
- **亲密度/养成数据**：**按 packId 分目录**存放，建议 `packs/<packId>/pet-state.json`（或 `%APPDATA%/deskpet/<packId>/`）。当前实现还没有亲密度系统（状态映射里已留"用户响应计正向"的口子），落库时按这个规则隔离。
- **菜单**：单 pack 时不显示分组层；多 pack 时菜单第一层按 `role` 分组，第二层才是动作（`maid-dsh` 与 `maid-basic` 加起来 15 个动作，不分组会太长）。

---

## 6. 分发

**素材不进 git**（帧 + mp4 体积大且多为 AI 生成受限素材），manifest/clips/state-map **进 git**（它们是可评审的配置）。

| 内容 | 位置 | 是否进 git |
|---|---|---|
| `manifest.json` / `clips.json` / `state-map.json` / `active.json` | `packs/<packId>/` | ✅ |
| `frames/**`（webp 帧） | `packs/<packId>/frames/` | ❌ Release 附件 |
| `source/*.mp4`（原始绿幕） | `packs/<packId>/source/` | ❌ 仅本地留档 |
| `posters/`（封面） | `packs/<packId>/posters/` | ❌ Release 附件 |
| `.backup/`、`.legacy/` | pack 内 | ❌ |
| `material/`（用户素材库） | 仓库根 | ❌ |

`.gitignore` 覆盖：`packs/*/frames/`、`packs/**/frames/`、`packs/*/source/`、`packs/**/source/`、`packs/*/.backup/`、`packs/**/.backup/`、`packs/.legacy/`、`packs/*/posters/`、`material/`，并保留旧的 `**/frames_*/` 规则防遗漏。

**最小可发布包**（别人拿到就能跑）：

```
maid-dsh.zip
├─ manifest.json
├─ clips.json
├─ state-map.json
└─ frames/
   └─ <每个 clipId>/f0001.webp ... （11 段，53.3MB）
```

解压到 `packs/maid-dsh/`，再把 `packs/active.json` 指到它即可运行。**不需要** Python、不需要工坊、不需要源 mp4。

Tauri 版的"最小可运行组合"是 `deskpet-tauri.exe` + `assets\`（`prepare_assets.py --assets` 从当前 active pack 复制出 `clips.json` / `meta.json` / 帧目录 / `state-map.json`）。帧和 mp4 不进 exe，exe 只有 3.2MB。

---

## 7. 验收记录（默认值一次跑通）

`build\_accept.py` 只用 `default_params()`（`hue_ref=136` + 自动裁黑边）走 `build_frames.build_clip`，输出到 `build/_accept_out/`（跑完可删）：

| 素材 | 源 fps | 裁边 | 帧数 | 体积 | 耗时 | 残留绿 | 脚底 y |
|---|---|---|---|---|---|---|---|
| 得意状（30fps） | 30 → 24（重采样，日志写明） | 无黑边（并集 2px，可忽略） | 144 | 6.45 MB | 17.0s | 0.27% | 510 |
| 炸毛（黑边+绿幕） | 24 | 并集 `[170,2,658,478]` | 110 | 3.79 MB | 27.7s | 0.28%（最大 0.72%） | 528 |

结论：拖进一个新视频、什么都不改、点开始生成，两段结果都可用（残留绿 < 0.3%）；30→24 的转换有日志，不是偷偷做的。

---

## 8. 需要拍板的三个点

1. **多 pack 的选择入口**：现在只有 `packs/active.json` 一行。要不要在右键菜单/设置页加"角色切换"下拉？加了就得决定：切角色是否记住每个 pack 的窗口位置与档位（建议：记住，按 packId 分目录存 `pet-state.json`）。
2. **亲密度落库**：状态映射里已留"用户响应 `needs_decision` 计正向、超时不扣分"的产品判断，但还没存。落 `packs/<packId>/pet-state.json` 还是 `%APPDATA%/deskpet/<packId>/`？前者随包走（换机器带走亲密度），后者更符合"用户数据不跟随素材包"。
3. **素材许可**：`license` 字段目前是占位（"AI 生成，未授权商用"）。将来若商用分发，需要逐 pack 确认来源与授权；是否要加一个"许可非空才允许打包"的检查？
