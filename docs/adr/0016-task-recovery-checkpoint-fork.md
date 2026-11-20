# ADR 0016：Task 与 Recovery / Checkpoint / Fork / Context 的边界

- 状态：已采纳
- 日期：2026-11-20

## 背景

Task 状态跨越多个 Run 与多个进程生命周期。崩溃、租约接管、压缩和 Fork 都可能让“执行进度”与“存储状态”不一致。

## 决策

### Checkpoint

Checkpoint 的 `tasks` 块通过注入的 `TaskService.snapshot_sync` 在同一个 SQLite 事务中读取：

- 当前 Plan 行；
- 全部 active/inactive Task 行；
- 依赖映射；
- 最近 200 条 attempts。

快照只用于诊断/恢复参考；Task 表仍是恢复的事实来源。旧 Checkpoint 没有 `tasks` 字段仍可验证；新字段缺失/类型错误会使 Checkpoint 判定损坏。状态在写入前统一脱敏。

### Recovery

恢复流程在工具、进程、Claim 对账之后调用 `TaskService.reconcile_after_crash`：

- 所有 running attempt → `unknown`，记录错误原因；
- 对应 in_progress Task → `blocked`，原因要求用户检查后显式 retry；
- 绝不自动重新执行 Task；
- 写入 `task.reconciled` 事件与审计；
- Recovery 报告包含 `reconciled_tasks`；
- 仅当存在 running attempt 时才触发 Task 恢复，正常空闲会话无额外写入。

`run_plan` 在领取 Task 前先执行 Recovery；被中断的 Task 不会在本轮自动重跑。

### Fork

Fork **不继承 Plan / Task / Attempt**：

- 新 Session 没有 Plan（`plans.session_id` 唯一）；
- 不复制 Task、依赖、执行历史；
- 源 Session 的任务状态完全不变；
- 如果用户希望在新分支继续计划，需要新建/重新生成计划。

这是有意保守：Task 绑定了执行 attempt、Claim 与完成验证，复制到新 Session 会把未经重新验证的“完成”状态当作事实。Transcript 中的任务文本仍会随消息复制，但它不是可执行状态。

### Context Compression

`TurnExecutor` 每轮把 `TaskService.render_context` 作为 pinned system 块注入：

- Plan 目标、版本、状态；
- Todo 标记与依赖；
- 当前 Task、验收标准与完成/阻塞规则。

该块与 Anchor 一样属于 pinned 内容，不参与普通轮次截断，L1-L4 压缩不会删除它。Token 预算由 `tasks.context_max_tokens` 限制。

### 一致性

- Task 状态写与事件/审计分离：状态先在同一事务中提交，事件/审计随后写入；审计失败记录日志但不回滚状态（task 状态不是工作区副作用）。
- Attempt 与 Transcript 不一致时：running attempt 由 Recovery 统一 unknown；Task 不由 Transcript 推断完成。
- Tool call 成功但 Task 状态未更新：Engine 在 run 结束后基于 completion request 与 verifier 更新；崩溃时按 running attempt 处理。

## 影响

- 崩溃后任务需要一次显式 resume/retry，不自动重放。
- Checkpoint 体积增加（Plan/Tasks/Attempts），但受上限约束。
- Fork 不复用计划，用户需重新规划；避免不安全的状态继承。
- 压缩后仍保留当前任务方向。

## 备选方案

- 复制 Task 到 Fork：拒绝；完成/Claim/attempt 状态未经重新验证。
- Recovery 自动重试 Task：拒绝；可能重复副作用。
- 从 Transcript 推断 Task 完成：拒绝；模型文本不是事实。
- 把 Task 状态仅存在 Context：拒绝；崩溃即丢失。