# 架构

> 状态：阶段 4（Task / Plan / Todo Engine）。本文描述当前已经实现的能力，以及后续阶段必须遵守的边界。

## 目标

Bear-Agent 是本地优先的 Agent Harness，具备四项不可妥协的属性：

1. **有界执行**——每次运行都有轮次、工具调用数、时间与 Token 预算。
2. **状态持久化**——有实际影响的操作与状态变更都会落盘。
3. **执行层安全**——权限与路径边界由代码强制执行，而不是依赖 Prompt 请求模型遵守。
4. **可替换的边缘**——模型、存储、工具与 UI 都是端口/适配器。

阶段 4 的非目标：托管多租户服务、分布式 Worker、图形化工作流编排器、模型训练。

## 分层

```text
+------------------------------------------------------------------+
| 接口层：CLI / 无头 NDJSON / 未来 HTTP+SSE                         |
+------------------------------------------------------------------+
| 应用层：Container（组合根）、SessionService                       |
+------------------------------------------------------------------+
| 内核层：Runtime - TurnExecutor - Context - Tools - Shell - Policy |
|         Sessions/Claims - Secrets - Audit - Events - Providers     |
+------------------------------------------------------------------+
| 适配器：OpenAI HTTP、SQLite、OS Sandbox、keyring、Rich CLI        |
+------------------------------------------------------------------+
```

依赖规则：

- `core` 不依赖 Harness 内任何其他模块。
- 内核模块依赖 `core` 与端口，不依赖 UI 或具体适配器。
- `bootstrap.py` 是唯一的组合根，负责装配 SQLite、Provider、工具、策略、上下文与运行时。
- `ui/` 依赖应用层/Container；内核永远不 import `ui/`。
- `policy/path_guard.py` 只依赖 `core`；工具通过 `ToolContext` 获取路径守卫，而不是 import policy。

已落地的约束示例：

- `policy.engine` 使用结构化 `ToolLike` 协议，而不是 `tools.base.Tool`，因此 policy 与 tools 不互相依赖。
- `storage.repositories` import 会话记录类型（适配器向内依赖）。
- `sessions.service` 不知道事件总线；需要实时 session-created 事件时，由调用方注入 `publish` 回调。
- `shell` 负责命令分析、进程执行与 Sandbox 适配器；Runtime 只看到 `ToolResult` 与 Sandbox 元数据。
- `secrets` 与 `audit` 是共享安全服务，注入 Executor 与需要审计的组件。
- `sessions/claims.py` 负责文件/树 Claim 协调，不属于模型可见的 Tool API。
- `context/compaction.py` 只读 Transcript，写入 `compactions` 与 artifact；`ContextBuilder` 是唯一把压缩视图变成 Provider 请求的组件。
- `sessions/checkpoints.py`、`sessions/recovery.py`、`sessions/fork.py` 通过 Repository 与 SessionService 工作，不 import Runtime。
- `artifacts/store.py` 只做内容寻址文件 I/O；元数据在 SQLite。
- `core/fencing.py` 的 ambient `LeaseFence` 由 Runtime 设置，Repository 在同一事务内校验。
- `tasks/service.py` 拥有 Plan/Task 状态机与依赖调度；`tasks/engine.py` 只做执行决策，不 import Runtime。
- `runtime/runtime.py` 的 `run_plan` 持有单租约并驱动 TaskEngine；Task 状态更新与执行处于同一 fence。
- `tasks/tools.py` 只调用 TaskService，不能绕过 ToolExecutor/Permission/Claim/Audit。

## Runtime 执行流程

```text
CLI / run 命令
  -> Container
  -> AgentRuntime.run(session_id, input)
       获取进程内锁
       获取 SQLite 会话租约（owner + epoch + expiry）
       设置 context-local LeaseFence
       启动租约心跳
       如存在崩溃中间态：RecoveryService.recover_if_needed
         （校验审计链、校验 Checkpoint、合成 unknown 工具结果、清理旧 Claim/进程）
       追加 user message + message.user 事件（同一事务）
       循环：
         检查取消 / 墙钟 / 轮次预算
         TurnExecutor.execute:
           加载转录历史
           ContextCompactor.prepare（L1-L4、Anchor Ledger；只读 Transcript）
           ContextBuilder.build（system + anchors + 压缩视图 + 预算）
           ModelCaller.stream（Provider 事件 -> assistant message + usage）
           持久化 assistant message + model.response 事件
           如果有工具调用：
             对每个调用执行 ToolExecutor：
               持久化 tool_call running（参数已脱敏）
               校验参数
               通用权限 + 工具 preflight（例如命令策略）
               审计策略决策
               可选审批（once/session/project，持久化）
               获取文件/树 Claim + lease-owner fencing
               审计 Sandbox 决策
               带超时执行（Sandbox 包装后的 Shell / 文件工具）
               脱敏结果，持久化完成状态 + 审计
             持久化工具结果消息
         每 N 个 turn 创建 Checkpoint
         StopPolicy.evaluate
       finally:
         释放 Claim
         将中断的 running 工具标记为 unknown（fenced）
         会话置为 idle（fenced）
         持久化 run.completed / run.cancelled / run.failed（fenced）
         释放租约
         清除 LeaseFence
```

关键不变量：

- 一条可见消息一定有对应事件（同一事务）。
- 工具调用在执行前标记 `running`，执行后进入终态。
- 崩溃后 `running` 工具会变成 `unknown`，绝不静默重放。
- assistant 的工具调用消息先于工具副作用持久化。
- Run 以结构化 `RunStatus` 结束，不会留下含糊的静默状态。

## 模块

| 模块 | 职责 | 不负责 |
|---|---|---|
| `core` | 消息分段、事件、取消、错误、用量、模式 | I/O |
| `config` | TOML + 环境变量 + CLI 覆盖合并 | 启动 Agent |
| `storage` | SQLite Schema、事务、Repository | 业务决策 |
| `sessions` | 会话记录、转录、租约、Checkpoint、恢复、Fork | 模型调用 |
| `tasks` | Plan/Task/Todo 模型、状态机、依赖调度、规划、验证、执行决策 | Tool/权限执行 |
| `shell/process_registry` | 记录与终止运行中的 Shell 进程 | 命令策略 |
| `providers` | Provider 端口、OpenAI 兼容适配器、重试分类 | 工具执行 |
| `tools` | 工具契约、注册表、参数校验、执行器、内置工具 | 权限策略 |
| `policy` | 权限矩阵、路径边界、敏感文件、审批 | 执行工具 |
| `context` | 稳定 System Prompt、Token 预算、历史分组、分层压缩、Anchor Ledger | 修改/删除 Transcript |
| `artifacts` | 内容寻址 artifact 写入、读取校验 | 压缩策略 |
| `runtime` | Run/Turn 编排、模型结果聚合、停止规则、Checkpoint 触发 | Provider 私有 wire 逻辑 |
| `shell` | 命令策略、有界进程运行器、Sandbox 适配器 | Agent Loop 决策 |
| `secrets` | keyring 存储、密钥引用、脱敏 | 持久化明文密钥 |
| `audit` | 哈希链安全记录与验证 | 通用会话 Trace |
| `sessions/claims` | 文件/树 Claim 租约与 fencing | 模型可见的 Tool API |
| `observability` | 事件总线、记录器、持久化/瞬态事件策略 | 改变运行行为 |
| `ui/cli` | Typer 命令、Rich 渲染、审批提示 | Agent Loop 逻辑 |

## 消息模型

内部消息与 Provider 无关：

```python
Message(role, parts=[TextPart | ToolCallPart | ToolResultPart])
```

OpenAI 适配器负责转换成 `chat/completions` payload。接入其他 Provider 只需增加适配器，不修改 Runtime。工具调用参数以流式片段到达，结束后统一校验；参数不完整时返回 `arguments_error`，执行器拒绝运行。

## 事件

事件是会话的可观测事实。事实事件按会话单调递增的 `seq` 持久化；流式文本/思考、工具进度等 delta 只实时发布，不落盘。

```text
session.created
run.started / run.completed / run.cancelled / run.failed
turn.started / turn.completed
message.user / message.assistant / message.tool
model.request / model.response / model.retry
tool.call.started / tool.call.completed / tool.call.failed
policy.decision / approval.requested / approval.decided
sandbox.decision / claim.acquired / claim.conflict
context.built / context.truncated
context.compacted / context.compaction.failed / context.reset
context.anchor.added / context.anchor.removed
checkpoint.created / checkpoint.invalid / checkpoint.pruned
recovery.started / recovery.completed / recovery.failed
recovery.tool.reconciled / recovery.process.terminated / recovery.claims.released
session.forked
task.plan.created / task.plan.revised / task.plan.completed / task.plan.blocked
task.plan.failed / task.plan.cancelled
task.started / task.attempt.started / task.attempt.finished
task.completion.requested / task.verification
task.completed / task.failed / task.blocked / task.cancelled / task.retry.scheduled
task.run.started / task.run.completed / task.run.failed / task.reconciled
model.text.delta / model.thinking.delta / tool.started / tool.finished
```

事件总线隔离订阅者：某个 UI/遥测处理器失败不会打断 Run。

## 持久化

SQLite（WAL）是状态真源。截至阶段 3 的表：

- `schema_meta`——迁移版本。
- `sessions`——cwd、标题、状态、模型/Provider、`last_seq`、租约字段。
- `events`——append-only 事实事件。
- `messages`——内部消息 JSON，按会话排序。
- `tool_calls`——running/completed/failed/unknown，含参数与结果摘要。
- `model_calls`——Token 用量与延迟。
- `artifacts`——内容寻址的大输出/摘要，含 kind 与元数据。
- `approvals`——精确指纹、带作用域和过期时间的授权。
- `file_claims`——文件/树 Claim，含 lease owner 与过期时间。
- `secret_names`——仅名称；值存放在 OS keyring。
- `audit_log`——append-only 记录，含 `prev_hash`/`hash`。
- `checkpoints`——不可变状态快照、checksum、schema version。
- `compactions`——L1-L4 压缩记录、覆盖消息范围、before/after token、artifact。
- `context_anchors`——跨压缩保留的关键事实。
- `context_state`——generation、最近压缩、L3 失败熔断状态。
- `process_records`——运行中的 Shell 进程（pid/pgid/owner），用于崩溃恢复。
- `sessions.parent_session_id` / `fork_message_seq` / `fork_checkpoint_id`——Fork 来源。
- `plans`——每个 Session 当前 Plan、goal、status、version。
- `plan_revisions`——Plan 版本快照、修订原因/来源。
- `tasks`——Task 定义、状态、尝试计数、验收标准、结果。
- `task_dependencies`——Task 依赖边。
- `task_attempts`——每次执行的 attempt、完成请求/验证、错误与结果。

所有多行变更都使用显式 `BEGIN IMMEDIATE` 事务。消息行与其消息事件一起写入；工具调用行通过 Repository 事务更新。所有会话状态写入默认读取 ambient LeaseFence，并在同一事务中校验 owner/epoch/过期时间。

## 上下文压缩

压缩是 Transcript 之上的只读视图：

```text
messages (immutable)
  + compactions (ordered, immutable; L2/L3 may supersede older summaries)
  + anchors (mutable, pinned)
  -> apply_compactions()
  -> pinned system messages + recent turns
  -> ContextBuilder budget/selection
```

L1 把旧工具输出写入 artifact 并替换为预览；L2 生成确定性摘要；L3 调用模型并在失败时回退 L2；L4 先创建 Checkpoint，再重置旧历史。原始消息行永不修改，每次有损重写都有 compaction 行与 artifact 引用。模型可通过 `read_artifact` 按 id 读回被归档的输出；该工具只允许读取当前 Session 的 artifact，并在读取时校验 SHA-256。

## Checkpoint 与恢复

Checkpoint 保存指针（event/message 序号）与无法从 Transcript 推导的状态（context generation、anchors、工具快照）。checksum 覆盖 session、kind、序号、状态与 schema version。损坏或不兼容的 Checkpoint 会被忽略并记录事件，Transcript 仍是事实来源。

恢复只修复可安全修复的中间态：尾部的悬空 tool_call 会得到明确的 “unknown / 未自动重试” 合成结果；非幂等工具不会自动重放；旧 Claim 释放；遗留 Shell 进程经身份校验后终止。审计链损坏时 fail-closed。

## Task / Plan / Todo

Plan 是 Session 当前的结构化目标；Task 是带依赖、验收标准和状态的执行节点；Todo 是 Task 的只读投影。Plan 修订写入版本化快照，Task 保持稳定 ID，因此执行历史不因修订复制。

调度由程序完成：`claim_next` 在事务内选择依赖已完成、pending、active 的最高优先级 Task，转为 in_progress 并创建 attempt；同一 Session 同时只有一个 in_progress Task。运行结果由 `TaskEngine` 结合 `task_complete` 请求、evidence 或独立模型验证决定 completed/failed/blocked，模型不能直接写状态。

`AgentRuntime.run_plan` 在一次持续 Lease 下循环：领取 Task → 用现有 turn/tool 路径执行 → 验证 → 更新状态 → 下一个 Task。每轮固定注入 Plan/Todo/当前 Task，压缩不会移除。执行过非幂等工具的失败 Task 不会自动重试；崩溃中的 running attempt 由 Recovery 标记 unknown、Task 标记 blocked。

## Session Fork

Fork 在单个事务中复制 `1..cut` 消息与活跃 anchors，创建新 Session ID，并记录 lineage。安全切点会回退到不完整 tool_call 之前。执行状态（tool_calls、model_calls、claims、approvals、processes、checkpoints、compactions）不继承；新会话必须重新获取 Lease/Claim 与审批。

## 租约、Claim 与并发

- 每个会话同时只有一个活跃 Run：进程内 `asyncio.Lock` + SQLite 租约。
- 租约 owner 为 `pid-<pid>-<run_id>`，包含 epoch、过期时间和心跳。
- 第二个 Run 会快速失败并返回“already running”，不会交错执行。
- 文件写入获取文件 Claim；Shell 执行获取整个工作区的树 Claim。Claim 记录 lease owner 并执行 fencing：租约被接管的进程不能获取或校验 Claim。
- Claim 会过期，由 Run 心跳续租，并在 Run 的 finally 中释放。崩溃的 Run 留下的 Claim 会在过期/租约失效前阻塞。
- SQLite `BEGIN IMMEDIATE` 在跨进程范围内串行化 Claim 获取与审计追加。这是单机并发；分布式 Server 不在范围内。

## 上下文管理（P1 基线；压缩属于 P3）

ContextBuilder 保持 System Prompt 与项目指令稳定，并按用户回合分组历史，保证 assistant 工具调用消息不会与工具结果分离。超预算时优先丢弃最旧的完整回合；只有最新回合的工具结果允许截断。用户当前请求本身绝不会被静默截断。

推迟到阶段 3：LLM 摘要、锚点账本、artifact 恢复、感知前缀缓存的压缩、Checkpoint/会话分裂。

## 安全（阶段 2 范围）

- 权限模式与执行层审批（once/session/project）。
- `PathGuard` 路径边界、敏感文件拒绝、`.git` 写拒绝。
- Tool preflight：Shell 命令在执行/审批前分析。
- Shell 命令策略：硬拒绝、包装器/解释器拆解、重定向/管道/命令链分析、allow/deny 配置。
- Sandbox 适配器：
  - Linux bwrap：系统只读、工作区可写、私有 `/tmp`、敏感文件 mask、可选网络 unshare；
  - macOS Seatbelt：默认拒绝的 profile、工作区/临时目录可写、敏感文件读取拒绝；
  - Windows Job Object：仅进程树/进程数/内存 containment，没有文件系统隔离；
  - `require` 在没有文件系统后端时 fail-closed；`process` 显式接受仅进程级 containment；`off` 是显式 opt-out。
- 密钥脱敏与 keyring 密钥；keyring 不可用时 `secret://` 引用 fail-closed。
- 哈希链审计日志与验证，覆盖策略、审批、Sandbox、Claim 与执行决策。
- 文件/树 Claim 与 lease-owner fencing。

仍推迟：MCP 信任、Prompt Injection 污点追踪、加密数据库、外部审计锚点、Windows 文件系统隔离。详见 `SECURITY.md`。

## UI 边界

CLI 订阅事件总线；实时执行不直接读数据库，Runtime 也从不调用 `rich`/`typer`。无头 `--json` 输出 NDJSON，与未来 HTTP/SSE 表面使用同一套事件词汇。

## 测试

- 单元：配置、消息、事件、权限矩阵、路径守卫、上下文预算、Provider SSE 解析/重试、命令策略矩阵、Sandbox 构造、审批、Claim、审计链、密钥/脱敏。
- 集成：真实 SQLite + 脚本化 Provider + 真实 Shell Runner，覆盖工具调用、命令拒绝、超时、取消、输出上限、Sandbox 模式、脱敏、Claim 与恢复。
- Sandbox 执行：Linux 可用时运行真实 bwrap 测试；Windows 运行 Job Object 测试；macOS 运行 Seatbelt profile 测试（实际执行依赖环境）。
- E2E：Typer CLI + 脚本化 Provider、NDJSON、Session 列表/查看、Audit verify/tail、Secret 命令。

任何测试都不依赖真实模型或网络。