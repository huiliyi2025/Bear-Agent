# ADR 0010：文件/树 Claim 与 lease-owner fencing

- 状态：已采纳
- 日期：2026-11-19

## 背景

多个会话可以同时运行在同一工作区。交错的文件写入会互相覆盖，而 Shell 命令可以写文件工具看不到的任意文件。

## 决策

在 SQLite 中引入 Claim：

- 文件写入 Claim 目标路径，`scope=file`；
- Shell 执行 Claim 工作区树，`scope=tree`。

Claim 包含 session ID、lease owner、mode、获取时间与过期时间。树 Claim 与树下任何文件 Claim 冲突；shared-read Claim 可共存。Claim 由 Run 心跳续租，在 Runtime `finally` 释放，过期或持有会话不再持有租约时清除。

执行前，执行器调用 `assert_owned`，同时检查 Claim 的 lease owner 与会话当前 lease owner。这会 fencing 掉运行租约已被接管的进程。

## 影响

- 并发文件写入与 Shell/文件组合会以可行动的冲突错误拒绝。
- Shell 对工作区文件 Claim 串行化；这是有意粗粒度且安全的。
- 崩溃的 Run 可能阻塞路径，直到 Claim 过期或租约失效。
- Claim 是单机（SQLite）；分布式协调不在范围内。

## 备选方案

- 不做 Claim：拒绝；并发会话会静默覆盖文件。
- OS 文件锁：不可移植，且无法与 Shell 树作用域集成。
- 逐目录细粒度 Shell 跟踪：需要文件系统监控，跨平台不可靠。