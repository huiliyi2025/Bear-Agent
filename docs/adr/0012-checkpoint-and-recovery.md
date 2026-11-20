# ADR 0012：Checkpoint、Lease Epoch Fencing 与 Session Recovery

- 状态：已采纳
- 日期：2026-11-19

## 背景

进程可能在模型调用、工具执行、Transcript 写入或 Checkpoint 创建的任何时刻退出。SQLite 保证单条事务原子，但跨表的会话状态仍可能处于“assistant 已声明 tool_call、工具结果尚未写入”的中间态。旧进程也可能在租约被接管后继续尝试写入。

## 决策

### Checkpoint

Checkpoint 只保存**指针与不可从 Transcript 推导的控制状态**，不复制消息：

- Session 元信息、`event_seq`、`message_seq`；
- 当前 Run 信息与 turn 序号；
- 上下文 generation、最近 compaction、活跃 anchors；
- running/recent 工具调用快照与用量汇总。

状态以规范化 JSON 存储，checksum 覆盖 session ID、kind、序号、状态与 schema version。读取时重新计算 checksum 并校验版本、边界（不得超前于当前 Transcript）。损坏或不兼容的 Checkpoint 只记录 `checkpoint.invalid` 事件，不参与恢复；Transcript 始终是事实来源。

Checkpoint 在单个 SQLite 事务中完成快照与插入，写入前验证 Lease fence。每 N 个 turn（默认 1）创建一次，并可配置保留数量；压缩前/后、恢复、Fork、手工操作也会创建。

### Lease Epoch Fencing

Session 租约包含 owner 与单调递增 epoch。Runtime 获取租约后设置一个 context-local 的 `LeaseFence`；所有 Transcript、Event、tool_calls、model_calls、session 状态与 continuity 写入默认读取该 fence，并在**同一个事务内**校验 owner、epoch 与过期时间。后台任务继承该上下文，因此旧 owner 即使延迟醒来也无法写入。

Fencing 拒绝写入而不是静默丢弃：`SessionLeaseError` 会终止该 Run，并由新的 owner 在恢复时处理中间态。

### Recovery

恢复在新 owner 已获得租约后、追加新用户消息之前执行：

1. 记录 `recovery.started` 并审计；若启用审计，先验证哈希链，链损坏则 fail-closed 拒绝恢复。
2. 校验最新 Checkpoint（仅作诊断/状态参考，不回滚 Transcript）。
3. 找出“assistant 声明了 tool_call 但没有对应 tool result”的情况。仅当该 assistant 消息位于 Transcript 尾部（之后没有用户消息）时，追加合成的 tool result：明确说明执行被中断、状态未知、**未自动重试**；幂等工具标注可安全重试，非幂等/有副作用工具要求先检查工作区或询问用户。工具调用行标记为 `unknown`，绝不重新执行。
4. 终止该会话遗留的 Shell 进程：进程表记录 pid/pgid/启动时间；当前 owner 自己的进程绝不会被当作孤儿。每个候选进程先在事务内校验 fence 并原子地领取（状态 running → unknown），只有一个恢复者能领取；发信号前再次校验 fence；终止前校验进程身份，无法验证身份时不杀进程并报告。Windows 优先 `taskkill /T /F` 终止进程树。
5. 释放该会话中不属于当前 owner 的 Claim；当前 owner 的 Claim 不动。fence 校验与删除在同一个事务内，因此恢复过程中租约被接管也不会删掉新 owner 的 Claim。
6. 记录 `recovery.completed` 与审计明细；异常时记录 `recovery.failed` 并抛出 `RecoveryError`。

Completing recovery 需要写入 Event 与 Audit；审计不可用且配置 `require_audit=true` 时恢复失败。

### 并发

租约是恢复锁：两个进程同时恢复同一 Session 时，`acquire_lease` 的 `BEGIN IMMEDIATE` 串行化，只有一个成功；失败者得到 `SessionLeaseError`，不会发生双重恢复或重复写入。

## 影响

- 崩溃后可继续会话，用户能看到哪些操作状态未知。
- 不会自动重放有副作用的工具；需要用户决策。
- Fencing 增加了每次写入的一次 SELECT，但换来旧 owner 无法污染状态。
- Windows 进程身份校验/终止依赖 ctypes；无法验证时选择不杀，可能留下孤儿进程。
- 极端情况下（中间位置的悬空 tool_call）无法在不破坏消息顺序的前提下自动修复，恢复会给出诊断并建议从 Checkpoint Fork。

## 备选方案

- 把所有 `running` 工具直接改成 `failed`：拒绝；会掩盖“可能已执行”的副作用。
- 自动重试所有工具：拒绝；可能重复写文件/执行命令。
- 只依赖进程内状态：拒绝；崩溃后不存在。
- 用文件锁代替 SQLite 租约：拒绝；跨平台与崩溃清理不可靠。