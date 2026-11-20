# Bear-Agent P3 完成报告

- 阶段：P3 — Context Compression、Checkpoint、Session Recovery、Session Fork
- 日期：2026-11-19
- 版本：0.3.0
- 状态：实现与验证完成；按要求未执行 Git commit，未进入 P4。

## 1. 交付内容

### 1.1 上下文压缩

- L1：旧工具输出写入内容寻址 artifact，上下文中替换为预览 + artifact id；支持 `read_artifact` 按字符偏移读回。
- L2：确定性结构化摘要（目标、约束、决策、工具活动、错误、状态）。
- L3：模型摘要；空输出/异常回退 L2；连续失败熔断按“剩余压缩尝试次数”跨 Run 计数，不会因 Run 的 turn index 重置而永久禁用。
- L4：先创建 Checkpoint，再重置旧历史；Checkpoint 失败则不重置。
- Anchor Ledger：首个用户目标、约束消息、手工锚点；去重、salience、预算化渲染；压缩与 L4 后仍保留。
- 预算触发：`trigger_ratio * available_budget`；目标 headroom；若“最近轮次 + Anchors”本身超过触发阈值，最多执行 L1/L2 并记录 target unreachable，避免每轮重复 L3/L4。
- 压缩只读 Transcript；compaction 记录覆盖范围、前后 token、artifact 引用与 supersede 关系；原始消息永不修改。
- 所有新持久化面（Checkpoint 状态、Anchors、artifact、L1 preview、L2/L3 摘要、进程命令预览）经过 `SecretRedactor`。

### 1.2 Checkpoint

- 单事务快照：Session 元信息、event/message 指针、Run/turn、context generation、最新 compaction、活跃 Anchors、工具状态、用量。
- SHA-256 checksum 覆盖 session、kind、序号、状态与 schema version；读取时校验版本、checksum、边界。
- 损坏/不兼容 Checkpoint 只记录 `checkpoint.invalid`，不参与恢复、不删除。
- 按 `keep` 保留；prune 与 fence 在同一事务。
- 每 N 个 turn（默认 1）自动创建；L4 前、手动命令也可创建。

### 1.3 Session Recovery 与 Lease Epoch Fencing

- Runtime 获取租约后设置 context-local `LeaseFence`；Transcript、Event、tool_calls、model_calls、session 状态、Checkpoint、Compaction、Anchor、Artifact、Process 写入在**同一事务内**校验 owner/epoch/expiry。
- 后台任务继承 ambient fence；租约被接管后旧 owner 的延迟写入会被拒绝。
- Runtime cleanup 顺序修正：标记中断工具 → 置 idle → 写 Run 结束事件 → 最后释放租约。
- 恢复流程：`recovery.started` → 审计链验证（fail-closed）→ Checkpoint 诊断 → 尾部悬空 tool_call 合成 “unknown / 未自动重试” → 孤儿进程 → 旧 Claim → `recovery.completed`。
- 不自动重放工具；幂等工具标注可重试，非幂等/有副作用工具要求检查或询问用户。
- 中间位置的悬空 tool_call 不写乱序结果，设置 `report.blocked`，Runtime 拒绝开始新 Turn 并给出人工处理路径。
- 当前 owner 的进程不会被当作孤儿；进程终止前原子领取（running → unknown）、再次校验 fence、校验 pid/pgid/启动时间；Windows 优先 `taskkill /T` 终止进程树。
- 旧 Claim 释放与 fence 校验在同一事务，恢复中租约被接管不会删除新 owner 的 Claim。
- 并发恢复由 SQLite 租约串行化，只有一个恢复者能成功。

### 1.4 Session Fork

- 从 `at_message_seq`、有效 Checkpoint 或 Transcript 末尾 Fork。
- 安全切点回退，绝不产生不配对的 tool_call/tool_result。
- 单事务复制消息与来源序号 <= 切点的活跃 Anchors（无来源序号的手工/全局锚点也复制）；“未来”锚点不泄露到过去的 Fork。
- 新 Session ID + `parent_session_id` / `fork_message_seq` / `fork_checkpoint_id`；源 Session 行、消息、事件完全不变。
- 不继承 tool_calls、model_calls、process_records、file_claims、approvals、checkpoints、compactions、context_state。
- CLI：`sessions fork`、`sessions recover`、`sessions checkpoints`；交互 `/compact`、`/anchor`、`/anchors`、`/fork`。

## 2. 架构与存储变更

### Schema v3

- `sessions`：`parent_session_id`、`fork_message_seq`、`fork_checkpoint_id`。
- `artifacts`：`kind`、`metadata_json`。
- 新表：`checkpoints`、`compactions`、`context_anchors`、`context_state`。

### Schema v4

- `process_records`：pid/pgid/lease owner/command preview/status，用于崩溃后进程恢复。
- v3 列迁移改为可重试：崩溃在部分 ALTER 之后不会因 duplicate column 失败。

### 新模块

- `context/compaction.py`、`context/anchors.py`、`artifacts/store.py`、`runtime/summarizer.py`。
- `sessions/checkpoints.py`、`sessions/recovery.py`、`sessions/fork.py`。
- `shell/process_registry.py`、`storage/continuity_repositories.py`、`core/fencing.py`。
- `tools/builtin/artifacts.py`（`read_artifact`）。
- `bootstrap.py` 新增 `Maintenance` 组合根（无 Provider/keyring 的离线恢复/Fork 命令）。

## 3. 验证结果

环境：Windows 11 Home China 10.0.26200，Python 3.13.5，uv。

| 门禁 | 结果 |
|---|---|
| `uv run pytest` | **291 passed, 3 skipped** |
| Coverage (`--cov`) | **80%**（P3 新模块：checkpoints 97%、fork 94%、recovery 87%、compaction 88%、anchors 90%、read_artifact 100%、process_registry 46%，POSIX/Windows 平台分支未全覆盖） |
| `uv run ruff check src tests` | 通过 |
| `uv run ruff format --check src tests` | 通过 |
| `uv run mypy src` | 通过（88 source files，strict） |
| `uv build` | 通过：`bear_agent-0.3.0.tar.gz` / `bear_agent-0.3.0-py3-none-any.whl` |
| CLI smoke | `bear --version` = 0.3.0；`bear sessions --help` 列出 fork/recover/checkpoints |

跳过项：

- Linux bubblewrap 执行测试（当前环境无可用 bwrap）。
- 符号链接安全测试（当前文件系统不允许创建符号链接）。
- POSIX FIFO 测试（Windows 不支持）。

### 3.1 真实验证与模拟验证的边界

**真实执行（本机 Windows）**

- SQLite 真实数据库、多连接、事务、WAL、迁移。
- Runtime + 真实 ToolExecutor + 真实 ShellRunner + Windows Job Object/进程创建时间校验/终止；恢复测试真实 spawn 进程并验证终止。
- CLI 命令（Typer）与 config 序列化。
- 标记为跳过的平台分支未执行。

**模拟/条件验证**

- 崩溃状态通过直接构造 `running` tool_calls 行、悬空 assistant tool_call、释放租约来模拟；没有真实 SIGKILL 整个 Harness 进程的 E2E。
- 压缩/恢复的模型调用使用 ScriptedProvider/FakeSummarizer；未调用真实外部模型。
- Linux/macOS 进程终止分支、macOS Seatbelt 仅通过单测/构造测试，未在当前环境真实执行。
- Windows 进程树终止的真实子进程链（npm/node）未构造；只验证了直接进程。

## 4. 定向代码审查与修复

独立只读审查覆盖恢复一致性、并发/fencing、Checkpoint 完整性、压缩预算、Fork 隔离、进程恢复、SQLite 事务、脱敏与测试缺口。未发现 Critical。

已修复：

- **High：恢复可能删除新 owner 的 Claim。** `release_stale` 现在在删除事务内校验 fence。
- **High：`terminate_orphans` 可能杀死当前 owner 的进程 / 租约过期后继续杀。** 跳过当前 owner；事务内原子领取；发信号前再次校验 fence。
- **Medium：L3 熔断使用绝对 turn index，跨 Run 永久禁用。** 改为剩余尝试次数。
- **Medium：目标不可达时每轮重复 L2/L3/L4。** 最近轮次+Anchors 已超触发时限制为 L1/L2 并记录 target unreachable。
- **Medium：Windows 终止不杀进程树。** 优先 `taskkill /T /F`，TerminateProcess 兜底。
- **Low：L1 preview/L2 摘要未在 control-state 中脱敏。** 已脱敏并补测试。
- **Low：Checkpoint prune / Artifact 行写入未 fence。** 已加同事务 fence。
- **Low：10s PID 身份容差过宽。** 收紧到 2s。
- **附带：数据库迁移拒绝新版本时泄漏连接。** 构造失败时关闭连接。

仍记录但未修复（Low）：

- Fork 在整个复制期间持有 SQLite 写锁；超大 Transcript 可能让其他写入等待到 busy timeout。建议会话空闲时 Fork。
- POSIX 进程终止分支在当前 Windows 环境无法真实验证。
- L3 模型调用未计入 `model_calls` 用量。

## 5. 残余风险

- 只自动修复 Transcript 尾部的悬空 tool_call；中间位置只诊断并阻塞。
- 进程终止依赖 pid/pgid/启动时间；无法验证身份时不杀。
- Checkpoint/artifact 无静态加密、外部完整性锚点与自动 GC。
- L3 摘要仍可能受 Prompt Injection/幻觉影响；锚点与 L2 降低风险但不消除。
- Fork 不继承执行状态，不自动继续被中断的工具。
- 压缩 token 估算为启发式。
- 本地审计链无外部锚点（P2 既有）。

## 6. 下一阶段建议

- P4：持久化 Task/Plan/Todo 引擎，把“未完成事项”从 Context 摘要升级为可恢复的任务状态。
- 为 fork/recovery 增加真实 Harness SIGKILL E2E（子进程 + 孤儿 Shell）。
- 为 L3 摘要调用记录 model_calls/usage，并增加摘要质量锚点评测。
- 评估 artifact GC 与 Checkpoint 静态加密/签名。