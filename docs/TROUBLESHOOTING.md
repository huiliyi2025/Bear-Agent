# Troubleshooting

先运行：

```bash
bear doctor
bear doctor --json
```

Doctor 默认只读；`--active` 会发送最小 Provider 请求并启动已配置 MCP Server，可能产生费用/执行你配置的命令。

## 启动与配置

**`provider ... has no API key`**

设置 `api_key_env` 指向的环境变量，或使用 `secret://name`/`bear secret set`。示例配置不包含真实密钥。

**`failed to start extensions`**

查看 `bear plugins validate`、`bear mcp status`、`bear hooks list`。MCP Server 必须 `trusted=true`；外部 Hook 需要 `hooks.allow_external=true`。失败时不会启动一个部分可用的扩展链。

**Windows 上 Shell 不可用**

默认 `sandbox.mode=require`，Windows Job Object 只有进程级 containment，没有文件系统隔离，因此 Shell fail-closed。显式设置 `sandbox.mode="process"` 接受进程级隔离；这明确不提供文件系统隔离。`off` 是显式 opt-out，不是沙箱。

**macOS Shell / Seatbelt**

`sandbox-exec` 已弃用但仍是当前后端；CI 只覆盖 profile 构造/单元测试，未验证真实执行。若 Seatbelt 不可用，Shell 会 fail-closed。

## TUI

**无法启动 TUI**

`bear tui` 需要真实 TTY；CI/管道环境使用 `bear run`、`bear chat` 或 `--json`。Windows Terminal/PowerShell 7、Linux 终端应支持。

**TUI 显示乱码**

使用 UTF-8 终端；Windows 下 Bear-Agent 会尝试重配 stdout 为 UTF-8。避免旧式 `cmd.exe` 代码页。

**取消/退出后仍有进程**

`Ctrl+C`/`/cancel` 取消 Runtime；`Ctrl+Q`/`/exit` 会等待清理并关闭 MCP/DB。若底层 Shell 子进程逃逸 OS 进程树，Windows/POSIX 的现有 Sandbox/Job Object 限制仍适用；用 `bear sessions recover` 或系统进程工具检查。

**审批 Modal 不出现**

确认 Permission Mode 为 `supervised` 且 Tool 的 preflight 返回 ask；`--yes`/AutoApprover 只用于无头自动化，TUI 默认使用交互审批。

## Provider

**Retry / Fallback 频繁**

`bear providers show` 查看 routes、Circuit 状态与能力；`bear metrics providers` 查看 retry/fallback 次数。Partial stream 不会重试/切换：检查 trace/日志中的 `partial_stream`。

**Context overflow**

`bear metrics show --section context`、`/compact`；调低 `context.trigger_ratio` 或提高 `agent.context_window`。压缩不会修改原始 Transcript。

## Session / Task / Recovery

**Session is already running**

同一 Session 只有一个租约。另一个进程/崩溃 Run 可能仍持有；等待租约过期后 `bear sessions recover <id>`，不要强删数据库。

**Recovery blocked**

中间乱序 Transcript 或不安全状态不会自动修复。用 `bear trace`/`bear sessions show` 检查，或从更早位置 `bear sessions fork`。

**Task blocked / retry refused**

执行过非幂等 Tool 的失败 Task 不会自动重试；检查证据并显式 `bear tasks retry --force` 或修订 Plan。

## Observability / Benchmark

**`bear trace list` 为空**

Trace 可能被配置关闭/采样，或已按保留策略裁剪。`bear trace stats` 与 `bear doctor --category observability`。

**Metrics 成本为 0/缺失**

配置 `[observability.pricing]`；`pricing_missing` 表示没有价格，不编造成本。

**Benchmark skipped**

真实 Provider 需要 `--allow-network` 与可用 Key；Shell/Sandbox 在主机能力不足时 SKIP；这是有意的安全行为。

## CI / 安装

```bash
uv sync --frozen
uv build
uv tool install dist/bear_agent-0.7.0-py3-none-any.whl
bear --version
bear doctor
```

CI 覆盖 Ubuntu/Windows/macOS；macOS Seatbelt 实机操作仍需人工确认。详细验证边界见 `P7-COMPLETION-REPORT.md`。