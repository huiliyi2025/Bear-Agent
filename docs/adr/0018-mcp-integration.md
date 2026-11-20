# ADR 0018：MCP Client 集成与信任边界

- 状态：已采纳
- 日期：2026-11-20

## 背景

MCP 允许接入外部 Tool Server，但 MCP Server 是独立进程/服务，工具具有未知副作用；其返回内容是不可信输入。

## 决策

使用官方 Python SDK（`mcp>=2.3,<3`），不自行实现协议栈。支持：

- **stdio**：配置 command/args/env/cwd，SDK 管理子进程与协议。
- **Streamable HTTP**：HTTPS；仅 loopback 允许明文 HTTP，便于本地开发；拒绝 URL 内嵌凭证。

生命周期：`start` 连接/初始化/能力协商/list_tools → 注册工具；执行中失败不自动重试工具调用；`reconnect(server)` 重新连接并替换该 server 的旧工具；`stop` 关闭连接并注销工具。Required server 启动失败使容器启动失败；optional server 记录错误但不注册工具。

工具接入：

- 名称空间 `mcp__<server>__<tool>`，冲突/重复注册拒绝。
- 原始 JSON Schema 作为 `ToolSpec`；执行前用 jsonschema 校验参数，非法参数不发送给 Server。
- `side_effect=EXEC`，`idempotent=false`：必须经过 PermissionEngine/审批；Task Engine 不会自动重试执行过 MCP 工具的任务。
- 调用经过 ToolExecutor，因此 Claim 之外的 Audit/Timeout/Cancel/结果脱敏仍生效。
- 结果 text/image/resource/structuredContent 转换为 ToolResult；错误映射为 `mcp_tool_error`；内容长度受 ToolExecutor 上限约束。
- MCP 返回内容按不可信工具输出处理，不进入系统提示，不作为权限/策略依据。

安全：

- 每个 enabled Server 必须显式 `trusted=true`；默认没有 Server。
- stdio 使用最小环境 + 显式 env；不经过 shell。
- HTTP 使用 https 与可选 TLS 校验；防止配置内嵌凭证。
- server 提供的 schema/description 属于不可信输入；只用于参数校验与模型可见描述。
- MCP 子进程不受 Bear Shell Sandbox 约束，也无文件 Claim；这是文档化边界。Recovery 不跟踪 MCP 子进程，依赖 SDK/OS 在父进程退出时关闭管道。

## 影响

- MCP 工具在 supervised/auto 下需要审批；unrestricted 仍可执行，但受硬安全策略外的最小约束。
- Server 断开时当前调用失败；下一次调用前需要重连，不在调用内自动重放。
- 工具 Schema 变化通过 reconnect 更新，旧工具先注销，避免旧 schema 执行。
- 多 Server 以名称空间隔离，冲突在启动时失败。

## 备选方案

- 把所有 MCP 工具包装成内置工具绕过 Executor：拒绝。
- 自动重试 MCP 调用：拒绝；副作用未知。
- 允许未 trusted Server：拒绝；默认关闭。