# Bear-Agent P4 完成报告

- 阶段：P4 — Task / Plan / Todo Engine
- 日期：2026-11-20
- 版本：0.4.0
- 状态：实现与验证完成；按要求未执行 Git commit，未进入 P5。

## 1. 交付内容

### 1.1 数据模型与状态机

- Plan：每个 Session 当前结构化目标，版本化；历史版本写入 `plan_revisions` 快照。
- Task：Plan 中的执行节点，稳定 ID、key、依赖、验收标准、优先级、状态、尝试次数、结果与元数据。
- Todo：Task 的只读投影（`[ ]`/`[>]`/`[x]`/`[!]`/`[-]`），不单独持久化，避免状态漂移。
- 显式 Task/Plan 状态转移表；非法转移抛出 `PlanValidationError`。
- 依赖表 + DFS 循环检测；拒绝未知依赖、自依赖、重复 key、超限任务。
- Task 调度：依赖已 completed/skipped、pending、active 的最高优先级任务；同一 Session 同时最多一个 in_progress Task。

### 1.2 Plan Generation 与 Revision

- `PlanGenerator`：严格 JSON 规划、程序校验、有限修复调用；解析/依赖/循环/数量错误不会落库。
- `plan_revisions` 保存 version、goal、任务/依赖快照、reason、source、run_id。
- 修订按 task_key 合并：
  - 新任务 pending；
  - 定义变化的 completed/failed/blocked 任务重置 pending 并重置 attempt 计数；
  - cancelled/skipped 恢复 pending；
  - 被移除任务：completed 归档，其余 cancelled；
  - 有 in_progress Task 时拒绝修订；
  - 修订次数上限。
- Agent 通过 `plan_revise` 工具提议修订，程序校验后持久化。

### 1.3 Task Execution Engine

- `TaskEngine` 与 Runtime 解耦：`start_next` 原子领取任务并创建 attempt；`finish` 根据 run 结果与验证决定状态。
- Runtime `run_plan`：
  - 整个 Plan 持有同一 Session Lease/Epoch 与心跳；
  - 恢复 → 领取 Task → 复用现有模型/ToolExecutor/权限/审批/Claim/Audit/Sandbox/Checkpoint/压缩路径 → 验证 → 更新状态 → 下一个 Task；
  - 每任务独立 run_id 与轮次预算；每任务结束释放 Claim、记录 run 结束事件、创建 Task Checkpoint。
- 完成验证：
  - `task_complete` 只写完成请求，不改变 Task 状态；
  - evidence 模式要求 summary + evidence；
  - model 模式调用独立 verifier；失败/异常 fail-closed 为 blocked；
  - 只有验证通过才 complete。
- 失败/重试/副作用保护：
  - 读/幂等任务可自动重试至 `max_attempts`；
  - 执行过非幂等工具（通过事件关联 `tool_calls.idempotent`）的失败任务不自动重试，标记 blocked；
  - 崩溃中的 running attempt 由 Recovery 标记 unknown，Task blocked，不自动重放。
- 预算：max_tasks、max_attempts、max_revisions、max_tasks_per_run、max_task_turns + Runtime 自身预算/重复调用检测。

### 1.4 Agent 协作、上下文与工具

- 任务 prompt 包含 Plan 目标、当前任务、验收标准、Todo、依赖与完成规则。
- 每轮固定注入 `<task-plan>` pinned system 块（目标、版本、Todo、当前任务/验收），压缩 L1-L4 不会移除。
- 工具：`task_complete`、`task_block`、`plan_revise`；均要求 ToolExecutor 授权与有效 LeaseFence，不能绕过权限/Claim/Audit。
- Task 状态事件与审计覆盖 Plan 创建/修订、Task 开始/完成/失败/阻塞/取消/重试、验证、恢复。

### 1.5 CLI

- `bear tasks plan "goal" [--session ID] [--run]`
- `bear tasks show <session>` / `task <task-id>`
- `bear tasks run <session>` / `resume <session> [--run]`
- `bear tasks revise <session> --file plan.json --reason ...`
- `bear tasks cancel <session> [--task ID]`
- `bear tasks retry <task-id> [--force]`
- 交互：`/plan`、`/tasks`、`/run-plan`、`/task`、`/cancel-plan`。
- Renderer 展示 Plan/Task 事件；`--json` 输出 NDJSON。

## 2. Schema 与模块

### Schema v5

- `plans`：每 Session 一个当前 Plan、goal、status、version。
- `plan_revisions`：版本快照、reason/source/run_id。
- `tasks`：定义、状态、attempt_count/max_attempts、verification、active、结果、时间。
- `task_dependencies`：Task 依赖边。
- `task_attempts`：每次执行状态、完成请求、evidence、验证、错误。

### 新模块

- `tasks/models.py`、`tasks/service.py`、`tasks/engine.py`
- `tasks/planner.py`、`tasks/verifier.py`、`tasks/tools.py`
- `storage/task_repositories.py`

### 修改

- Runtime：`run_plan`、单租约跨任务执行、每任务 request 循环复用。
- TurnExecutor：pinned Task/Plan/Todo 上下文。
- Checkpoint：tasks 快照（同事务、脱敏、可校验）。
- Recovery：running task attempt → unknown、Task → blocked。
- Fork：不继承 Plan/Task/Attempt。
- Bootstrap：TaskService/Planner/Verifier/Engine/Task tools 装配；Maintenance 提供 task 读取。
- 配置：`[tasks]`。
- CLI/渲染器/文档/ADR。

## 3. 验证结果

环境：Windows 11 Home China 10.0.26200，Python 3.13.5，uv。

| 门禁 | 结果 |
|---|---|
| `uv run pytest` | **374 passed, 3 skipped** |
| Coverage | **80%**（tasks/service 84%、engine 82%、models 95%、planner 79%、verifier 89%、tools 95%、task_repositories 93%） |
| `uv run ruff check src tests` | 通过 |
| `uv run ruff format --check src tests` | 通过 |
| `uv run mypy src` | 通过（96 source files，strict） |
| `uv build` | 通过：`bear_agent-0.4.0.tar.gz` / `bear_agent-0.4.0-py3-none-any.whl` |

### 3.1 真实验证与模拟验证

**真实执行**

- Windows SQLite（多连接、事务、迁移、并发 claim）。
- 真实 Runtime + ToolExecutor + 权限/审批/Claim/Audit/Sandbox 路径；ScriptedProvider 驱动模型。
- 真实 TaskService/TaskEngine/Checkpoint/Recovery/Fork。
- CLI Typer 命令与配置序列化。

**模拟/条件验证**

- 模型规划/验证使用 ScriptedProvider/Fake verifier；未调用真实外部模型。
- 崩溃通过 running attempt + 释放租约模拟；未做真实 SIGKILL Harness E2E。
- 平台相关能力（Linux/macOS）沿用 P2/P3 的条件验证边界。

## 4. 定向代码审查

独立只读审查覆盖状态机、调度、修订、Runtime 集成、fencing、恢复、验证、Checkpoint/Fork/Context 与 CLI。发现 1 Critical、1 High、2 Medium、5 Low；全部修复并补回归测试。

- **Critical：Plan 修订后任务无法再次 claim。** 修订会把 attempt_count 重置为 0，而 attempt_no 由 attempt_count+1 计算，与 `UNIQUE(task_id, attempt_no)` 冲突。已把 attempt_no 改为跨修订单调递增（`MAX(attempt_no)+1`），attempt_count 仅作为当前修订的重试预算；补充“修订后重新 claim”回归测试。
- **High：run_plan 异常路径返回 reconcile 前的旧报告。** finally 中的 reconcile 会把任务/Plan 置为 blocked，但返回值仍是 active，CLI 退出码为 0。已在 reconcile 后重新计算 report，并让 CLI 将带 issues 的 active 视为失败；补充“finish 抛异常”回归测试。
- **Medium：同定义修订被误判为变更。** 存储的 max_attempts 被配置上限截断，但比较使用原始值。已比较 effective（截断后）值，并补充 no-op revision 测试。
- **Medium：verifier 看到整个会话而不是当前 attempt。** 已记录 attempt 起始 message seq，验证只使用该边界之后的 Transcript；补充跨任务证据隔离测试。
- **Low：** fail/block 未强制 attempt id（已强制）；verifier 出现 `completed=true + unmet` 时 fail-closed；被修订移除的 inactive Task 拒绝 retry；Plan 状态转移校验与 completed plan 取消拒绝；CLI `--json` 不再混入 Rich 输出，未知 session 返回 2。
- **附加加固：** 手动 retry/resume 也会检查最后一次 attempt 是否使用过非幂等工具；默认拒绝，必须显式 `--force`，避免“崩溃恢复后 resume 自动重放 Shell 副作用”。
- 审查未发现确定性完成绕过、自动副作用重放或 task 状态无 fence 写入。


## 5. 残余风险

- 模型 verifier 可能误判；evidence 模式不判断语义质量。
- 自动重试仅对未执行非幂等工具的任务开放；写文件工具标记幂等，重复执行覆盖同一内容但无跨工具事务。
- Plan 执行期间长期持有 Session Lease；崩溃后需要显式 resume/retry。
- 长 Plan 的 Transcript 会累积任务 prompt/结果，依赖压缩；早期细节可能丢失。
- Fork 不继承任务计划，需要重新规划。
- Task 审计 best-effort：状态已提交后审计失败只记录日志，不回滚状态。
- 未实现任务级网络/资源配额（沿用 Session/Runtime 预算）。

## 6. 下一阶段建议

- P5：Hooks、插件、MCP、Provider fallback；为 Task 工具/Plan 事件提供 Hook 扩展点。
- 增加真实 SIGKILL Plan 执行 E2E 与多进程竞争测试。
- 评估任务级资源/费用统计与 verifier 质量评估集。
- 考虑 Plan 模板、人工审批计划与多 Agent 分工。