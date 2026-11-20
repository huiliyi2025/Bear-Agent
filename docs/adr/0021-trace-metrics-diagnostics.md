# ADR 0021：Trace、Metrics 与 Diagnostics 的职责边界

- 状态：已采纳
- 日期：2026-11-20

## 背景

Agent Harness 已经拥有 EventBus、append-only 事件、Audit 哈希链、`model_calls`、`tool_calls`、Task/Attempt、Compaction、Checkpoint、Recovery 等事实记录。P6 需要回答“一次 Run 经历了什么、时间花在哪里、成功率如何”，但不能把这些事实再复制成一套平行数据库，也不能让遥测阻塞或绕过安全决策。

## 决策

### Trace = 路径，不是第二套事实

- 一个 Trace 对应一次可诊断操作（普通 Run 或 Plan Run）。Span 是其中的计时操作：turn、task、model call、provider attempt/retry/fallback、tool call、MCP call、hook、compaction、checkpoint、recovery、fork。
- Trace 只新增 `traces` 与 `spans` 两张表（Schema v6）。它不复制 Transcript、Event、Audit、Model Call 或 Tool Call：
  - 事件行继续使用已有 `span_id`/`parent_span_id` 列；
  - `span.attributes` 只保存计数、ID、状态、Provider/Model、错误类别等元数据；
  - 查询/导出时通过 `spans.span_id` join 已有事件。
- 根 Trace 行在操作开始时插入，因此进程崩溃会留下 `running`/`incomplete` 可见状态，而不是静默缺失。Span 在内存中批量写入（默认 32 个），减少 SQLite 写锁竞争。
- Trace 写入不参与租约 fencing，也不是安全记录。任何写入失败只记录日志并丢弃该批 Span；Run、Tool、Approval、Claim、Recovery 继续执行。Trace 表不参与权限判断。
- 属性统一脱敏、截断，并有总字节上限。默认**不记录** Prompt、模型输出、工具参数、工具结果或完整事件内容。导出事件时还会移除 `content_preview`、`arguments_preview`、`summary`、`evidence` 等字段，避免 Trace 成为第二条更完整的数据泄露路径。
- 并发/异步通过 ContextVar 传播 Trace/Span 上下文；每个 asyncio Task 继承自己的父 Span，不会串到其他 Session。取消与异常分别标记 `cancelled`/`error`。
- 保留策略：按天数与最大 Trace 数裁剪；启动时把超过宽限期的 `running` Trace 标记为 `incomplete`，不自动关闭可能仍被其他进程写入的 Trace。

### Metrics = 对权威事实的按需聚合

- 不新增 Metric Sample 表。MetricsService 在查询时聚合：
  - `events`：Run/Task 状态、Provider retry/fallback/circuit、审批等待、权限拒绝、Sandbox、Hook、Recovery、Checkpoint、Fork、Claim 冲突；
  - `model_calls`：Provider/Model、延迟、Token、失败；
  - `tool_calls`：工具分布、延迟、超时/取消/未知；
  - `tasks`/`task_attempts`/`plans`/`plan_revisions`：完成率、重试、Blocked、修订；
  - `compactions`/`context_anchors`：压缩层级、前后 Token、Artifact、Anchor；
  - `traces`：Trace 总量与不完整数。
- 因此 Metrics 的额外写入开销是 0；查询按时间窗口与会话过滤，并设置 20 万行上限，超限明确标记 `truncated=true`（计数是下界）。
- 统计使用精确样本计算 Mean/Median/P50/P95/P99/Min/Max/Stdev；不伪造百分位。按 Provider/Model/Tool/Task 分组；Session/Run/Tool Call ID 只作为行过滤，不作为 Metrics Label。
- 成本估算来自 `[observability.pricing]` 配置（Provider:Model、Model、Provider:*、* 优先级），缓存 Token 单独计价；无价格时报告 `pricing_missing`，绝不编造成本。估算不是账单。
- 进程内 `MetricsRegistry` 提供 Counter/Gauge/Histogram，用于实时 Surface、Benchmark 和性能测量；标签键/值/序列数有上限，溢出折叠为 `_overflow`。它不持久化。
- 系统指标：SQLite 文件/WAL/Pragma、Sessions/Claims/Traces/Benchmark 计数、进程内存（Windows `GetProcessMemoryInfo`、POSIX `getrusage`）与 CPU 时间；平台无法可靠读取时返回 unavailable，而不是 0。

### Diagnostics = 分隔静态检查与主动探测

- `DiagnosticsService` 输出结构化 Check（PASS/WARN/FAIL/SKIP），每条带 category、mode、summary、detail、metadata。
- 默认 `static`：配置版本、数据目录、SQLite `quick_check`/Schema、审计链、Checkpoint 校验、Recovery backlog、Provider 配置/能力、MCP 配置/命令存在性、Plugin Manifest/依赖、Hook 配置、Sandbox/Shell 能力、Trace/Metrics/Benchmark 存储、保留策略。
- `--active` 才执行 Provider 最小请求与 MCP Server 启动；会明确标记 ACTIVE，并提示可能产生费用/执行配置命令。Doctor 默认不启动服务、不执行 Hook、不调用模型。
- 报告可 JSON 输出/导出，所有自由文本错误经 Redactor，绝不输出 API Key/Headers/Secret。

### CLI

- `bear trace list|show|run|stats|export|prune`：Span 树、事件链接、按 Run 还原、脱敏导出、保留裁剪。
- `bear metrics show|providers|tools|tasks|recovery|export`：聚合报告与导出。
- `bear doctor --json|--export|--active|--category`。
- `bear bench list|run|report|history|compare|export`（见 ADR 0022）。

## 影响

- Trace 与 Metrics 查询读取同一 SQLite；长窗口全量查询可能较慢，但不会影响运行写路径。索引覆盖 Session/Run/Task/Status/时间与 Span Parent。
- 不保存逐指标样本意味着无法回放任意历史 Label 组合；需要时从事实表重新聚合。
- Trace 的属性由调用点决定，必须遵守“只记录元数据”的约定；代码审查与导出清洗是防线。
- 进程内 Circuit/Fence 统计重启后重置，报告会明确标注。
- Diagnostics 的静态检查可能把“需要主动连接才能发现”的问题报为 PASS/WARN；`--active` 是显式选择。

## 备选方案

- 每 Span/每指标同步写 SQLite：拒绝；增加关键路径延迟与锁竞争。
- 独立 Trace 事件表：拒绝；与 EventBus 重复且容易不一致。
- 把 Session/Run ID 作为 Metrics Label：拒绝；高基数会打爆内存与查询。
- Doctor 默认连接 Provider/MCP：拒绝；可能产生费用、执行不受信任命令、改变外部状态。