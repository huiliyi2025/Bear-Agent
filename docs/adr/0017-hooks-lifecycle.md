# ADR 0017：Hooks 生命周期与执行边界

- 状态：已采纳
- 日期：2026-11-20

## 背景

扩展逻辑需要观察或干预 Agent 生命周期，但任意 Hook 都可能阻塞 Runtime、递归触发自身、修改上下文或试图绕过安全决策。

## 决策

Hook 分两类：

1. **进程内 Hook**：由受信任代码/Plugin 注册的 async handler；可观察事件，`before_tool_call` 可额外否决，`before_model_call` 可追加有界上下文。
2. **外部命令 Hook**：配置中的可执行文件 + 参数，通过 stdin/stdout JSON 通信。默认关闭；必须显式 `hooks.allow_external=true`。外部 Hook 只能否决工具调用，不能追加上下文，也不能读取未脱敏的密钥。

生命周期事件保持精简，复用 EventBus 作为观察源：

- 显式前置：`model_before`、`tool_before`。
- 显式错误：`model_error`。
- 其余观察事件（run/turn/tool after/error/task/plan/context/checkpoint/recovery）通过 EventBus 映射，`HOOK_*` 事件与 delta 不映射，避免递归。

执行语义：

- 顺序：priority DESC，再按注册顺序。
- `before_tool_call` 在 PermissionEngine、审批、Claim 之后、工具副作用之前运行；只能**额外拒绝**，不能 allow、不能修改参数，因此不能绕过已有安全链。
- `before_model_call` 的上下文追加有字符上限并脱敏；不能删除系统安全指令。
- 超时（默认 5s）与取消：超时按 Hook 的 `fail_mode`（open/closed）处理；取消向上传播。
- 递归防护：ContextVar 记录深度与当前事件，超过 `max_depth` 或同事件重入立即停止并记录失败。
- Hook 执行状态写入 `HOOK_STARTED/COMPLETED/FAILED/DENIED` 事件与审计（best-effort，脱敏）。
- 外部命令使用 `create_subprocess_exec`（不经 shell）、最小环境 allowlist、有界 stdout、超时杀进程树；输出必须是单个 JSON 对象。

## 影响

- Hook 无法修改工具参数或审批结果，降低了“Hook 变成后门”的风险。
- 外部 Hook 仍是用户权限下的任意进程，不是沙箱；必须显式开启并信任。
- EventBus 观察 Hook 只能在事实事件之后运行；需要前置否决/上下文修改的场景使用显式 Hook。
- 进程内 Plugin Hook 可执行任意 Python；见 ADR 0019 的信任模型。

## 备选方案

- 允许 Hook 修改工具参数：拒绝；必须重新审批/Claim，复杂且容易绕过。
- 所有 Hook 都走外部分离进程：拒绝；进程内观察/上下文扩展失去低延迟，且仍需信任。
- 每个模块自建 Hook 接口：拒绝；生命周期和错误语义会分裂。