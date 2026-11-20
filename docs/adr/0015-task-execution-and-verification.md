# ADR 0015：任务执行、完成验证与 Runtime 集成

- 状态：已采纳
- 日期：2026-11-20

## 背景

只有把 Plan 接入真实 Runtime 才有意义。同时，模型会倾向于“宣称完成”，不能把任务状态完全交给模型决定。

## 决策

### 规划

`PlanGenerator` 通过一次严格的模型调用生成 JSON Plan；程序执行：

- Pydantic 结构校验；
- task key 唯一、依赖存在、无自依赖、无环；
- Task 数量上限；
- 解析/校验失败时做有限次修复调用；失败后抛出可行动的 `PlanGenerationError`。

模型输出永远不直接落库；只有通过校验的 `PlanDraft` 才由 `TaskService.create_plan` 持久化。

### 执行

`AgentRuntime.run_plan` 在一次持续 Session Lease / Epoch 下执行整个 Plan：

1. 获取租约、设置 ambient fence、启动心跳；
2. Recovery（含 Task attempt 对账）；
3. `TaskEngine.start_next` 原子领取下一个可执行 Task 与 attempt；
4. 用该 Task 的 prompt 走现有 `_execute_request`：模型调用、ToolExecutor、权限/审批/Claim/审计/Sandbox、Checkpoint、压缩全部复用；
5. 单个 Task 的轮次预算由 `tasks.max_task_turns` 限制；
6. `TaskEngine.finish` 根据运行结果与验证决定 Task 状态；
7. 每个 Task 结束释放 Claim、持久化 run 结束事件、创建 Task Checkpoint；
8. 循环直到完成、阻塞、取消或 `max_tasks_per_run` 预算耗尽。

整个 Plan 共用一个租约，因此 Task 状态写入、验证、下一个 Task 的领取都不会与另一个进程交错；任何 Task 状态写入仍逐次校验 fence。

### 完成验证

Task 运行成功结束 ≠ 任务完成：

- Agent 必须调用 `task_complete(summary, evidence)`；该工具只写入“完成请求”，不改变 Task 状态。
- Engine 在 Task run 结束后读取请求：
  - `verification=evidence`：要求非空 summary + 至少一条 evidence；
  - `verification=model`：独立模型调用，按验收标准与 Transcript 判断，JSON 输出 `{completed, reason, unmet}`；
  - verifier 不可用/超时/格式错误：fail-closed，Task 标记 blocked，不消耗重试。
- 验证通过才调用 `TaskService.complete_task`；失败则进入重试或阻塞。

### 失败、重试与副作用

- 任务失败时，Engine 检查该 run 是否执行过非幂等工具（通过 `TOOL_CALL_STARTED` 事件关联 `tool_calls.idempotent`）。
- 只要执行过非幂等工具，就**不自动重试**：Task 标记 blocked，要求用户检查后显式 retry。
- 纯读/幂等任务可以自动重试，直到 `max_attempts`。
- 每个 Task 有 attempt 记录；历史不会被覆盖。
- 用户可通过 CLI retry（可选 `--force` 明确追加一次上限）或 plan revision 继续。

### 计划修订

Agent 通过 `plan_revise` 工具提议修订；`TaskService` 校验后落入新版本。修订只允许在没有 in_progress Task 时发生，避免正在执行的任务被悄悄替换。

### 预算与循环防护

- `max_tasks`、`max_attempts`、`max_revisions`、`max_tasks_per_run`、`max_task_turns`；
- Runtime 自身 max_turns / max_tool_calls / 墙钟预算；
- StopPolicy 重复调用检测；
- Plan 无 ready Task 时立即停止并推导 blocked/failed/completed，不空转。

## 影响

- 任务状态由程序控制，模型只能提出请求。
- 验证增加一次模型调用成本；离线/测试可用 evidence 模式。
- 非幂等任务失败后需要人工干预，牺牲自动化换取副作用安全。
- Plan 执行期间 Session 租约被长时间持有；心跳维持租约。

## 备选方案

- 模型直接写 Task 状态：拒绝；无法防止随意宣称完成。
- 每个 Task 独立 Run/租约：拒绝；任务状态更新无法与执行原子协调，且并发调度复杂。
- 自动重试所有失败：拒绝；可能重复 Shell/写操作。
- 只做文本 Plan：拒绝；无法恢复、调度或验证。