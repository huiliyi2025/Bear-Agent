# ADR 0020：Provider Retry、Fallback 与流式安全边界

- 状态：已采纳
- 日期：2026-11-20

## 背景

Provider 会限流、超时、断连或返回部分流。重试/切换如果发生在已经输出文本或 Tool Call 之后，会造成重复文本、重复工具调用或已执行副作用被重复执行。

## 决策

请求路由为有序 Provider/Model 列表：

- Primary：`agent.default_provider` / `default_model`。
- Fallback：`agent.fallback_models`，格式 `provider:model` 或 `provider`。
- 每个 route 有自己的能力声明（context_window、tools、streaming）、RetryPolicy 与 Circuit Breaker。

Retry：

- 仅在**尚未向消费者产生任何事件**时重试。
- Rate limit / timeout / network / 5xx / malformed response 可重试；invalid request/auth 等按策略。
- 有界 attempts、指数 backoff、jitter、Retry-After；取消立即中止。
- 重试预算按 route 计算，不会与 Runtime 轮次预算相乘。

Fallback：

- 仅在尚未产生任何事件时切换。
- fallbackable：可重试类别 + auth/not_found/context_length；invalid_request 不切换。
- context_length 只切到 context_window 更大的 route。
- 能力检查：请求带 tools 时跳过不支持 tools 的 route；不支持 streaming 时该 route 用非流式请求。
- Circuit Breaker：连续失败达到阈值后打开，cooldown 后半开探测；全部打开时返回结构化错误，避免雪崩和频繁切换。

流式边界：

- 任意 Text/Thinking/ToolCall/Usage/Finish 事件一旦产出，就不再重试/切换；错误标记为 `partial_stream`，Runtime 结束当前请求并保留已发布输出与诊断。
- Tool Call 参数在流结束后由 ModelCaller 校验；参数不完整不会执行工具。
- Provider 切换只发生在“新请求”层面；不会自动重新执行已执行的工具。
- 每个 Model Call 记录实际使用的 provider/model（通过 route ContextVar），便于追溯。

配置与 CLI：

- `provider.max_retries`、retry delays/jitter、能力；`agent.fallback_models`、circuit 参数。
- CLI 提供 provider 配置展示、测试/诊断；密钥/h headers 不输出。
- 健康状态是进程内状态；跨进程仅通过日志/事件/CLI 诊断。

## 影响

- 透明 fallback 只覆盖“尚未输出”的失败；部分流失败必须显式失败，而不是假装前一次输出不存在。
- 不同 Provider 的模型输出格式由 OpenAI-compatible adapter 统一；真正不兼容的 Provider 需要新 adapter。
- Circuit 是进程内启发式，重启后重置。
- 失败路由不会自动回切；cooldown 后按主顺序重新尝试。

## 备选方案

- 任意时刻 fallback：拒绝；会重复文本/工具调用。
- 自动重放已执行工具：拒绝；副作用不可逆。
- 只做单 Provider 重试：拒绝；无法跨服务容错。