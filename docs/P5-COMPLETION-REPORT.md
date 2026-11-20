# Bear-Agent P5 完成报告

- 阶段：P5 — Hooks / MCP / Plugins / Provider Fallback
- 日期：2026-11-20
- 版本：0.5.0
- 状态：实现与验证完成；按要求未执行 Git commit，未进入 P6。

## 1. 交付内容

### 1.1 Hooks

- 精简生命周期：session/run/turn、model before/after/error、tool before/after/error、task started/completed/failed/blocked/cancelled、plan revised、context compacted、checkpoint created、recovery started/completed、run terminal。
- 观察 Hook 通过 EventBus 映射；`model_before`/`tool_before` 是显式前置拦截点。
- 进程内 Hook：优先级排序、每 Hook 超时、取消传播、fail-open/fail-closed、深度/同事件递归防护。
- 能力边界：`tool_before` 在审批与 Claim 之后，只能额外拒绝，不能 allow/修改参数；`model_before` 只能追加有界、脱敏上下文。
- 外部命令 Hook：无 shell 子进程、JSON stdin/stdout、最小环境、有界输出、超时进程树终止；默认关闭，需 `hooks.allow_external=true`；不能追加上下文。
- Hook 执行事件（started/completed/failed/denied）与审计；CLI `hooks list`。

### 1.2 MCP

- 基于官方 `mcp>=2.3,<3`，不自行实现协议。
- Transport：stdio（最小环境 + 显式 env/cwd）与 Streamable HTTP（HTTPS；loopback 例外；拒绝 URL 凭证；可选 TLS 校验）。
- 生命周期：start/initialize/list_tools/call/stop/reconnect；多 Server 并发启动；required/optional 策略；失败原子清理。
- Tool 接入：`mcp__<server>__<tool>` 命名空间、冲突检测、原始 JSON Schema、jsonschema 本地校验、ToolExecutor 权限/审批/审计/超时/取消、结果转换/脱敏/截断。
- MCP Tool 为 EXEC、非幂等；失败不会自动重试，Task Engine 不会自动重跑执行过 MCP 工具的任务。
- 状态可观察：CLI `mcp status/tools`；不泄露解析后的 secrets。

### 1.3 Plugins

- `plugin.toml` Manifest：ID、PEP 440 版本、`requires_bear`、依赖、权限、Hook/MCP 贡献、可选 Python entrypoint、enabled_by_default。
- PluginManager：发现、依赖拓扑/循环、版本/权限/冲突校验、staging + 一次性提交、失败回滚、启用/禁用状态 JSON、加载/卸载。
- 贡献：外部 Hook 命令、MCP Server、受信任 Python Hook/Tool。
- 安全：Python 默认关闭；需 `plugins.allow_python=true` 且 `permissions.python=true`；Manifest-only 扩展不执行代码；MCP Server 仍需自身 trusted，外部 Hook 仍需 allow_external；进程内 Python 无沙箱风险已文档化。
- CLI `plugins list/info/validate/enable/disable`；示例 Plugin 与开发文档。

### 1.4 Provider Retry / Fallback / Failover

- `FallbackProvider`：有序 Primary/Fallback route、每 route RetryPolicy、能力声明、Circuit Breaker、half-open 探测。
- 错误分类：rate_limit/timeout/network/server_error/malformed_response 可重试；auth/not_found/context_length 可 fallback；invalid_request/tool_format 不重试/不切换。
- Backoff + jitter + Retry-After + 有界 attempts；取消即时中止。
- 流式安全：只有尚未产生任何事件时才重试/切换；一旦输出 Text/Thinking/ToolCall/Usage/Finish，partial stream 以结构化错误结束，绝不重复输出或重放工具。
- 能力兼容：tools/streaming/context window；context_length 只切到更大窗口；fallback route 使用自己的 model；primary 保留请求 model（planner/summarizer/verifier 覆盖）。
- 实际 provider/model 记录到 ModelResponse、model_calls 与事件；CLI `providers show/test`；`BEAR_FALLBACK_MODELS`。

### 1.5 Runtime / CLI 集成

- `Container.startup()`：加载 Plugin → 合并 MCP 配置 → 连接 Server → 注册工具；`Container.close()` 停止 MCP、卸载 Plugin。
- CLI chat/run/task 执行前调用 startup；`/tools` 自动包含 MCP 工具。
- HookManager 订阅 EventBus；TurnExecutor/ToolExecutor 调用前置 Hook。
- TaskEngine、Checkpoint、Recovery、Context Compression 的事件自动成为 Hook 观察事件。
- Provider Router 被 Runtime、Planner、Verifier、Summarizer 共用。

## 2. 验证结果

环境：Windows 11 Home China 10.0.26200，Python 3.13.5，uv。

| 门禁 | 结果 |
|---|---|
| `uv run pytest` | **580 passed, 3 skipped** |
| Coverage | **81%**（hooks/manager 87%、mcp/tools 90%、mcp/manager 85%、plugins/manifest 93%、providers/router 90%、plugins/api 87%） |
| `uv run ruff check src tests` | 通过 |
| `uv run ruff format --check src tests` | 通过 |
| `uv run mypy src` | 通过（110 source files） |
| `uv build` | 通过：`bear_agent-0.5.0.tar.gz` / `bear_agent-0.5.0-py3-none-any.whl` |

### 2.1 真实验证与模拟验证

**真实执行**

- 真实 stdio MCP Server 子进程（python 脚本）与真实 in-process Streamable HTTP MCP Server：发现、调用、错误、超时、取消、停止、重连、多 Server、冲突、ToolExecutor 权限/审批、CLI。
- 真实 Runtime + ToolExecutor + TaskEngine + Provider Fallback（ScriptedProvider/respx）。
- 真实 Hook 外部命令子进程；真实 Plugin（Manifest MCP + Python Tool/Hook）加载。
- Windows SQLite、CLI、config。

**模拟/条件验证**

- 外部真实模型/Provider 网络未验证；使用 ScriptedProvider 与 respx。
- MCP SDK 2.3 在 Windows 验证 stdio/HTTP；其他平台未单独验证。
- 外部 Hook/Python Plugin 的隔离能力取决于 OS 用户权限，未做沙箱验证。
- Partial SSE 通过 fake provider/respx 模拟。

## 3. 定向代码审查

两组独立审查（Provider 流式容错；Hooks/MCP/Plugin 安全）未发现 Critical。修复的 High：

- **Provider：400 context-length 被误分类为 invalid_request** → 解析 400 body/code，正确分类 `context_length` 并允许 fallback；补 HTTP 级测试。
- **Provider：200 SSE error payload 被当成成功** → 检测 `error` 对象，分类 rate_limit/context_length/server_error；避免空回答关闭 Circuit。
- **Provider：half-open probe 在取消时永久卡住** → 在 emit/sleep/取消路径清理 probe_in_flight；补取消回归测试。
- **Hooks/MCP：相对路径与默认 cwd 落在 Agent 可写工作区** → 外部 Hook 与 MCP Server 默认 cwd 改为数据目录下的安全目录；相对脚本/模块不再从工作区解析；补工作区投毒回归测试。
- **tasks run/resume 在无 TTY 时使用 AutoApprover** → 现在默认 DenyingApprover，只有显式 `--yes` 或交互 TTY 才自动/交互审批 EXEC 工具。

已修复 Medium/Low：

- Circuit 不再因 invalid_request/context_length 等客户端错误打开。
- 截断 SSE（无 finish reason/[DONE]）判定 malformed_response。
- 失败 Model Call 也写入 model_calls（status=failed）。
- `providers` CLI 使用 SecretStore；`config show --json` 移除 MCP env/headers。
- 402 与 transport/decoding 错误分类；Retry-After HTTP-date；jitter 配置校验与延迟上限。
- Hook 超时即使 handler 吞掉 Cancellation 也无法绕过；非法 Hook event 启动失败。
- MCP tool description/schema/result/structured 有大小上限，base64 不再整体解码。
- Loopback 校验使用 `ipaddress.is_loopback`，拒绝 `127.0.0.1.attacker.example`。
- Plugin state 损坏 fail-closed；Container startup 失败可重试；MCP CLI 错误使用 Redactor。
- 外部 Hook 命令/非法 event 在启动时 fail-closed；MCP tool description/schema/result 上限；ProviderErrorEvent 不再被当作成功。


## 4. 残余风险

- Python Plugin / 外部命令 Hook 在用户权限下运行，不是沙箱；只提供显式信任开关。
- MCP Server 子进程不受 Bear Sandbox、文件 Claim、Recovery 管理；父进程崩溃后的清理依赖 SDK/OS。
- MCP/Hook 返回内容仍可能包含 Prompt Injection，只按不可信数据/工具输出处理。
- Circuit Breaker 进程内、重启重置；多进程不共享健康状态。
- partial stream 无法透明恢复，只能结束请求并保留诊断。
- 非 OpenAI-compatible Provider 仍需要独立 adapter。
- 真实第三方 Provider 的行为差异（tool call 格式、usage、stream）未全面验证。
- Plugin 状态/审计无签名/来源校验；不自动安装远程插件。

## 5. 下一阶段建议

- P6：Trace/Metrics/Benchmark、Hook/MCP/Provider 指标、故障注入与回放。
- 增加跨平台 MCP/Seatbelt/bwrap 真实验证。
- 评估 Plugin 签名/来源验证与 MCP 进程回收。
- Provider 能力矩阵与真实多厂商兼容测试。