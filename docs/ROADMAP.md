# 开发路线图

按阶段交付。一个阶段只有在验收检查通过、测试全绿、文档与实际一致时才算完成。

## 阶段状态

| 阶段 | 范围 | 状态 |
|---|---|---|
| P0 | 调研与架构基线 | 已完成 |
| P1 | 内核基础 | **已完成** |
| P2 | Shell 执行与执行安全 | **已完成** |
| P3 | 上下文压缩 + Checkpoint + 恢复 | 下一阶段 |
| P4 | 持久化 Task / Plan / Todo 引擎 | 计划中 |
| P5 | Hooks、插件、MCP、Provider Fallback | 计划中 |
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

## P3——上下文压缩 + Checkpoint + 恢复

### 范围

- L1 工具结果裁剪、L2 陈旧轮摘要、L3 结构化 LLM 摘要、L4 Checkpoint/会话分裂。
- 锚点账本（目标、约束、决策、文件、错误、验证状态）。
- Artifact 存储与 `recall`/`read_artifact`。
- 风险操作前与步骤边界的 Checkpoint 快照。
- 从快照 + 事件重放进行崩溃恢复。
- Session Fork。

### 验收

- 长会话压缩后不丢失任务状态与验收标准。
- 每次有损重写都有 artifact 引用。
- 被 kill 的进程可以从安全点恢复，不重放副作用。
- 摘要质量检查覆盖全部锚点类别。

## P4——持久化 Task / Plan / Todo 引擎

### 范围

- Task、Plan（版本化）、Step（依赖、尝试次数、验收）、Todo 投影。
- Retry、Replan、验证、计划审批、Plan Mode。
- 任务事件与进度投影；从第一个未完成步骤恢复。
- `todo` 与 `plan` 工具。

### 验收

- 多步骤任务重启后从第一个未完成步骤继续。
- 失败步骤按策略重试，之后 Replan 或带原因阻塞。
- 任务完成必须有证据；未验证完成必须显式标记。
- Todo 与 Plan 永不分裂。

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