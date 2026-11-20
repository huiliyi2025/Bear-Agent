# 变更记录

记录所有重要变更。项目按阶段路线图推进；公开版本发布后使用语义化版本。

## [0.6.0] — 2026-11-20 — 阶段 6：Observability / Trace / Metrics / Benchmark

### 新增

- Schema v6：`traces`、`spans`、`benchmark_runs`；事件继续使用已有 `span_id` 关联 Trace，不复制 EventBus/Audit/Model Call/Tool Call 事实。
- TraceService：根 Trace/Span、父子关系、状态（running/ok/error/cancelled/incomplete）、ContextVar 异步传播、批量写入、错误隔离、脱敏与属性上限、导出、保留裁剪、崩溃后不完整识别。
- Trace 接入 Session/Run/Turn/Task/Plan Revision/Model/Provider Retry/Fallback/Tool/MCP/Hook/Context Compression/Checkpoint/Recovery/Fork；`bear trace list/show/run/stats/export/prune`。
- MetricsService：按时间窗口/Session 聚合 events、model_calls、tool_calls、tasks、attempts、plans、plan_revisions、compactions、context_anchors、checkpoints、traces。
- Runtime/Task/Provider/Model/Tool/Context/Recovery/System 指标：成功率、P50/P95/P99、Token、缓存 Token、Usage 缺失、可配置价格成本估算、审批等待、权限拒绝、Sandbox、Hook、MCP 调用/配置、Plugin 配置、Circuit、Retry/Fallback、Fencing/Claim/未知工具状态；MCP/Plugin 生命周期历史明确标记不可用。
- 进程内 Counter/Gauge/Histogram Registry 与 LiveMetricsCollector；标签键/值/序列数上限与 `_overflow` 折叠。
- DiagnosticsService：结构化 PASS/WARN/FAIL/SKIP、静态/活动模式、配置/Schema/存储/SecretStore/Provider/MCP/Plugin/Hook/Sandbox/Audit/Checkpoint/Recovery/Trace/Metrics/Benchmark/保留策略/环境检查；`bear doctor --json --export --active --category`。
- Benchmark Framework：Scenario Registry、Warmup、迭代、独立临时 workspace/data dir、Mean/Median/P50/P95/P99/Min/Max/Stdev、Throughput、Error Rate、Token、Cost、结构化结果、Run ID、历史、比较、JSON 导出。
- 43 个内置 Benchmark 场景：Kernel、Tool、Context、Task、Provider、MCP、E2E；真实 stdio MCP、真实 Runtime/ToolExecutor/TaskEngine、Plugin Hook；真实 Provider 与 ScriptedProvider 分离。
- CLI `bear bench list/run/report/history/compare/export/prune`、`bear metrics show/providers/tools/tasks/recovery/export`。
- 配置 `[observability]`（采样、批量、保留、价格）与 `[benchmarks]`；`config_version`；`config show` 输出新配置。
- 新增 ADR 0021（Trace/Metrics/Diagnostics 边界）与 ADR 0022（Benchmark Framework）。

### 安全

- Trace 不记录 Prompt、模型输出、工具参数/结果；导出事件移除 content/arguments/summary/evidence 等内容字段。
- Trace/Metrics 写入或查询失败不影响 Run、Tool、Approval、Claim、Audit、Sandbox、Recovery 决策。
- Diagnostics 默认只做静态检查；`--active` 才调用 Provider/启动 MCP，并明确标记。
- Benchmark 不为提速降低安全设置；Sandbox/Shell 不可用即 SKIP；真实 Provider 需显式 `--allow-network`；结果脱敏。
- 修复外部 Hook/MCP 安全 cwd 目录不存在导致 WinError 267 的问题；`ensure_data_dirs` 现在创建 `hooks`/`mcp` 安全目录。

### 已知限制

- Trace 属性由调用点约定；Metrics 查询为按需聚合（20 万事件行上限）。
- 成本为配置估算，不是账单；Fencing 拒绝计数进程内。
- Benchmark 是单机短样本；真实 Provider 受网络/模型版本/价格影响。
- macOS Seatbelt 与真实第三方 Provider 未在本机验证；CI 多平台结果单独说明。

## [0.5.0] — 2026-11-20 — 阶段 5：Hooks / MCP / Plugins / Provider Fallback

### 新增

- Hooks 生命周期：session/run/turn/model/tool/task/plan/context/checkpoint/recovery/terminal 事件；进程内 Pre/Post 与观察 Hook。
- Hook 执行语义：优先级顺序、超时、取消、fail-open/fail-closed、递归/深度防护；`tool_before` 只能额外拒绝，`model_before` 只能追加有界脱敏上下文。
- 外部命令 Hook：`create_subprocess_exec`（无 shell）、最小环境、有界 stdout、进程树终止、严格 JSON 输出；默认关闭，需 `hooks.allow_external=true`。
- Hook 事件、审计、CLI `hooks list`。
- MCP Client：官方 `mcp>=2.3,<3`；stdio 与 Streamable HTTP；初始化/能力/工具发现/调用/超时/取消/重连/停止；多 Server 命名空间与冲突检测。
- MCP Tools 接入 ToolRegistry/ToolExecutor：原始 JSON Schema、jsonschema 参数校验、EXEC 审批、结果转换/脱敏/截断、不可信内容边界；required/optional Server 策略。
- Plugin Framework：`plugin.toml` Manifest、版本/`requires_bear`/依赖/权限/冲突校验；声明式 MCP Server 与外部 Hook；可选受信任 Python 入口（双重显式开关）；状态文件启停；CLI `plugins list/info/validate/enable/disable`。
- Provider Router：有序 Primary/Fallback 路由、错误分类、Retry-After/backoff/jitter、Retry Budget、能力兼容、Circuit Breaker、half-open 恢复。
- 流式安全：只有尚未产生任何事件时才重试/切换；partial stream 返回结构化诊断，绝不重放已输出文本或已执行 Tool Call。
- Provider 实际 route 记录到 ModelResponse/model_calls/事件；CLI `providers show`/`test`；`BEAR_FALLBACK_MODELS`。
- Container 异步 `startup`：加载 Plugin、合并 MCP Server、连接并注册工具；CLI run/chat/task 与 `mcp status/tools` 接入。
- 配置 `[hooks]`、`[mcp]`、`[plugins]` 与 Provider fallback/circuit 字段。

### 安全

- Hook 不能 allow/修改工具参数/绕过审批；外部 Hook 明确为用户权限进程。
- MCP Server 必须显式 trusted；stdio 不经 shell；HTTP 仅 https（loopback 例外）；参数校验在本地完成；结果视为不可信。
- Plugin Python 默认关闭，需 `plugins.allow_python` + `permissions.python`；无沙箱风险已文档化。
- Provider 密钥注册到全局脱敏器；事件/状态不输出密钥/headers；partial stream 不重复副作用。
- 所有扩展工具仍经过 PermissionEngine/Approval/Claim/Audit/Sandbox/Lease。

### 已知限制

- Python Plugin 与外部 Hook 在进程/用户权限下运行，不是沙箱。
- MCP Server 子进程不受 Bear Shell Sandbox 约束、无文件 Claim；Recovery 不跟踪。
- Circuit Breaker 为进程内状态；重启后重置。
- 跨 Provider 能力差异仅支持 OpenAI-compatible 语义；非兼容 Provider 需 adapter。
- partial stream 无法透明恢复，只能保留诊断。
- 真实网络 Provider 未验证；使用 respx/本地 MCP Server 验证。

## [0.4.0] — 2026-11-20 — 阶段 4：Task / Plan / Todo Engine

### 新增

- Plan / Task / Todo 数据模型：Plan 版本化、Task 稳定身份、Todo 只读投影。
- 完整 Task 状态机与显式状态转移表：pending / in_progress / completed / failed / blocked / cancelled / skipped。
- 依赖表、拓扑调度、优先级排序、循环依赖/未知依赖/自依赖校验；同一 Session 同时只有一个 in_progress Task。
- Plan 生成：模型输出严格 JSON，程序校验结构、依赖与环；有界修复调用；失败可行动报错。
- Plan 修订：按 task_key 合并、版本快照、修订上限；已完成/失败/阻塞/取消/移除任务有明确策略；拒绝在有任务执行中修订。
- Task Engine：原子领取任务、attempt 记录、run 结果处理、独立完成验证（evidence 或模型）、失败重试/阻塞/取消。
- 非幂等副作用保护：执行过非幂等工具的失败任务不会自动重试。
- Runtime `run_plan`：整个 Plan 共用 Session Lease/Epoch 与心跳，复用 ToolExecutor、PermissionEngine、Claim、Audit、Sandbox、Checkpoint 与压缩。
- 每轮 pinned Task/Plan/Todo 上下文；压缩与 L4 不会丢失当前任务方向。
- Task 工具：`task_complete`、`task_block`、`plan_revise`，只通过 ToolExecutor 执行。
- Checkpoint 增加 tasks 快照；Recovery 将 running attempt 标记 unknown、Task 标记 blocked，不自动重放。
- Session Fork 不继承 Plan/Task/Attempt，避免不安全完成状态继承。
- CLI：`tasks plan/show/run/task/revise/cancel/retry/resume`；交互 `/plan`、`/tasks`、`/run-plan`、`/task`、`/cancel-plan`。
- 配置 `[tasks]`：任务数、尝试次数、修订次数、每轮任务预算、每任务轮次、验证开关与模型、上下文上限。
- Schema v5：plans、plan_revisions、tasks、task_dependencies、task_attempts。

### 安全与可靠性

- 所有 Task 状态写入在事务内校验 Session Lease owner/epoch/expiry。
- 完成必须经过独立验证或证据门槛；模型不能直接写状态。
- 崩溃恢复不自动重放任务；失败后执行过非幂等工具的任务要求显式重试。
- Task 事件与审计覆盖创建、修订、开始、完成、失败、阻塞、取消、重试、验证与恢复。
- 模型生成的 Plan/修订经过程序结构、依赖与循环校验。

### 已知限制

- 模型验证仍可能误判或受到 Prompt Injection；evidence 模式不判断语义质量。
- 自动重试只对未执行非幂等工具的任务开放；其余需要用户介入。
- Plan 执行期间长期持有 Session 租约。
- Fork 不继承任务计划，需要重新规划。
- 每次 Task run 是一次独立的模型对话轮次序列，Transcript 会累积任务 prompt。

## [0.3.0] — 2026-11-19 — 阶段 3：上下文压缩、Checkpoint、恢复与 Fork

### 新增

- 分层上下文压缩：L1 工具输出归档、L2 确定性摘要、L3 模型摘要（失败回退 + 熔断）、L4 Checkpoint 重置；压缩只读 Transcript，原始消息永不修改。
- Anchor Ledger：自动保留首个目标与约束，支持手工锚点，压缩与 L4 重置后仍注入。
- 内容寻址 ArtifactStore：原子写入、SHA-256 读取校验、压缩产物与工具输出归档。
- `read_artifact` 工具：模型可按 artifact id 读回被 L1 归档的完整输出；按 session 隔离，并校验文件完整性。
- Schema v3/v4：checkpoints、compactions、context_anchors、context_state、process_records、Session Fork 字段、artifact kind/metadata。
- Checkpoint：单事务快照、checksum、schema version、边界校验、保留策略、损坏/不兼容诊断。
- Lease Epoch Fencing：Transcript、Event、tool_calls、model_calls、session 状态、Checkpoint、Compaction、Anchor、Process 写入在事务内校验 owner/epoch/expiry；旧 owner 无法污染状态。
- Session Recovery：启动前恢复、审计链 fail-closed、尾部悬空 tool_call 合成 “unknown/未自动重试”、旧 Claim 清理、孤儿 Shell 进程身份校验后终止、不可修复状态阻塞。
- Session Fork：从消息位置或 Checkpoint 派生独立会话、安全切点、lineage、Anchors 继承、执行状态/审批/Claim/进程隔离。
- Runtime：Recovery 接入、逐 Turn Checkpoint、RecoveryError/SessionLeaseError 结构化失败、cleanup 写入顺序修正（释放租约最后）。
- Shell：进程登记与退出标记，命令预览脱敏。
- CLI：`sessions fork/recover/checkpoints`；交互 `/compact`、`/anchor`、`/anchors`、`/fork`。
- 配置：`[context]`、`[checkpoints]`、`[recovery]`。

### 安全与可靠性

- 新增写入全部 fenced；ContextVar ambient fence 阻止被接管的后台任务继续写。
- Checkpoint 状态、Anchor、artifact、进程命令预览、压缩摘要均经过 SecretRedactor。
- Recovery 不盲目把 `running` 改成 `failed`，不自动重放有副作用工具。
- 审计链损坏时恢复 fail-closed；Recovery 事件与审计覆盖所有自动动作。
- Session Fork 不复制 tool_calls、model_calls、claims、approvals、processes、checkpoints、compactions；project 作用域审批按既有 project ID 语义仍适用。

### 已知限制

- 只自动修复 Transcript 尾部的悬空 tool_call；中间乱序会阻塞并给出人工路径。
- 孤儿进程终止无法验证身份时选择不杀。
- L3 摘要调用未计入 `model_calls` 用量。
- Checkpoint/artifact 无静态加密、外部锚点与自动 GC。
- Token 估算为启发式；L3 摘要质量依赖模型。

## [0.2.0] — 2026-11-19 — 阶段 2：Shell 执行与安全

### 新增

- `bash`/平台 Shell 工具：超时、有界输出、取消、进程树终止、Sandbox 元数据。
- 执行层命令策略：硬拒绝、包装器/解释器拆解、管道、重定向、编码 PowerShell、POSIX/Windows 危险模式、allow/deny 配置、`Tool.preflight` Hook。
- Sandbox 适配器：Linux bwrap、macOS Seatbelt、Windows Job Object；`require`/`process`/`off` 模式，fail-closed 选择与如实的 capabilities 报告。
- 审批作用域（once/session/project）、精确指纹、持久化、过期、CLI 提示。
- keyring `SecretManager`、`secret://`/`env:` 引用，以及工具输出/事件/审计详情/持久化参数的集中脱敏。
- 哈希链审计日志、`bear audit verify/tail`、篡改检测。
- 文件/树 Claim、lease-owner fencing、过期、心跳续租、跨会话冲突处理。
- Schema v2：approvals、file claims、secret names。
- CLI：`audit`、`secret`、扩展 `doctor`；config/init 覆盖 shell、sandbox、secrets、audit、claims、approvals。
- P2 测试：策略矩阵、Sandbox 构造/执行、Windows Job Object、Claim、审批、审计、密钥/脱敏、Shell Runtime 与 CLI。

### 安全 Hardening 审查（2026-11-19）

- 硬拒绝规则增加 token 级语义检查（引号、转义、变量形式、包装器、busybox/toybox、非相邻 pipe-to-interpreter）。
- Windows Job Object 以挂起状态 spawn，加入 Job 后 resume（关闭 assign 前的执行窗口）。
- 文件 Claim 在写入前再次检查；Claim 要求有效 session lease；Windows 路径大小写归一化。
- 审计哈希包含 session ID 并使用无歧义 JSON；resource 脱敏。Hardening 之前 P2 构建生成的审计链使用旧格式，变更后无法验证（仅未发布数据）。
- 拒绝 plaintext/file keyring 后端；config JSON 不输出 shell.env/headers；CLI Runtime 错误脱敏。
- 阻断硬链接敏感别名与非普通文件；Sandbox mask 工作区硬链接文件；敏感 mask 溢出时 fail-closed。
- 直接 Shell/文件工具调用需要 executor 授权标记。
- 每个发现均补充回归测试。

### 已知限制

- Windows Sandbox 仅进程级（Job Object）；`require` 默认禁用 Shell，`process` 明确标注没有文件系统隔离。
- 本地审计链没有外部锚点；有数据库写权限者可重算。
- 网络控制是 Sandbox 级，不是逐命令。
- MCP 信任与 Prompt Injection 污点追踪仍属后续阶段。

## [0.1.0] — 2026-11-19 — 阶段 1：内核基础

### 新增

- 异步 Agent Runtime：有界轮次、结构化停止原因、取消、会话锁、SQLite 租约。
- Provider 中立的消息模型与事件词汇。
- OpenAI 兼容流式/非流式 Provider 适配器、SSE 工具调用装配、用量归一化、重试分类。
- SQLite 持久化：会话、事件、消息、工具调用、模型调用、Schema 迁移。
- Session 创建/列表/查看/恢复与中断工具恢复。
- Tool 契约、注册表、执行器：参数校验、权限决策、审批、超时、结果截断。
- 内置工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`glob`、`grep`。
- 权限模式（`plan`、`supervised`、`auto`、`unrestricted`）与 `PathGuard`（穿越、符号链接、敏感文件、`.git`）。
- ContextBuilder：稳定 system/project Prompt、Token 估算、按 Turn 分组预算装配。
- CLI：`bear`、`run`、`sessions`、`config`、`init`、`doctor`、`--json`、流式渲染器、审批提示。
- 单元、集成、安全与 CLI E2E 测试；ruff、mypy、pytest CI。

### 已知限制

- 无 Shell 工具、OS Sandbox、密钥存储、审计链、跨会话文件 Claim（阶段 2）。
- 无压缩、锚点账本、artifact、Checkpoint（阶段 3）。
- 无 Task/Plan/Todo 引擎（阶段 4）。
- 无 Hooks、MCP、插件（阶段 5）。
- Provider 不返回流式 usage 时 Token 用量可能为 0。
- TUI 是流式 CLI，不是全屏应用。