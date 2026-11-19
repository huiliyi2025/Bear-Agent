# ADR 0006：Shell 执行是流水线，不是字符串检查

- 状态：已采纳
- 日期：2026-11-19

## 背景

Shell 命令是 Agent Harness 中最大的副作用面。`subprocess.run(command, shell=True)` 加一句 Prompt 并不是安全控制。命令文本也不能完整表达 Shell 的实际行为（包装器、管道、编码 payload、子进程）。

## 决策

每次 Shell 调用都经过：

1. 类型化参数与 `Tool.preflight` 命令分析；
2. 通用权限矩阵与作用域审批；
3. 文件/树 Claim；
4. Sandbox 选择与包装；
5. 有界异步进程运行器：超时、取消、进程树终止；
6. 脱敏、事件持久化与审计。

运行器操作 argv，而不是临时拼接字符串。平台 Shell 由配置选择（`/bin/bash`/`/bin/sh`、PowerShell 或 cmd）。每个流都有输出上限；达到上限后继续 drain，避免管道死锁。

## 影响

- Shell 永远不会成为绕过策略/审批/Claim 的一行命令。
- 命令分析默认保守，并且明确不是完整 Shell parser；Sandbox 与审批仍是主要 containment。
- 运行器可以用假 Sandbox 决策与真实进程测试。
- Windows 进程树终止依赖 Job Object cleanup 加 taskkill。

## 备选方案

- 仅 Prompt 禁止：拒绝；不可强制。
- 完整 Shell parser：因范围拒绝；包装器/间接执行改为升级审批。
- 完全禁止 Shell：安全但不可用；`require` 在没有文件系统 Sandbox 时提供 fail-closed 行为。