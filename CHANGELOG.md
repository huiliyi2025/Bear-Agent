# 变更记录

记录所有重要变更。项目按阶段路线图推进；公开版本发布后使用语义化版本。

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