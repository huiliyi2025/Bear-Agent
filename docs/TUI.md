# Bear-Agent TUI

`bear tui`（在真实终端直接运行 `bear`）打开 Bear-Agent 的交互式终端界面。它复用现有 CLI 相同的 `Container`、`Runtime`、`ToolExecutor`、`ApprovalService`、`TaskService`、`Checkpoint`/`Recovery` 与持久化层；TUI 只是呈现与命令控制，不建立第二套执行链。

## 布局

```text
┌ Bear-Agent v0.7.0  session ...  idle  provider/model  permission=auto  ctx 1.2k/8k ┐
├───────────────────────────────────────────────┬──────────────────────────────────┤
│ transcript                                    │ Plan                              │
│ assistant / tool / policy / provider events    │ plan / task / dependency status   │
│                                               │ Runtime                           │
│ live assistant stream                         │ run / checkpoints / context       │
│                                               │ Extensions                        │
│                                               │ MCP / Plugins / Hooks             │
├───────────────────────────────────────────────┴──────────────────────────────────┤
│ Ask Bear-Agent or type /help                                                     │
└ ctrl+q Quit  ctrl+c Cancel  f1 Help  f2 Tasks  f3 Metrics ────────────────────────┘
```

- 顶部状态栏：Session、Run 状态、Provider/Model、权限模式、Context Token。
- 主区：Transcript；流式文本在 live 区显示，结束后归入 Transcript。
- 侧栏：Plan/Task/Todo、Context/Checkpoint、MCP/Plugin/Hook 状态。
- 底部：输入框与快捷键提示。

## 快捷键

| 键 | 行为 |
|---|---|
| `Enter` | 发送消息或命令 |
| `Ctrl+Q` | 退出并清理 |
| `Ctrl+C` | 取消当前 Run 或后台 Job（不会自动退出） |
| `F1` | 帮助 |
| `F2` | 刷新 Task 面板 |
| `F3` | 显示实时 Metrics |

## 命令

| 命令 | 行为 |
|---|---|
| `/help` | 命令帮助 |
| `/session` | 当前 Session 状态 |
| `/sessions` | 最近 Session 列表 |
| `/new` | 新建 Session |
| `/resume <id>` | 切换到已有 Session |
| `/fork [message_seq]` | 从当前位置或指定消息位置 Fork |
| `/plan <goal>` | 生成并校验 Plan |
| `/tasks` | 刷新 Plan/Task 面板 |
| `/run-plan` | 执行当前 Plan |
| `/task <id>` | 查看 Task、依赖、尝试与错误 |
| `/cancel-plan` | 取消 Plan 与未完成任务 |
| `/compact` | 强制分层 Context 压缩 |
| `/anchor <text>` / `/anchors` | 固定/查看 Anchor |
| `/checkpoints` | 查看 Checkpoint |
| `/run <text>` | 运行一次 Agent 请求 |
| `/cancel` | 取消当前 Run/Job |
| `/permission <mode>` | `plan`/`supervised`/`auto`/`unrestricted` |
| `/tools` | 已注册 Tool |
| `/metrics` | 当前进程实时 Metrics |
| `/trace` | 当前 Session 最近 Trace |
| `/status` | 刷新状态面板 |
| `/mcp` / `/plugins` / `/hooks` | 扩展状态 |
| `/doctor` | 运行静态 Diagnostics |
| `/exit` | 退出并清理 |

## 审批

需要审批的 Tool/Shell/MCP 调用会打开全屏 Modal：

- 显示 Tool 名称、Risk、原因、完整（已脱敏）参数；参数区可聚焦/滚动；默认不聚焦任何按钮，Enter 不会批准；
- `o` 允许一次、`s` 允许本 Session 相同调用、`p` 允许本 Project（如果有 project id）、`d`/`Esc` 拒绝；
- 不存在 Auto Approve 快捷键；未选择前 Tool 不会执行；
- 审批仍由既有 `ApprovalService` 持久化与精确指纹控制；
- 退出应用时 pending 审批被拒绝。

## 取消与资源清理

- `Ctrl+C`/`/cancel` 调用 `AgentRuntime.cancel`；Run 的 finally 会清理 Tool/Claim/Lease。
- `/exit`、`Ctrl+Q` 或关闭终端触发 `on_unmount`：取消 Run/Job、等待清理、停止 MCP/Plugin、关闭 Trace/DB。
- TUI 不直接调用 Tool；所有副作用仍经过 `ToolExecutor`、PermissionEngine、Approval、Claim、Audit、Sandbox。
- 退出不会让后台 Shell 继续持有 Bear Claim；但操作系统进程的最终回收仍依赖 Runtime/Sandbox 的现有清理语义。

## 信息层级

默认不展示每个内部事件：流式文本、Tool 结果、Plan/Task 状态、错误与审批优先。Provider Retry/Fallback、Compaction、Recovery、MCP、Hook 失败以简短状态或警告出现；完整细节使用对应 CLI（`bear trace`、`bear metrics`、`bear doctor`、`bear mcp status`）。

## 非目标

- 不实现编辑器、Diff 预览、鼠标布局或完整 IDE。
- 不替代 `bear run`/`--json` 的自动化路径。
- 不改变默认安全策略；TUI 不会自动批准任何操作。