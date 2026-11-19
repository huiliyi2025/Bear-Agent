# ADR 0008：精确指纹审批作用域与 keyring 密钥

- 状态：已采纳
- 日期：2026-11-19

## 背景

对每条命令都询问会造成审批疲劳；永久记住“允许 Shell”又不安全。配置文件或日志中的密钥是常见泄露路径。

## 决策

审批以“工具名 + 精确参数”的哈希为键，作用域为：

- once：不持久化；
- session：带 session ID 与过期时间持久化；
- project：带 project ID 与过期时间持久化。

一次授权绝不覆盖其他命令或工具调用。审批决策会写入事件与审计。

密钥通过 `SecretStore` 使用 OS keyring；配置引用为 `secret://name` 或 `env:NAME`。没有明文文件回退。`SecretRedactor` 替换工具输出、事件、审计详情与持久化工具参数中的已知值与常见凭证格式。Shell 环境由 allowlist 构建，不继承进程环境。

## 影响

- 用户可以批准重复的精确命令，而不授权所有命令。
- session/project 授权在过期前可跨恢复继续使用。
- keyring 不可用时 `secret://` 引用 fail-closed；plaintext/file 类型 keyring 后端（包括不安全的 chainer 子后端）会被拒绝。
- 脱敏是 best-effort；未知密钥格式如果从未注册或匹配，仍可能泄露。

## 备选方案

- 全局“允许 Shell”开关：拒绝；范围太宽。
- 命令前缀审批：拒绝；`rm` 前缀会授权所有目标。
- 把明文加密配置当“密钥库”：拒绝；主密钥管理难度不亚于直接使用 OS keyring。