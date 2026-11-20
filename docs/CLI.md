# CLI 参考

所有命令都从同一配置加载器与组合根启动；`--config` 可放在子命令后。密钥优先使用环境变量或 `secret://`，不要写入命令历史。

## 入口

```text
bear                      真实终端进入 TUI；非 TTY 回退经典 chat
bear tui                  强制 TUI（需要 TTY）
bear chat                 经典流式 chat
bear run <prompt>          单次无头执行
```

## Session / Run

```text
bear sessions list [--limit N]
bear sessions show <session-id> [--messages N]
bear sessions checkpoints <session-id>
bear sessions recover <session-id>
bear sessions fork <session-id> [--at SEQ] [--checkpoint ID] [--title TEXT]
```

`run` 支持 `--resume`、`--json`、`--yes`、`--permission`、`--max-turns`；`--json` 输出纯 NDJSON，不混入人类提示。

## Task / Plan

```text
bear tasks plan <goal> [--session ID] [--run] [--json]
bear tasks show <session-id> [--attempts/--no-attempts]
bear tasks run <session-id> [--cwd DIR] [--yes] [--json]
bear tasks task <task-id>
bear tasks revise <session-id> --file PLAN.json --reason TEXT
bear tasks cancel <session-id> [--task TASK_ID]
bear tasks retry <task-id> [--force]
bear tasks resume <session-id> [--run]
```

需要审批的 Task Run 在无 TTY 时默认拒绝；`--yes` 是显式自动化选择。

## 扩展

```text
bear providers show [--json]
bear providers test [--provider P] [--model M]
bear mcp status
bear mcp tools
bear plugins list/info/validate/enable/disable
bear hooks list [--json]
```

MCP Server 必须显式 trusted；Plugin Python 默认关闭；外部 Hook 默认关闭。

## 可观测性

```text
bear trace list [--session ID] [--run ID] [--status S] [--since 7d] [--json]
bear trace show <trace-id> [--events] [--json]
bear trace run <run-id> [--json]
bear trace stats [--json]
bear trace export -o FILE [--trace ID|--run ID|--session ID]
bear trace prune --yes

bear metrics show [--since 7d|24h|all] [--session ID] [--section S] [--json] [--export FILE]
bear metrics providers/tools/tasks/recovery [--json]
bear metrics export -o FILE

bear bench list [--category C] [--json]
bear bench run <id> [--iterations N] [--warmup N] [--param k=v] [--trace] [--no-save] [--allow-network] [--json]
bear bench run --all [--iterations N]
bear bench report [run-id] [--samples] [--json] [--output FILE]
bear bench history [--scenario ID] [--json]
bear bench compare <run-a> <run-b> [--threshold PCT] [--json]
bear bench export <run-id> -o FILE
bear bench prune --yes
```

## 运维 / 安全

```text
bear init [--force]
bear doctor [--json] [--export FILE] [--active] [--category C]
bear config show [--json]
bear config path
bear audit verify
bear audit tail [--limit N]
bear secret set <name>
bear secret list
bear secret delete <name>
```

`doctor` 默认只做静态检查；`--active` 才发送最小 Provider 请求/启动 MCP。`config show`、审计、Trace/Metrics 导出均经过脱敏。

## Exit Code

- `0`：成功或仅 WARN 的 doctor。
- `1`：Run 非 COMPLETED、doctor FAIL、Benchmark failed。
- `2`：参数/配置/未知对象等可操作错误。
- 交互模式退出码由 Typer 正常返回。

## 输出约定

- `--json` 命令只输出一个 JSON 文档或 NDJSON；进度/提示不写入 stdout。配置/参数预检错误写 stderr（stdout 可能为空），成功后 stdout 保持纯 JSON/NDJSON。
- 人类模式使用 Rich；颜色可被重定向禁用。
- 密码、API Key、Authorization Header、MCP env/headers 不输出。