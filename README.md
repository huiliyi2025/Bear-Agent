# Bear-Agent

本地优先、可恢复、可扩展的 Agent Harness。

Bear-Agent 把模型变成有边界、可观测的执行者：每次工具调用都经过权限检查，每个有实际影响的操作都会持久化，每次运行都以结构化停止原因结束。它是一个真实的工程基线，而不是单文件 Agent Demo。

## 当前状态

**阶段 6（Observability / Trace / Metrics / Diagnostics / Benchmark）已完成。** 当前版本支持：

- 在 OpenAI 兼容流式端点上运行异步 Agent Loop；
- 执行 Shell 命令：超时、输出上限、进程树终止、取消；
- 在执行层分析命令：命令链、管道、重定向、包装器、解释器、危险模式，默认保守；
- 在可用平台上应用真实 OS 沙箱：Linux bwrap、macOS Seatbelt、Windows Job Object（仅进程级，明确标注）；
- 使用 SQLite 持久化会话、消息、事件、工具调用、审批、文件 Claim 和 Token 用量；
- 精确指纹审批：once / session / project 三种作用域；
- API Key 存储在 OS keyring，工具输出、事件、审计记录中的密钥会被脱敏；
- 维护并验证 Hash Chain 安全审计日志；
- 阻止多个会话同时写入同一文件或工作区树；
- 分层上下文压缩（L1 工具输出归档 → L2 确定性摘要 → L3 模型摘要 → L4 Checkpoint 重置），并用 Anchor Ledger 保留目标与约束；
- Checkpoint：不可变、带 checksum 与版本的状态快照，可按数量保留；
- 崩溃恢复：Lease Epoch Fencing、只读 Transcript 对账、合成“状态未知/未自动重试”的工具结果、清理旧 Claim 与孤儿 Shell 进程；
- Session Fork：从历史位置或 Checkpoint 派生独立会话，不继承执行状态与审批；
- 持久化 Task / Plan / Todo：模型生成结构化计划、程序校验依赖/循环、版本化修订、依赖调度；
- 任务执行接入真实 Runtime：独立完成验证、失败重试、阻塞、取消、恢复与副作用保护；
- Hooks：Session/Run/Turn/Model/Tool/Task/Plan/Context/Checkpoint/Recovery 生命周期；进程内观察/否决/上下文扩展与外部命令 Hook；
- MCP：官方协议 Client、stdio 与 Streamable HTTP、工具发现/命名空间/Schema 校验/超时/取消/重连，接入 ToolExecutor 安全链；
- Plugin：Manifest、版本/依赖/权限校验、MCP/Hook/Tool 贡献、显式受信任 Python 入口、CLI 管理；
- Provider Fallback：多 Provider/Model 路由、Retry/Backoff/Jitter/Retry-After、Circuit Breaker、流式安全（partial stream 不重放）；
- Trace：Run/Task/Turn/Model/Provider Retry/Fallback/Tool/MCP/Hook/Compression/Checkpoint/Recovery/Fork Span，父子关系、异步传播、批量持久化、脱敏导出与保留策略；
- Metrics：从既有事件与领域表聚合 Run/Task/Provider/Model/Tool/Context/Recovery/System 指标、P50/P95/P99、Token、可配置价格成本估算、JSON 导出；
- Diagnostics：结构化 `bear doctor`，静态检查与显式 `--active` 连通性检查分离；
- Benchmark：43 个内置场景、Warmup、迭代统计、历史、比较、真实/脚本化 Provider 分离；
- CLI 流式输出文本与工具状态，或输出 NDJSON 给脚本。

暂未实现（见 [ROADMAP.md](docs/ROADMAP.md)）：完整 TUI、子代理、Web UI、独立 Eval 质量套件。

## 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- 任意 OpenAI 兼容 Chat Completions 端点（OpenAI、DeepSeek、Qwen、Ollama、vLLM、中转等）
- 可选：Linux 的 `bubblewrap`、macOS 的 `sandbox-exec`、Windows 10/11（Job Object 进程控制）

## 安装

```bash
git clone https://github.com/huiliyi2025/Bear-Agent.git
cd Bear-Agent
uv sync
```

在仓库内运行 CLI：

```bash
uv run bear --help
```

把 `bear` 命令安装到 PATH：

```bash
uv tool install .
bear --help
```

## 快速开始

```bash
bear init                 # 写入 ~/.config/bear-agent/config.toml
export DEEPSEEK_API_KEY=...
bear doctor               # 结构化诊断配置、存储、Provider、Sandbox、审计、Trace/Metrics
bear doctor --json        # 机器可读诊断报告
bear                      # 进入交互会话
```

单次执行：

```bash
bear run "阅读 README.md，用 5 条要点总结项目"
bear run "运行测试并报告失败" --permission supervised
bear run "..." --json     # 输出 NDJSON，供脚本 / CI 使用
```

会话检查、恢复与分叉：

```bash
bear sessions list
bear sessions show <session-id>
bear sessions checkpoints <session-id>   # 列出并校验 Checkpoint
bear sessions recover <session-id>       # 崩溃后手动恢复
bear sessions fork <session-id> --at 12  # 从第 12 条消息分叉
bear sessions fork <session-id> --checkpoint <checkpoint-id>
```

交互模式下可使用 `/compact`、`/anchor <text>`、`/anchors`、`/fork [message_seq]`。

任务规划与执行：

```bash
bear tasks plan "实现并验证一个功能，包含测试与文档"   # 生成并校验 Plan
bear tasks show <session-id>                          # Plan / Todo / 状态
bear tasks run <session-id>                           # 执行 Plan
bear tasks task <task-id>                             # 任务详情与尝试历史
bear tasks revise <session-id> --file plan.json --reason "..."
bear tasks retry <task-id> [--force]
bear tasks resume <session-id> [--run]
bear tasks cancel <session-id> [--task <task-id>]
```

交互模式下可使用 `/plan <goal>`、`/tasks`、`/run-plan`、`/task <id>`、`/cancel-plan`。

扩展管理：

```bash
bear providers show                 # Primary / Fallback / 能力 / 重试策略
bear providers test                 # 发送最小请求验证路由
BEAR_FALLBACK_MODELS="backup:backup-model,other:model" bear run "..."
bear mcp status                     # 连接已配置 MCP Server
bear mcp tools                      # 列出 MCP 工具
bear plugins list/validate/info ID  # Plugin 管理
bear plugins enable/disable ID
bear hooks list                     # Hook 配置
```

可观测性与 Benchmark：

```bash
bear trace list                     # 最近 Trace
bear trace show <trace-id> --events # Span 树 + 关联事件类型
bear trace run <run-id>             # 按 Run 还原执行路径
bear trace export --trace <id> -o trace.json
bear trace stats                    # Trace/不完整计数
bear trace prune --yes              # 按保留策略删除

bear metrics show                   # Run/Task/Model/Tool/Context/Recovery/System
bear metrics providers              # Provider/Model 分布、Token、成本估算
bear metrics tools                  # Tool 分布、超时、审批、Sandbox、MCP、Hook
bear metrics tasks                  # Task 完成率、重试、Plan 修订
bear metrics recovery               # Recovery/Checkpoint/Fencing
bear metrics export -o metrics.json

bear bench list                     # 43 个内置场景
bear bench run e2e.single_tool      # 运行并保存
bear bench run kernel.trace_write --iterations 5 --trace
bear bench history --scenario e2e.single_tool
bear bench compare <run-a> <run-b>  # 同一场景/参数
bear bench report <run-id> --samples
bear bench export <run-id> -o bench.json
bear bench prune --yes             # 按保留策略清理历史
bear bench run provider.real_request --allow-network  # 真实 Provider，可能产生费用
```

成本估算需要配置价格；没有价格时报告 `pricing_missing`，不会编造费用：

```toml
[observability.pricing."deepseek:deepseek-chat"]
input_per_million = 0.27
output_per_million = 1.10
currency = "USD"
```

`bear doctor --active` 会发送一个最小 Provider 请求，并启动已配置的 MCP Server；默认静态检查不会执行这些操作。Trace 默认只记录元数据，不记录 Prompt/工具参数/工具结果。

不修改配置文件，直接覆盖 Provider：

```bash
bear run "hello" \
  --base-url https://api.deepseek.com/v1 \
  --model deepseek-chat \
  --api-key "$DEEPSEEK_API_KEY"
```

## 配置

配置格式为 TOML。优先级：CLI 参数 > `BEAR_*` 环境变量 > 配置文件 > 内置默认值。

```toml
[agent]
default_provider = "deepseek"
default_model = "deepseek-chat"
max_turns = 30
permission_mode = "auto"   # plan | supervised | auto | unrestricted

[providers.deepseek]
type = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = ["deepseek-chat", "deepseek-reasoner"]

[shell]
enabled = true
default_timeout_s = 120
max_timeout_s = 600
max_output_bytes = 200000
allow_commands = []
allow_prefixes = []
deny_patterns = []

[sandbox]
mode = "require"           # require | process | off
network = false
read_system = true

[secrets]
backend = "keyring"

[audit]
enabled = true

[claims]
enabled = true
ttl_s = 600

[approvals]
session_ttl_s = 86400
project_ttl_s = 2592000
```

常用环境变量：

| 变量 | 含义 |
|---|---|
| `BEAR_CONFIG` | 配置文件路径 |
| `BEAR_PROVIDER` / `BEAR_MODEL` | 覆盖 Provider / 模型 |
| `BEAR_BASE_URL` / `BEAR_API_KEY` | 覆盖默认 Provider |
| `BEAR_DATA_DIR` | 移动 SQLite 数据库与 artifact 目录 |
| `BEAR_PERMISSION_MODE` | `plan`、`supervised`、`auto`、`unrestricted` |

`bear config show` 永远不会输出密钥。优先使用 `api_key_env` 或 `secret://name`，不要在配置文件中直接写明文 Key。

## 命令

```text
bear                         交互式会话
bear run <prompt>            单次无头执行
bear run <prompt> --json     NDJSON 事件流
bear sessions list           最近会话
bear sessions show <id>      会话记录 + 用量
bear config show             当前生效配置（已脱敏）
bear config path             默认配置路径
bear init                    创建初始配置
bear doctor                  结构化诊断（--json/--export/--active/--category）
bear trace ...               Trace 查询/导出/裁剪
bear metrics ...             Metrics 聚合/导出
bear bench ...               Benchmark 运行/历史/比较
bear audit verify            验证审计哈希链
bear audit tail              最近审计记录
bear secret set <name>       把密钥写入 OS keyring
bear secret list             列出密钥名称（不显示值）
bear secret delete <name>    删除密钥
```

交互式 Slash 命令：`/help`、`/tools`、`/metrics`、`/session`、`/permission <mode>`、`/new`、`/exit` 等。

## 权限模式

| 模式 | 读取 | 工作区写入 | Shell 执行 |
|---|---|---|---|
| `plan` | 允许 | 拒绝 | 拒绝 |
| `supervised` | 允许 | 询问 | 询问 |
| `auto`（默认） | 允许 | 允许 | 安全命令允许；有风险命令询问 |
| `unrestricted` | 允许 | 允许 | 允许，但硬拒绝命令仍然拒绝 |

即使在 `unrestricted` 模式，路径边界、敏感文件拒绝、`.git` 写保护和高危命令硬拒绝仍然生效。拒绝规则始终优先。

审批使用“工具 + 参数”的精确指纹：

- **once**：仅本次调用；
- **session**：本会话内相同的精确调用（持久化、会过期）；
- **project**：本项目内相同的精确调用（持久化、会过期）。

一次授权不会扩大为对其他命令的永久授权。

## Shell 与 Sandbox

Shell 执行是一条流水线，而不是 Prompt 约定：

```text
命令策略 -> 审批 -> 文件/树 Claim -> Sandbox -> 有界运行器
```

### Sandbox 模式

| 平台 | 后端 | 文件系统隔离 | 网络控制 |
|---|---|---|---|
| Linux | bubblewrap（`bwrap`） | 是：系统只读、工作区可写、私有 `/tmp` | 默认 `--unshare-net`，可配置开启 |
| macOS | `sandbox-exec` Seatbelt | 是：默认拒绝、工作区/临时目录可写 | 默认拒绝，可配置开启 |
| Windows | Job Object | **否**：仅进程树、进程数、内存限制 | 无 |
| 任意 | `off` | 无（显式关闭） | 无 |

`sandbox.mode`：

- `require`（默认）：必须存在文件系统级 Sandbox，否则 Shell 工具拒绝执行。因此在 Windows 上默认禁用 Shell。
- `process`：接受仅进程级 containment（Windows Job Object），并在结果中明确标注没有文件系统隔离。
- `off`：显式关闭。命令仍然经过命令策略、审批和 Claim，但**没有 OS 隔离**，结果会明确说明。

Windows 上请使用 `mode = "process"` 获得 Job Object 进程控制，或者仅在接受无隔离运行时使用 `mode = "off"`。Job Object 进程以挂起状态创建，加入 Job 后再恢复运行，因此用户代码不会在 containment 生效前执行。

### 命令策略

- 硬拒绝：递归删除根目录（POSIX 与 Windows）、磁盘格式化/裸设备写入、force push、hard reset、force clean、管道到解释器、PowerShell 递归删除、注册表/磁盘工具、fork bomb，以及用户配置的拒绝正则。
- 包装器和解释器（`bash -c`、`env`、`sudo`、`python -c`、PowerShell `-Command`/`-EncodedCommand`、`cmd /c`、`xargs`）会被拆解分析或升级审批。
- 工作区内的安全只读命令可在 `auto` 下直接运行；未知或变更类命令需要询问；工作区外路径在没有文件系统 Sandbox 时询问或拒绝。
- `shell.allow_commands` / `shell.allow_prefixes` 可以显式放行特定命令；硬拒绝仍然优先。
- `shell.deny_patterns` 用于添加自定义硬拒绝正则。

### Claim

文件写工具会 Claim 目标文件；Shell 命令会 Claim 整个工作区树。第二个会话不能写同一文件（或在一个文件 Claim 存在时运行 Shell）；丢失 session lease 的旧进程所持有的 Claim 会被 fencing 拦截。

### 密钥

- 优先使用 `api_key_env` 或 `secret://name` 引用。
- `bear secret set name` 把值写入 OS keyring。
- 工具输出、事件、审计详情和常见凭证格式会被脱敏。
- 没有明文文件密钥回退；plaintext/file 类型的 keyring 后端会被拒绝，keyring 不可用时 `secret://` 引用 fail-closed。

## 数据与隐私

- SQLite 数据库：Linux 位于 `~/.local/share/bear-agent/bear.db`，Windows 位于 `%LOCALAPPDATA%\bear-agent`，可用 `BEAR_DATA_DIR` 覆盖。
- 消息、事件、工具调用元数据、审批、Claim 和用量都保存在本地。
- 不向项目维护者发送遥测。Provider 只会收到完成请求所需的对话内容。
- 不要把凭证放进工作区；路径守卫和脱敏是纵深防御，不是良好卫生习惯的替代品。

威胁模型、Sandbox 边界和当前限制见 [SECURITY.md](docs/SECURITY.md)。Windows 文件系统隔离明确不在本阶段承诺范围内；Job Object 后端仅提供进程级控制。

## 开发

```bash
uv sync
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest
```

架构、添加工具和调试方法见 [DEVELOPMENT.md](docs/DEVELOPMENT.md)。设计基线见 [ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [ROADMAP.md](docs/ROADMAP.md)。

## 项目结构

```text
src/bear_agent/
  core/          事件、消息、取消、用量、共享枚举
  config/        TOML + 环境变量配置
  storage/       SQLite、迁移、Repository
  sessions/      会话记录、转录、租约、文件 Claim
  providers/     模型 Provider 端口 + OpenAI 兼容适配器
  tools/         工具契约、注册表、执行器、内置工具（含 Shell）
  shell/         命令策略、运行器、Sandbox 适配器、环境
  policy/        权限矩阵、路径守卫、审批
  secrets/       keyring 存储、引用解析、脱敏
  audit/         哈希链安全日志
  context/       Prompt、Token 预算、工作上下文装配
  runtime/       Agent Loop、Turn 执行器、停止策略
  observability/ 事件总线 + 记录器
  ui/cli/        Typer/Rich CLI、渲染器、审批提示
```

## 参与贡献

欢迎贡献。请先阅读 [CONTRIBUTING.md](docs/CONTRIBUTING.md)。项目按阶段推进，请保持改动与当前阶段一致，并为修复补充回归测试。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。