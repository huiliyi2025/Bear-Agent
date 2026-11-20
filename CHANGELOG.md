# 变更记录

记录所有重要变更。项目按阶段路线图推进；公开版本发布后使用语义化版本。

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