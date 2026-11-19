# 安全

> 阶段 2 范围。本文说明当前已经强制实施的控制，以及尚未实现的控制。
> 不要把推迟项当作已实现。

## 威胁模型

Bear-Agent 假设模型可能出错、被操纵，甚至主动对抗：

- 来自仓库文件、网页内容或工具输出的 Prompt Injection；
- 尝试读取凭证或写出工作区；
- 破坏性或不可逆的 Shell 命令；
- 通过 Shell、解释器、包装器、管道或编码进行的间接执行；
- 恶意 MCP Server 或插件（阶段 5）；
- 资源耗尽（轮次、Token、磁盘、进程、输出）；
- 通过工具输出、日志、事件或审计记录泄露密钥；
- 多个会话同时写同一文件或工作区。

**信任边界是运行时，不是 Prompt。**

## 已实施控制（阶段 2）

### 权限模式与审批

| 模式 | 读取 | 工作区写入 | Shell 执行 |
|---|---|---|---|
| `plan` | 允许 | 拒绝 | 拒绝 |
| `supervised` | 允许 | 询问 | 询问 |
| `auto` | 允许 | 允许 | 安全命令允许；有风险命令询问 |
| `unrestricted` | 允许 | 允许 | 允许，但硬拒绝模式除外 |

- 拒绝规则优先于任何 allow 规则。
- 审批是“工具 + 参数”的精确指纹授权：
  - once：不持久化；
  - session：持久化，按会话作用域，会过期；
  - project：持久化，按项目/cwd 作用域，会过期。
- 一次授权不会授权其他命令或其他工具调用。
- 审批决策会写入事件与审计。
- 无 TTY 的无头运行使用拒绝型 approver。

### Shell 命令策略

命令策略在审批与执行之前、于执行器流水线中运行。它默认保守，且不是完整 Shell parser。

- 硬拒绝（模式/allowlist 都不能覆盖）：
  - 根目录递归删除（`rm -rf /`、`rm -rf C:\`、`del/rd` 根路径形式）；
  - `mkfs`、裸 `dd` 写入、磁盘格式化/diskpart/注册表删除；
  - shutdown/reboot/halt/poweroff；
  - `git push --force`、`git reset --hard`、`git clean -fdx`；
  - `DROP TABLE` / `TRUNCATE TABLE`；
  - `curl|wget | sh/bash/powershell/cmd/python/node/perl/ruby/php/iex`；
  - PowerShell `Invoke-Expression` 与递归强制删除；
  - fork bomb 与用户配置的拒绝正则。
- 包装器/解释器（`sudo`、`env`、`bash -c`、`python -c`、`cmd /c`、PowerShell `-Command`/`-EncodedCommand`、`xargs`）会被拆解、解码或升级审批。
- 管道、命令链、输出重定向需要审批。
- 没有文件系统 Sandbox 时，重定向到工作区外直接拒绝；有 Sandbox 时需要审批。
- 只有在路径参数留在工作区内时，安全只读命令才允许在 `auto` 下运行；未知命令需要询问。
- 显式敏感文件路径参数（`.env`、credentials、密钥、`.npmrc`、`.netrc`、`.git-credentials`、secrets/tokens 文件）会被硬拒绝，包括符号链接形式。bwrap 与 Seatbelt 还会 mask 已发现的敏感工作区文件；Windows process-only 模式不会。
- 递归/glob 读取（如 `grep -r KEY .`）无法仅靠文本策略完全阻止。请使用具备文件系统能力的 Sandbox，并避免把密钥放进工作区；Windows process-only/off 模式仍是已知风险。
- `shell.allow_commands` / `shell.allow_prefixes` 是用户的显式覆盖；硬拒绝仍然优先。

### OS Sandbox

| 平台 | 后端 | 文件系统隔离 | 进程/资源 |
|---|---|---|---|
| Linux | bubblewrap | 系统只读、工作区可写、私有 `/tmp`、可选网络 unshare | `--die-with-parent`、PID namespace |
| macOS | Seatbelt `sandbox-exec` | 默认拒绝、仅工作区/临时目录可写 | 允许进程 |
| Windows | Job Object | **无** | 进程树、进程数、内存限制 |
| 任意 | `off` | 无（显式 opt-out） | 无 |

- `sandbox.mode=require`（默认）要求文件系统级后端，否则 Shell 工具拒绝执行。因此 Windows 默认禁用 Shell。
- `sandbox.mode=process` 接受仅进程级 containment；结果会明确标注没有文件系统隔离。
- `sandbox.mode=off` 是显式 opt-out。工具结果会写 `NOT OS-ISOLATED`；绝不会被报告成 sandbox。
- Sandbox 初始化失败会在执行前中止命令。
- 不承诺 Windows 文件系统隔离。AppContainer/restricted token 隔离属于未来工作。

### 密钥

- 生产密钥存放在 OS keyring（Windows Credential Manager、macOS Keychain、Linux Secret Service）。
- `secret://name` 与 `env:NAME` 引用在创建 Provider 时解析；keyring 中缺失会 fail-closed。
- 没有明文文件密钥回退。明文配置值只作为用户显式选择，绝不称为安全存储。
- 已知密钥值与常见凭证格式会从以下位置脱敏：
  - 持久化/返回前的工具结果；
  - 事件 payload；
  - 审计详情；
  - 工具调用行中保存的命令参数；
  - `bear_agent` logger 输出的记录（best-effort）。
- Shell 环境由小 allowlist + 显式配置构建；默认不继承 Provider API Key。

### 审计日志

- `audit_log` 是带 `prev_hash`/`hash` 的 append-only 日志。
- 记录覆盖策略决策、审批、Sandbox 决策、Claim、命令/工具开始与结束、拒绝和失败。
- `bear audit verify` 重算哈希链并报告第一条坏记录；`bear audit tail` 显示最近记录。
- 哈希字段包含 session ID、actor、action、resource、规范化 detail JSON 与时间戳；整条记录使用无歧义 JSON，而不是分隔符拼接。Hardening 之前的 P2 审计链使用旧格式，将无法通过验证；这是未发布阶段数据。
- 本地哈希链能检测意外损坏与朴素编辑/删除。它**不是**防篡改存储：拥有数据库写权限的攻击者可以重算整条链。目前没有外部锚点/签名。

### 文件 Claim

- 文件写会 Claim 目标路径；Shell 命令会 Claim 工作区树。
- Claim 保存 session ID、lease owner、scope、mode 与过期时间。
- Claim 随 Run 租约续租，在 `finally` 释放，崩溃后过期。
- Claim 被 session lease owner fencing：租约被接管的进程不能获取或校验 Claim。
- 树 Claim 与树下的文件 Claim 冲突；这会跨会话串行化 Shell 写与文件工具写。

### 工具执行

- 执行前进行 JSON Schema 校验；非法参数不会到达工具。
- 未知工具、参数不完整、preflight 失败都会被拒绝。
- 每个工具有超时与输出上限。
- 每个工具调用执行前持久化 `running`，执行后进入终态；崩溃会标记 `unknown`，绝不静默重放。
- 结果在持久化前、返回模型前都会脱敏。
- 安全动作之前的审计失败会中止该动作（fail-closed）。

### Hardening 审查（2026-11-19）

针对真实执行路径重新审查 P2 实现，已完成修复：

- 硬拒绝规则增加了 token 级语义检查，因此引号、反斜杠转义、变量形式（`$HOME`、`${HOME}`、`%USERPROFILE%`）、通配根路径、包装器（`env`、`sudo`、`busybox`、`toybox`）都无法把已知根删除变成 `allow`。非相邻管道（如 `curl ... | tee ... | sh`）也会硬拒绝。
- Windows Job Object 现在以**挂起**状态创建进程，加入 Job 后再恢复；不存在 assign 前的用户代码窗口。
- 文件 Claim 在 `write_file`/`edit_file` 实际变更前再次检查，而不只在工具调用前检查。过期 session lease 既不能获取也不能满足 Claim。
- Windows 上 Claim 路径进行大小写归一化（大小写不敏感文件系统）。
- 审计哈希现在包含 session ID，并使用无歧义 JSON payload，而不是分隔符拼接；审计 resource 会脱敏。
- 拒绝 plaintext/file 类型的 keyring 后端（包括不安全的 chainer 子后端）。
- `config show --json` 不再输出 `shell.env` 或 Provider headers。
- CLI/Runtime 错误输出经过脱敏器。
- 文件工具阻断硬链接敏感别名与非普通文件（FIFO/设备）；bwrap/Seatbelt 也会 mask 工作区硬链接文件。
- 直接 Shell 调用必须经过 Tool preflight（`approved` 标记）；直接文件写需要 executor 授权。

Hardening 后的残余风险：

- Session transcript/event 写入没有逐次 lease epoch fencing；心跳取消、Claim 与 Run 租约覆盖副作用，但丢失租约的进程在中途仍可能在察觉前 append。
- POSIX 子孙进程终止是 best-effort（`ps` 扫描 + 进程组）；同时逃逸两者的子进程在没有 PID namespace 的宿主机上可能存活。
- 递归/glob Shell 读取仍比显式路径策略更宽；Windows process-only/off 模式不 mask 敏感工作区文件。
- keyring set/get 通过后端选择/拒绝逻辑测试；当前测试环境没有写入用户真实 Credential Manager。
- macOS Seatbelt profile/deny 规则有单元测试，但当前环境未验证实际执行。

## 尚未实现

| 控制 | 阶段 | 当前风险 |
|---|---|---|
| Windows 文件系统隔离（AppContainer/restricted token） | 后续 | 已评估但未交付；Windows Job Object 仅进程级，`require` 默认阻止 Shell |
| MCP 信任与能力策略 | P5 | MCP 尚未实现 |
| Prompt Injection 污点追踪 | P3/P5 | 不可信输出未作为数据流追踪 |
| 外部审计锚点/签名 | 后续 | 本地哈希链可被数据库写者重算 |
| 静态加密数据库 | 后续 | SQLite 在磁盘上为明文 |
| 逐命令网络策略 | 后续 | Sandbox 网络是会话级全开/全关 |
| Windows Job Object assign 竞态 | 已修复 | 进程挂起创建、加入 Job、再恢复 |

## 运维建议

- 探索不可信仓库时使用 `supervised` 或 `plan` 模式。
- 不要在项目中保存 API Key；使用 `bear secret` 或环境变量。
- Windows 上使用 `sandbox.mode=process` 获得 Job Object containment，或仅在接受无隔离运行时使用 `off`。不要把 `off` 描述成 sandbox。
- 仔细阅读审批提示；project 作用域仍然只覆盖精确命令/工具调用。
- 把 `BEAR_DATA_DIR` 放在受信任、访问受控的文件系统上。
- 升级并执行数据库迁移前备份数据目录。
- 把模型输出与工具输出都视为不可信。

## 漏洞报告

请私下向维护者报告安全问题，不要开公开 issue。请提供最小复现、受影响版本和影响。不要包含真实凭证。

## 贡献者安全不变量

1. 文件操作绝不能绕过 `PathGuard`。
2. 绝不能使用未校验或不完整的参数执行工具调用。
3. 绝不能让工具或模型输出修改权限模式或工具目录。
4. 绝不记录密钥或完整凭证文件内容。
5. 副作用在持久化之前不得标记为完成。
6. 新增有副作用的工具必须提供安全测试、preflight/Claim 设计与审计事件。
7. 安全控制必须 fail-closed，错误信息必须可行动。
8. 不要把仅进程级 containment 报告为文件系统隔离。