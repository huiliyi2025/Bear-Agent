# ADR 0007：Sandbox 能力必须显式声明并 fail-closed

- 状态：已采纳
- 日期：2026-11-19

## 背景

Linux、macOS、Windows 提供不同的隔离原语。“限制工作区写入”不是文件系统隔离。把仅进程级机制伪装成文件系统 Sandbox 会造成虚假安全感。

## 决策

每个 `Sandbox` 声明 capabilities（`filesystem`、`network`、`process`、`resource`）与后端名称。`SandboxManager` 模式：

- `require`（默认）：只接受具备文件系统能力的后端；否则在执行前拒绝命令；
- `process`：接受进程/资源 containment（Windows Job Object），但结果与审计明确说明文件系统未隔离；
- `off`：显式 opt-out；工具结果 `sandboxed=False` 并标注 `NOT OS-ISOLATED`。

后端：Linux bwrap、macOS Seatbelt、Windows Job Object。Sandbox 初始化或 spawn hook 失败会中止命令。

## 影响

- Windows 默认禁用 Shell；用户必须显式选择 `process` 或 `off`。
- Windows Job Object 进程以挂起状态创建，加入 Job 后再通过 `NtResumeProcess` 恢复；不存在 assign 前的执行窗口。
- 测试可以断言 capabilities，而不是假设后端就是完整 Sandbox。
- macOS Seatbelt 已被 Apple 弃用，但仍是唯一实用的内置单进程机制。
- Windows AppContainer/restricted token 隔离经过评估但未交付：它需要原生 ACL/能力配置和进程启动辅助，部分实现会是虚假安全声明。Job Object 仍然仅进程级；`require` 默认阻止 Windows Shell。

## 备选方案

- 静默退化为无 Sandbox 执行：拒绝；违反 fail-closed。
- 仅 Docker Sandbox：作为强制依赖被拒绝；保留为未来可选后端。
- 宣称 Job Object 是文件系统 Sandbox：拒绝；虚假安全声明。