# ADR 0014：Task / Plan / Todo 数据模型与状态机

- 状态：已采纳
- 日期：2026-11-20

## 背景

复杂目标需要多步骤执行，但“让模型输出一段 Todo 列表”无法保证依赖顺序、失败重试、崩溃恢复或并发安全。Task 状态必须是持久化事实，而不是 Prompt 里的文本。

## 决策

### 三个概念，两个实体

- **Plan**：一个 Session 当前的结构化目标与任务集合。Session 与 Plan 一对一，Plan 带版本号。
- **Task**：Plan 中的可执行节点，拥有稳定 ID、任务 key、依赖、验收标准、状态、尝试次数与结果。
- **Todo**：Task 的只读投影（`[ ]`、`[>]`、`[x]`、`[!]`、`[-]`），不单独持久化。避免 Task 与 Todo 两份状态漂移。

### Plan 修订

Plan 行保存当前版本；每次创建/修订把完整快照写入 `plan_revisions`（goal、tasks、dependencies、reason、source、run_id）。Task 行保持稳定身份，因此执行历史、attempts 与依赖关系不会因为快照而复制两份。

修订按 `task_key` 合并：

- 新 key：创建 pending Task。
- 已完成且定义未变：保持 completed。
- 已完成但定义变化：重置 pending、attempt_count=0（历史 attempts 保留）。
- failed/blocked 且定义变化：重置 pending、attempt_count=0。
- cancelled/skipped 且仍在计划中：恢复为 pending。
- 修订中缺失的 Task：completed 保留但 `active=0`；其他状态标记 cancelled 并 `active=0`。
- 任一 Task 处于 in_progress 时拒绝修订；Agent 必须先 block/cancel 当前任务。
- 修订次数有上限；初始快照不计入 revision 配额。

### 状态机

Task 状态：`pending`、`in_progress`、`completed`、`failed`、`blocked`、`cancelled`、`skipped`。

合法边由 `ALLOWED_TASK_TRANSITIONS` 显式定义：

- pending → in_progress / blocked / cancelled / skipped
- in_progress → completed / failed / blocked / cancelled
- failed → pending（重试）/ blocked / cancelled
- blocked → pending（显式重试）/ cancelled
- completed → pending（仅 Plan 修订重置）
- cancelled/skipped → pending（显式恢复）

Plan 状态：`draft`、`active`、`completed`、`failed`、`blocked`、`cancelled`。调度器根据 Task 状态推导 Plan 状态：有可执行 Task → active；全部完成 → completed；仅有失败 → failed；等待失败/阻塞依赖 → blocked。

### 依赖与调度

依赖存储在 `task_dependencies`，创建/修订时：

- 未知依赖、重复 key、自依赖由 Pydantic 拒绝；
- 循环依赖由 DFS 拒绝；
- 最大 Task 数由配置限制。

`claim_next` 在一个 SQLite 事务内选择所有依赖已 completed/skipped、状态 pending、active 的最高优先级 Task，转为 in_progress 并创建 running attempt；同一 Session 同时最多一个 in_progress Task。排序为 priority DESC、order_index ASC、created_at ASC。

## 影响

- 状态机与依赖由程序控制，模型只能提议 Plan/修订/完成请求。
- Task 身份稳定，执行历史可追溯。
- 快照会重复存储计划内容，但总量受 Task/Revision 上限约束。
- 修订会重置被改变任务的尝试计数，可能重新执行已完成工作；这是显式计划变更的代价。

## 备选方案

- Task 与 Todo 分别持久化：拒绝；必然漂移。
- 修订时创建全新 Task 集：拒绝；执行历史与稳定身份丢失。
- 允许任意状态跳转：拒绝；会产生不可解释的进度。
- 用模型判断依赖顺序：拒绝；依赖必须可验证、可测试。