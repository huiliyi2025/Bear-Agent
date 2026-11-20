# ADR 0013：Session Fork 的隔离与继承边界

- 状态：已采纳
- 日期：2026-11-19

## 背景

用户可能想从历史位置尝试另一种方案，而不破坏原会话。Fork 必须复制足够的对话上下文，同时不能继承正在执行的工具状态、旧租约、Claim 或过宽的审批授权。

## 决策

Fork 从以下位置之一创建：指定 `message_seq`、有效 Checkpoint 的 `message_seq`，或当前 Transcript 末尾。

在单个 SQLite 事务中：

- 创建新 Session ID，记录 `parent_session_id`、`fork_message_seq`、`fork_checkpoint_id`；
- 复制 `1..cut` 的消息，保留原序号与内容；
- 复制活跃 Anchor（新 ID），但只复制来源消息序号不晚于切点、或没有来源序号的（手工/全局）锚点；来自“未来”消息的锚点不会泄露到过去的 Fork；
- 写入 `session.created` 与 `session.forked` 事件；
- 原 Session 的 session 行、消息、事件完全不变。

**安全切点**：如果切点落在“assistant 已发出 tool_call、结果在切点之后”的位置，自动回退到该 assistant 消息之前，绝不产生不配对的 tool_call。

**不继承**：tool_calls、model_calls、process_records、file_claims、approvals、compactions、context_state、Checkpoint 行。Fork 是新执行上下文，必须重新获取 Lease、重新审批、重新 Claim。`fork_checkpoint_id` 仅作为来源引用，Checkpoint 内容不复制，避免把旧执行状态当作新会话事实。

Project 作用域的审批按 project ID 生效，这是既有语义：Fork 与源会话处于同一项目时会共享 project 审批，但不共享 session/once 审批。文档与 CLI 会明确说明。

Fork 读取使用事务快照，不获取源会话 Lease、不修改源会话；源会话正在运行时也能得到一致切点。

## 影响

- 可以从 Checkpoint 或任意安全消息位置分叉，原会话不受影响。
- 新会话首次运行会重新建立 Lease/Claim，安全边界不因 Fork 放宽。
- 压缩产物不继承，新会话会在需要时重新压缩；Anchors 保留，因此目标与约束不会丢失。
- 执行状态不继承意味着 Fork 不会“接着跑”被中断的工具；这是有意的安全选择。

## 备选方案

- 复制整个 Session 行与执行状态：拒绝；旧 owner/Claim/审批会污染新会话。
- 在同一 Session 上打分支标记：拒绝；租约与 Transcript 无法独立演进。
- 继承所有审批：拒绝；一次授权不应因复制而扩大作用域。