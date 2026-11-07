# dsh-deskpet-bridge

ZhiLing 桌宠的 DSH 插件：把 DSH Desktop 的对话流、工具调用、审批与选择题事件，广播成一个本机
WebSocket（默认 `127.0.0.1:55471`），供两版桌宠各自订阅——Python 版经 `build/tools/dsh_bridge.py`
中转，Tauri 版用内置客户端直接连。

- **零依赖**：纯 Node ESM，`package.json` 没有 `dependencies`，不需要 install / build，clone 下来即可装。
- 需要带「插件管理」的 DSH Desktop（插件界面能「安装第三方插件」、支持 profile 的
  `dsh.profile.bundles`）；本包 `manifestVersion: 1`，`bundle.patch` 指向 `cordis.patch.yml`。

## 安装

1. DSH 设置 →「插件」→ 添加插件，选「安装第三方插件」，填本机目录 `<仓库路径>\dsh-deskpet-bridge`
   （也接受 npm 包名或 Git 地址）；
2. 点「立即启用」，**重启 DSH Desktop** 才会加载（它是以 bundle patch 形式挂进 profile 的）；
3. 落盘方式：DSH 用 pnpm 以 `link:` 把本目录写进 `%USERPROFILE%\.dsh\profiles\desktop\package.json`
   的 `dependencies`，并在 `dsh.profile.bundles` 里登记本插件。

验证：浏览器或 `curl http://127.0.0.1:55471/health` 能返回 JSON 即已加载。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `port` | `55471` | 监听端口。改这里后要同步两处：Python 桥的 `--ws-port`、Tauri 设置「提醒与连接」里的 DSH 端口 |
| `allowRemote` | `false` | 只绑 `127.0.0.1`，并拒绝非回环来源的 WS 请求；保持关闭，不要暴露到局域网 |
| `debug` | `false` | 打开后才逐条记录广播与 token 明细，排查时临时开 |

## `/health`

```json
{"ok":true,"plugin":"@local/dsh-deskpet-bridge","version":3,"pid":22524,"port":55471,
 "uptimeSec":41793,"sockets":2,"events":138294,"tokens":136545,
 "lastType":"status","lastAt":"2026-11-07T03:11:48.775Z"}
```

`sockets` = 当前 WebSocket 客户端数。内部是一个 `Set`，广播遍历全部连接，**支持多客户端**——Python 版与
Tauri 版可以同时接同一个插件，互不影响。`events` / `tokens` 是累计计数，`lastType` / `lastAt` 是最近一条广播。

## 日志

固定写到 `deskpet-python\build\cache\dsh_bridge_plugin.log`（环境变量 `DESHPET_PLUGIN_LOG` 可改路径）。
单文件上限 **2 MB**，超过就轮转为 `.1`（只留 1 份，不按时间戳累积）；默认只记加载、监听、连接/断开、
审批与错误，高频事件明细需 `debug=true`。

事件映射（DSH 事件 → 桌宠状态）见根 README 的「DSH 插件」章节；
桥的两端排查看 [deskpet-python/README.md](../deskpet-python/README.md) 的 4.2 节。
