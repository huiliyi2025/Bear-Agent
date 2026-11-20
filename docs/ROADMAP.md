# 开发路线图

按阶段交付。一个阶段只有在验收检查通过、测试全绿、文档与实际一致时才算完成。

## 阶段状态

| 阶段 | 范围 | 状态 |
|---|---|---|
| P0 | 调研与架构基线 | 已完成 |
| P1 | 内核基础 | **已完成** |
| P2 | Shell 执行与执行安全 | **已完成** |
| P3 | 上下文压缩 + Checkpoint + 恢复 | **已完成** |
| P4 | 持久化 Task / Plan / Todo 引擎 | **已完成** |
| P5 | Hooks、插件、MCP、Provider Fallback | 下一阶段 |
| P6 | 可观测性、Trace、评估 | 计划中 |
| P7 | TUI、打包、发布工程 | 计划中 |
| P8 | Server/Web UI、多会话、子代理 | 可选 |

---

## P0——调研与架构（已完成）

- 深入研究 Tianshu-harness（v3.37.4）源码及更广泛的 Harness 设计。
- 确定产品定位、模块边界、数据流、安全模型与路线图。
- 产出：设计基线（本仓库的 docs/ADR）。

## P1——内核基础（已完成）

### 已交付

- Python 3.13 / uv 项目，严格 mypy 与 ruff 配置。
- 配置：TOML + `BEAR_*` 环境变量 + CLI 覆盖，`config show` 脱敏。
- Core：Provider 中立的消息、事件、取消、错误、用量。
- SQLite 存储：迁移、会话、事件、消息、工具调用、模型调用、租约。
- Session：创建/列表/查看/恢复、append-only 转录、带 epoch 与心跳的租约、 中断工具恢复。
- Provider：OpenAI 兼容流式/非流式适配器、SSE 工具调用装配、用量归一化、重试分类、有界重试。
- Tool：契约 + 注册表 + 执行器，含 Schema 校验、权限检查、审批、超时、截断、审计事件。
- 内置工具：`read_file`、`write_file`、`edit_file`、`list_dir`、`glob`、`grep`。
- Policy：plan/supervised/auto/unrestricted 矩阵；`PathGuard` 防符号链接、穿越、敏感文件与 `.git`。
- Context：稳定 System Prompt + 项目指令、Token 估算、按轮分组预算装配。
- Runtime：有界 Turn 循环、停止条件（最终答复、最大轮次、最大工具调用、无进展、超时、取消）、结构化 Run 结果。
- CLI：`bear`、`run`、`sessions`、`config`、`init`、`doctor`、`--json`、流式渲染、审批提示。
- 测试：单元、集成、安全、CLI E2E；不需要真实网络/模型。
- 文档：README、ARCHITECTURE、ROADMAP、DEVELOPMENT、SECURITY、CONTRIBUTING、ADR。
- CI：lint、格式、类型检查、测试。

### 验收证据

- `uv run pytest` 通过（单元 + 集成 + 安全 + CLI E2E）。
- `uv run ruff check src tests` 与 `ruff format --check` 通过。
- `uv run mypy src` 在严格设置下通过。
- 脚本化 Provider 可以执行工具调用、持久化转录，并在第二个进程中恢复。
- Run 取消有集成测试覆盖；另一场景验证中断的 `running` 工具会标记为 `unknown`，不会重放。
- 路径逃逸、符号链接逃逸、敏感文件、supervised 拒绝测试通过。

### 已知限制（设计上推迟）

- ~~无 Shell 工具 / OS Sandbox~~——已在 P2 交付。
- 无上下文摘要、锚点账本、Checkpoint 文件（P3）。
- 无 Task/Plan/Todo 引擎；Loop 仍以单次请求为中心（P4）。
- 无 Hooks/MCP/插件（P5）。
- 无 Trace UI / Eval 套件（P6）。
- TUI 是流式 CLI，不是全屏应用（P7）。
- Provider 在流式模式下不返回 usage 时，用量可能为 0。

## P2——Shell 执行与执行安全（已完成）

### 范围

- **P2.1 Shell 工具**：`bash`/平台 Shell 工具、工作目录、净化环境、stdout/stderr、退出码、超时、进程树终止、输出上限、取消、状态/事件持久化、清晰错误分类。
- **P2.2 命令策略**：执行层 allow/ask/deny 分析：命令链、管道、重定向、包装器、解释器/间接执行、危险模式、allow/deny 配置，默认保守。
- **P2.3 OS Sandbox**：统一 `Sandbox` 适配器；Linux bwrap、macOS Seatbelt、Windows Job Object（仅进程级 containment）。默认要求文件系统隔离；不可用平台 fail-closed。`sandbox.mode=off` 是显式且文档化的 opt-out，绝不宣称是 sandbox。
- **P2.4 审批作用域**：once/session/project、精确指纹持久化、过期、恢复、事件 + 审计。
- **P2.5 SecretStore + 脱敏**：keyring 存储、`secret://`/`env:` 引用、工具输出/事件/审计/日志脱敏、不可用存储处理。
- **P2.6 Hash Chain 审计日志**：append-only 安全记录、验证、损坏检测、CLI 验证；说明本地哈希链不能防御的场景。
- **P2.7 跨 Session 文件 Claim**：文件/树 Claim、owner + lease-owner fencing、过期/续租/释放、冲突错误、Runtime 清理；`bash` 持有工作区树 Claim，文件工具持有文件 Claim。
- MCP 配置/发现有意推迟到 P5。

### 已交付

- Schema v2：`approvals`、`file_claims`、`secret_names`。
- keyring `SecretManager`、`secret://`/`env:` 引用、集中式 `SecretRedactor`（工具输出、事件、审计、持久化参数）。
- 哈希链 `AuditService`、`bear audit verify/tail`、篡改测试。
- 文件/树 Claim、lease-owner fencing、过期、心跳续租、清理、跨会话冲突测试。
- 命令策略：包装器/管道/重定向/解释器分析、POSIX/Windows 硬拒绝、allow/deny 配置、`Tool.preflight`。
- `ShellRunner`：异步 spawn、有界 stdout/stderr、超时、取消、POSIX 进程组、Windows Job Object/taskkill 进程树终止。
- Sandbox 适配器：Linux bwrap、macOS Seatbelt、Windows Job Object；`require`/`process`/`off` 模式；fail-closed 选择。
- `BashTool`：净化环境、工作目录、退出码、Sandbox 元数据。
- 审批：精确指纹、once/session/project、持久化、过期、恢复、CLI 提示。
- `ToolExecutor` 集成：参数脱敏、preflight、审计、作用域审批、Claim、fenced 执行、结果/meta 脱敏、Sandbox 事件。
- Runtime：Claim 心跳续租/释放、审计感知清理。
- CLI：`audit verify/tail`、`secret set/list/delete`、doctor 检查 Sandbox/keyring/审计/Claim。
- 测试：命令策略矩阵、Sandbox 构造、Windows Job Object、Claim、审批、审计篡改检测、密钥/脱敏、Shell Runtime（退出/超时/取消/输出上限/拒绝/fail-closed）、bwrap 执行测试、CLI E2E。

### 验收证据

- 全量测试通过；`ruff`、`ruff format --check`、`mypy` 通过。
- 危险命令矩阵在每种模式下给出预期决策，包括包装器、管道、重定向、编码 PowerShell。
- Shell 命令正确返回 stdout/stderr/退出码；超时与取消终止进程树；输出有上限。
- `require` 在没有文件系统后端时 fail-closed；Windows Job Object 标注 process-only；`off` 标注 `NOT OS-ISOLATED`。
- 审批精确匹配、持久化、可恢复、会过期、有作用域。
- 密钥值不出现在工具输出、事件、审计详情中。
- 审计链验证通过，并能检测 detail/hash 篡改。
- 两个 Session 不能同时 Claim 同一文件或工作区树；过期/旧租约 Claim 被 fencing。
- 真实 bwrap 执行测试接入 Linux CI；Windows Job Object 测试在 Windows 运行；macOS profile 测试全平台运行。

### Hardening 审查（2026-11-19）

- Token 级语义硬拒绝覆盖引号/转义/变量/包装器；非相邻的 downloader→解释器管道也会拒绝。
- Windows Job Object 使用 suspended spawn → assign → resume，没有 assign 前的用户代码窗口。
- 文件写在变更前二次检查 Claim；Claim 要求有效且未过期的 session lease；Windows Claim 路径大小写归一化。
- 审计哈希包含 session ID，并使用无歧义 JSON；审计 resource 脱敏。
- 拒绝 keyring plaintext/file 后端（以及不安全的 chainer 子后端）。
- `config show --json` 不输出 `shell.env`/Provider headers；CLI 错误脱敏。
- 硬链接敏感别名与非普通文件被阻断；bwrap/Seatbelt mask 工作区硬链接文件。
- 直接调用 Shell/文件工具需要 executor 授权标记。
- 每项修复均有回归测试；见 `tests/unit/test_command_policy.py`、`test_claims.py`、`test_audit.py`、`test_sandbox.py`、`test_secrets.py`、`test_tools_builtin.py`、`test_approvals.py` 与 `tests/integration/test_shell_runtime.py`。

### 已知限制

- Windows 本阶段没有文件系统隔离；默认 `require` 禁用 Shell，`process` 是显式的仅进程级模式。
- Session transcript/event 写入没有逐次 epoch fencing；心跳取消与 Claim 覆盖副作用，但丢失租约的进程在察觉前可能短暂 append。
- POSIX 子孙进程终止是 best-effort（`ps` 扫描 + 进程组）；同时逃逸两者的子进程在没有 PID namespace 时可能存活。
- Windows process/off 模式的递归/glob 读取仍比显式敏感路径策略更宽。
- 本地审计无外部锚点；拥有数据库写权限者可重算整条链。
- 网络控制是 Sandbox 级全开/全关，不是逐命令策略。
- Prompt Injection 污点追踪与 MCP 信任属于后续阶段。
- Shell 环境是净化后的环境，不是完整能力沙箱；命令文本会被分析，但不是完整 shell parser。
- 当前环境未对用户真实 Credential Manager 执行 keyring set/get；macOS Seatbelt 实际执行未验证。

### 验收

- 危险命令矩阵在每种权限模式下触发预期决策，包括包装器（`bash -c`、`env`）、管道到 Shell、重定向、间接执行。
- `shell` 正确执行命令、返回 stdout/stderr/退出码，支持超时、取消、进程树清理，输出有上限。
- 有文件系统能力的后端下，Sandbox 命令不能写出工作区；能力不可用时 fail-closed。Windows process-only 明确标注没有文件系统隔离。
- 审批有作用域、精确匹配、持久化、可恢复、会过期；一次审批不会变成全量授权。
- 密钥不出现在工具输出、事件、审计详情、日志或 `config show`；keyring 不可用会被报告，`secret://` fail-closed。
- 审计链验证成功，能检测篡改记录，并记录命令/策略/审批/Sandbox/Claim 决策。
- 两个 Session 不能并发写同一文件或运行冲突的 Shell/树 Claim；过期 Claim 可回收，旧 lease owner 不能写。
- 全量测试、ruff、format、mypy、CI 通过；文档与实现一致。

## P3——上下文压缩 + Checkpoint + 恢复（已完成）

> 完成报告：[P3-COMPLETION-REPORT.md](P3-COMPLETION-REPORT.md)。

### 范围

- L1 工具结果归档、L2 确定性摘要、L3 结构化 LLM 摘要、L4 Checkpoint 重置。
- 锚点账本（目标、约束、手工事实）。
- Artifact 存储（内容寻址、读取校验）与 `read_artifact` 工具（session 隔离、按字符偏移读取）。
- 检查点状态快照（指针 + 控制状态、checksum、版本、保留策略）。
- 从 Transcript + Checkpoint 诊断进行崩溃恢复；不自动重放副作用。
- Transcript/Event/tool/model 状态的 Lease Epoch Fencing。
- Session Fork（历史位置或 Checkpoint）。

### 已交付

- `context/compaction.py`：L1-L4、预算触发、目标余量、supersede、失败回退、L3 熔断、事件 + 审计、artifact。
- `context/anchors.py`：Anchor Ledger、自动目标/约束提取、去重、预算化渲染、脱敏。
- `context/builder.py`：pinned system 消息、`system_extra`、`available_budget`，压缩视图接入。
- `artifacts/store.py`：原子写入、SHA-256 路径、读取校验、脱敏。
- Schema v3/v4：checkpoints、compactions、context_anchors、context_state、process_records、Session Fork 列、artifact kind/metadata。
- `sessions/checkpoints.py`：单事务快照、checksum、版本/边界校验、保留策略、损坏诊断。
- `sessions/recovery.py`：审计链 fail-closed、尾部悬空 tool_call 合成 “unknown/未重试”、不可修复状态阻塞、旧 Claim 清理、孤儿进程终止。
- `shell/process_registry.py`：进程登记、身份校验（POSIX /proc 或 ps、Windows GetProcessTimes）、安全终止。
- `sessions/fork.py`：安全切点、单事务复制、lineage、Anchors 继承、执行状态隔离。
- Runtime：启动前恢复、逐 Turn Checkpoint、RecoveryError/SessionLeaseError 结构化失败、cleanup 顺序修正。
- Lease Epoch Fencing：ambient `LeaseFence` + Repository 同事务校验；旧 owner 写入拒绝。
- CLI：`sessions fork/recover/checkpoints`，交互 `/compact`、`/anchor`、`/anchors`、`/fork`。
- 配置：`[context]`、`[checkpoints]`、`[recovery]`；doctor/CLI 可查看。

### 验收证据

- 单元 + 集成测试覆盖压缩各层、预算、失败回退、熔断、L4 重置、锚点保留；Checkpoint 创建/损坏/不兼容/保留/fencing；恢复的幂等/非幂等工具、Claim、进程、审计篡改、并发恢复；Fork 隔离与安全切点；旧 owner 写入拒绝；Transcript/Event 原子回滚。
- 压缩前后 Transcript 逐字节不变；L1 artifact 可读且内容一致；请求使用压缩视图。
- 崩溃模拟（running tool + 尾部悬空）可在下一次 Run 前恢复；中间悬空会阻塞并给出诊断。
- 并发恢复通过 SQLite 租约串行化，只有一个恢复者。
- 全量 pytest、ruff、format、mypy、build 通过（见 P3 Completion Report）。

### 已知限制

- Token 估算仍是启发式；L3 摘要质量依赖模型，仍可能受 Prompt Injection/幻觉影响。
- 只自动修复 Transcript 尾部的悬空 tool_call；中间乱序只报告并阻塞。
- 孤儿进程终止依赖 pid/pgid 与启动时间校验；无法验证身份时不杀。
- Checkpoint/artifact 无静态加密与外部完整性锚点；artifact 无自动 GC。
- L3 摘要调用未计入 `model_calls` 用量/成本统计。
- Fork 不继承执行状态，不会自动继续被中断的工具。
- 压缩 artifact 写入与 compaction 行之间不是跨资源事务，极端失败会留下未引用文件（内容寻址、无害）。
- Fork 在单个 `BEGIN IMMEDIATE` 事务中复制整个历史；超大 Transcript 或源会话正在高频写入时，可能让其他写入等待到 busy timeout。建议在会话空闲时 Fork。

## P4——持久化 Task / Plan / Todo 引擎（已完成）

> 完成报告：[P4-COMPLETION-REPORT.md](P4-COMPLETION-REPORT.md)。

### 范围

- Task、Plan（版本化）、依赖、尝试次数、验收、Todo 投影。
- Plan 生成、Plan 修订、状态机、调度、完成验证。
- Retry、Block、Cancel、Resume；任务事件与审计。
- Runtime 集成、Checkpoint、Recovery、Fork 与压缩边界。
- CLI 与交互命令。

### 已交付

- Schema v5：plans、plan_revisions、tasks、task_dependencies、task_attempts。
- `tasks/models.py`：Task/Plan/Attempt 状态、显式转移表、PlanDraft/TodoItem、依赖/循环校验。
- `tasks/service.py`：创建/修订 Plan、claim、状态转移、attempt、重试/阻塞/取消、崩溃对账、快照、渲染。
- `tasks/planner.py`：结构化模型规划 + 程序校验 + 有界修复。
- `tasks/verifier.py`：EvidenceVerifier、ModelTaskVerifier（fail-closed）。
- `tasks/engine.py`：调度、run 结果处理、完成验证、副作用保护、预算、报告。
- `tasks/tools.py`：task_complete / task_block / plan_revise。
- Runtime `run_plan`：单租约跨任务执行；每任务独立 run_id/轮次预算；Task Checkpoint。
- Checkpoint tasks 快照；Recovery 对账 running attempt；Fork 不继承任务。
- 每轮 pinned Task/Plan/Todo 上下文，压缩不丢失。
- CLI `tasks` 命令与交互命令；配置 `[tasks]`。

### 验收证据

- 状态机非法转移、循环依赖、依赖调度、并发 claim、修订合并与上限、fencing、崩溃对账均有测试。
- 真实 Runtime + ScriptedProvider + 真实 TaskService/ToolExecutor 执行依赖 Plan；完成验证、事件、审计、Checkpoint 通过。
- 中断 attempt 在下一轮 Plan 执行前被标记 unknown/blocked，且不自动重跑。
- 非幂等工具失败后阻塞而非自动重试。
- Fork 不继承 Plan/Task。
- CLI plan/show/run/task/revise/cancel/retry/resume 有 E2E 覆盖。
- 全量 pytest、ruff、format、mypy、build 通过（见 P4 Completion Report）。

### 已知限制

- 模型验证仍可能误判；evidence 模式不判断语义质量。
- 自动重试只对未执行非幂等工具的任务开放。
- Plan 长时间持有 Session 租约；恢复/重试需要用户显式操作。
- Fork 不继承 Plan/Task；需要重新规划。
- Transcript 会累积每个任务的 prompt 与结果，长 Plan 依赖压缩。

## P5——可扩展性

- Interceptor/Observer Hooks、外部命令 Hook、项目信任。
- Python entry-point 插件与示例。
- MCP 客户端、工具命名空间、能力策略、健康检查/重启。
- Provider Fallback、多 Provider、能力矩阵。

## P6——可观测性与评估

- Trace 服务与 CLI（`bear trace`、`stats`、`usage`、`replay`）。
- 成本模型、按 Session/任务聚合、导出。
- Benchmark 套件/运行器/报告；可复现 fixture。

## P7——UX 与分发

- 全屏 TUI（prompt_toolkit/Textual）、Plan/Todo 面板、Diff 预览。
- 安装器（`uv tool`、wheel）、发布流程、升级/迁移。
- 完整用户、Provider、工具与安全指南。

## P8——可选

- HTTP/SSE Server、Web UI、多会话服务。
- 子代理委派与并行执行。
- 远程/MCP 生态集成。

---

## 每阶段完成定义

1. 功能在代码中实现，不只是文档。
2. 新行为有单元 + 集成测试；新安全边界有安全测试。
3. `ruff`、`mypy` 与全量测试通过。
4. 文档与本路线图更新；限制如实说明。
5. 不回归已有阶段的验收。