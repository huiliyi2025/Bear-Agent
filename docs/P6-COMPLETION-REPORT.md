# Bear-Agent P6 完成报告

- 阶段：P6 — Observability / Trace / Metrics / Diagnostics / Benchmark
- 日期：2026-11-20
- 版本：0.6.0
- 状态：实现、测试、审查与文档完成；按要求未执行 Git commit，未进入 P7。

## 1. 交付内容

### 1.1 Trace

- Schema v6：`traces`、`spans`；事件继续使用已有 `events.span_id`/`parent_span_id` 关联，不复制 EventBus、Audit、Model Call 或 Tool Call 事实。
- 根 Trace/Span 生命周期；根 Span 在 Trace 开始时写入 `running` 行，崩溃后可见且不会产生永久孤儿。
- 父子 Span、异步 ContextVar 传播、并发隔离；错误/取消状态；批量 Span 写入（默认 32）。
- 接入 Session/Run/Turn/Task/Plan Revision/Model Call/Provider Attempt/Retry/Fallback/Tool Call/MCP/Hook/Compaction/Checkpoint/Recovery/Fork。
- 属性只记录元数据并有字节上限；默认不记录 Prompt、模型输出、工具参数/结果；导出进一步移除 `content_preview`、`arguments_preview`、`summary`、`evidence`、`goal`、`title`、`reason`、`unmet` 等字段。
- Trace 写入失败被隔离，不失败 Run/Tool/审批/Claim/审计/恢复。
- 查询/导出/统计/保留裁剪/不完整 Trace 识别；`bear trace list/show/run/stats/export/prune`。

### 1.2 Metrics

- 没有 Metric Sample 表；按需聚合既有事实：events、model_calls、tool_calls、tasks/task_attempts/plans/plan_revisions、compactions/context_anchors、checkpoints、traces。
- Run/Task/Provider/Model/Tool/Context/Recovery/System 指标：成功率、失败/取消/阻塞、P50/P95/P99、Token/缓存 Token、Usage 缺失、可配置价格成本估算、Retry/Fallback/Circuit、审批等待、权限拒绝、Sandbox、Hook、MCP 调用/配置、Plugin 配置、Shell、Fencing/Claim/未知工具、SQLite/存储/进程资源；MCP/Plugin 生命周期历史标记为不可用。
- 查询按时间窗口（UTC 归一化）与 Session 过滤；事件行上限 20 万并标记下界；Session/Run/Tool Call ID 不作为 Label。
- 进程内 Counter/Gauge/Histogram Registry 与 EventBus LiveMetricsCollector；标签基数上限与 `_overflow` 折叠。
- `metrics_enabled=false`/`observability.enabled=false` 会禁止报告与实时采集；CLI 明确报错。
- `bear metrics show/providers/tools/tasks/recovery/export`；交互 `/metrics`。
- 成本来自 `[observability.pricing]`，缺失价格报告 `pricing_missing`；估算不是账单。

### 1.3 Diagnostics

- 结构化 Check（PASS/WARN/FAIL/SKIP，`static`/`active`）。
- 配置版本、数据目录、磁盘、SQLite quick_check/Schema/WAL、审计链、Checkpoint、Recovery backlog、Provider 配置/能力、MCP 配置/命令、Plugin Manifest/依赖、Hook、Sandbox/Shell、SecretStore、Trace/Metrics/Benchmark 存储与保留策略、环境依赖。
- `--active` 才发送最小 Provider 请求或启动 MCP Server；`--category` 同时限制主动检查范围。
- `bear doctor --json --export --active --category`。
- 报告自由文本与 metadata 经 Redactor；Provider/MCP 配置的密钥值注册到 Redactor；URL 用户信息被移除；不输出 Headers/Secret。

### 1.4 Benchmark

- Scenario Registry、Warmup、迭代、独立临时 workspace/data_dir、精确统计（Mean/Median/P50/P95/P99/Min/Max/Stdev、Throughput、Ops/s、Error Rate、Token、Cost）、Run ID、持久化、历史与比较、JSON 导出。
- 43 个内置场景：Kernel、Tool、Context、Task、Provider、MCP、E2E；真实 stdio MCP Server、真实 Runtime/ToolExecutor/TaskEngine、真实 Plugin Hook；真实 Provider 场景需显式 `--allow-network`。
- Benchmark Profile 默认关闭 Harness Trace/Metrics，避免污染计时；`--trace` 为显式开销。
- 比较要求同一场景、同参数、同迭代数、双方 completed 且有成功样本；阈值必须有限非负；失败/跳过/零样本拒绝比较。
- `bear bench list/run/report/history/compare/export/prune`；`--no-save` 不打开数据库。
- 结果记录环境、Git commit/dirty、uv.lock 哈希、设置快照与 settings hash；base_url 脱敏。

### 1.5 集成与文档

- Container 构造并生命周期管理 TraceService、MetricsService、LiveMetricsCollector；启动时恢复不完整 Trace、按保留策略裁剪；关闭时按顺序隔离 Teardown，始终关闭 Trace/DB。
- Trace 覆盖 Runtime/Turn/Task/Provider/Tool/Hook/MCP/Context/Checkpoint/Recovery/Fork；事件与消息自动带当前 Span。
- Schema v6 索引覆盖 Session/Run/Task/Status/时间、Span Parent、事件 span、Metrics 时间过滤。
- 配置 `[observability]`（采样/批量/保留/价格）与 `[benchmarks]`（迭代/Warmup/保留/比较阈值）；`config_version=1`。
- ADR 0021/0022；ROADMAP、CHANGELOG、ARCHITECTURE、SECURITY、DEVELOPMENT、README 更新。

## 2. 验证结果

环境：Windows 11 Home China 10.0.26200，Python 3.13.5，uv；Linux 验证使用 WSL Ubuntu 24.04 + Python 3.13.18 + 真实 bubblewrap 0.9.0。

| 门禁 | 结果 |
|---|---|
| `uv run pytest` | **623 passed, 3 skipped** |
| Coverage | **80%** |
| `uv run ruff check src tests` | 通过 |
| `uv run ruff format --check src tests` | 通过（204 files formatted） |
| `uv run mypy src` | 通过（137 source files） |
| `uv sync --frozen` | 通过 |
| `uv build` | 通过：`bear_agent-0.6.0.tar.gz` / `bear_agent-0.6.0-py3-none-any.whl` |
| CLI smoke | `bear --version` = 0.6.0；43 个场景可列出 |

跳过项：Linux bwrap 执行测试（Windows）、符号链接逃逸（主机权限）、POSIX FIFO 测试。

### 2.1 跨平台

- **Windows 11**：完整测试、CLI、Benchmark、真实 stdio MCP、Plugin Hook、Trace/Metrics/Diagnostics 全部通过；Windows Shell 在 `sandbox.mode=require` 下按设计不可用，相关场景 SKIP。
- **Linux（WSL Ubuntu 24.04）**：完整测试通过；使用真实 `bubblewrap 0.9.0` 运行 bwrap 文件系统隔离测试（包括根路径词法拒绝与私有 `/tmp` 语义）。
- **macOS**：本机不可用；未运行 Seatbelt 实机测试。GitHub Actions 在 P6 前的 CI 修复提交 `024501a` 上已三平台全绿；P6 工作区未提交，因此 macOS 尚未验证 P6 改动，需提交后由 CI 给出实际结果。

### 2.2 真实验证与模拟验证

**真实执行**

- Windows/Linux SQLite 多连接、真实 Runtime/TurnExecutor/ToolExecutor/TaskEngine/Provider Router。
- 真实 stdio MCP Server 子进程、真实 in-process Streamable HTTP MCP Server、真实 Plugin Hook 子进程。
- 真实 Trace/Metrics/Diagnostics/Benchmark 数据链路；真实 bubblewrap 沙箱。
- 真实 `uv build` 与安装包版本。

**模拟/条件验证**

- 真实外部模型 Provider 网络未验证；Provider 场景使用 ScriptedProvider/respx。
- macOS Seatbelt 未实机验证。
- 非 OpenAI-compatible Provider 仍需 adapter；真实第三方 Provider 的 tool/usage/stream 差异未全面验证。
- Git 工作区脏时 `git_dirty=true`，Benchmark 结果仍绑定 commit 但无法区分未提交改动。

### 2.3 性能测量（Windows，5 次迭代）

- `kernel.trace_write`（200 spans）：Mean 9.4 ms，P95 10.0 ms；禁用 Trace 的同循环 Mean 0.37 ms；约 **0.05 ms/span**。
- `e2e.single_tool`（5 次完整 Run）：Mean 162 ms，P95 167 ms，Token 750。
- `kernel.session_create`（100 sessions）：Mean 117 ms。
- `kernel.context_budget`（200 次构建）：Mean 107 ms。
- Metrics：2,000 model calls + 2,000 tool calls 查询 < 5 s（测试上限）；事件查询上限 20 万并标记下界。

## 3. 定向代码审查

进行了三组独立只读审查：Trace/Metrics/Diagnostics、Benchmark/CLI、Runtime/扩展集成。发现并修复：

### Critical / High

- **取消发生在 Trace 初始化期间导致心跳/租约僵尸**：Run/Plan Run 将 Trace 进入放入受保护范围；取消时停止心跳、重置 Fence、清理 `_active`、释放租约；补回归测试。
- **Trace 收尾取消丢失 Span/留下 running 行**：根 Span 开始时写入 running 行；flush 批次在取消时恢复；完成状态只在持久化成功后设置；`close()` 重试未完成 Trace；补回归测试。
- **Provider attempt Span 跨越 yield 导致 GeneratorExit 被误判为 Provider 失败/打开 Circuit**：改为显式 Span 生命周期，不跨 yield 设置/恢复 ContextVar；保留 GeneratorExit 传播；补早退回归测试。
- **`run_plan` 清理/ContextVar 泄漏**：提前重置 Fence/清理 `_active`，清理放入保证执行的 `finally`，不吞 Cancellation；Trace 关闭不再 `suppress(BaseException)`。
- **Doctor 泄露 URL 凭证/Provider/MCP 错误文本**：注册已解析密钥、剥离 URL userinfo、所有活动检查与 MCP metadata 经 Redactor；补脱敏测试。
- **Trace 导出遗漏任务 goal/title/reason/unmet 等内容字段**：扩展内容字段过滤；补导出测试。
- **Benchmark compare 接受失败/跳过/零样本/不同迭代数**：增加 completed、成功样本、迭代数、参数、阈值校验；拒绝无效比较。
- **JSON 模式混入人类可读输出**：`--json` 与 `--output/--export` 不再打印成功提示；禁用 Benchmark 时输出结构化 skipped。
- **Benchmark 容器默认开启 Trace/Metrics，污染计时**：Benchmark Profile 默认关闭；`--trace` 显式开启；结果记录 observability 设置与 settings hash。
- **`kernel.trace_write` 在 Trace 关闭/采样时静默成功**：改为 SKIP；运行后校验实际 span 数。
- **MCP initialize/multi-server 将关闭时间计入计时/失败误报**：清理移入 cleanup，启动失败 SKIP；工具名/回声校验。
- **E2E 场景只看 Run COMPLETED**：增加成功工具事件、MCP 工具完成、Hook COMPLETED/HOOK_FAILED 拒绝、Fallback 实际路由断言。
- **Metrics flags 未生效**：report/CLI/live collector 统一受 `observability.enabled`/`metrics_enabled` 控制；补测试。
- **Container.close 可能跳过 Trace/DB 清理**：各 Teardown 隔离，Trace/DB 在 finally 关闭；`close()` 幂等。
- **Trace 写入异常仍可能失败 Tool/Run**：start/flush/row 构建/end 全部隔离，属性序列化失败丢弃。
- **Task Span 总是 ok**：按完成/失败/阻塞/取消设置状态。

### Medium（已修复）

- UTC 时间窗口归一化；`--since all` 表示全部历史。
- Metrics 时间索引；MCP/Plugin 生命周期历史标记为不可用并提供配置状态，避免静默假零。
- Shell/工具 failed 计数排除 running/unknown；unknown 单独统计。
- Diagnostics 负值 sentinel 视为 WARN；缺失依赖 FAIL。
- `--category` 限制 active 副作用。
- 保留裁剪跳过 running Trace。
- 默认参数、note、metrics snapshot、设置快照、base_url 脱敏；benchmark 结果持久化失败不丢弃测量。
- Throughput 使用全部迭代 wall time，并保留成功-only throughput。
- 未知场景/空 category 明确失败；`--iterations 0` 拒绝；阈值 0/NaN/负数校验。
- 每次迭代使用隔离 Settings 深拷贝；BenchmarkContext 设置不再指向生产数据目录。
- 真实 Provider 缺失价格报告 `pricing_missing`。
- Trace 导出事件、Span 错误文本先脱敏后截断。

审查中未发现未修复的 Critical/High；Low 项作为残余风险记录。

## 4. 残余风险

- Trace 属性由调用点约定；新增 instrumentation 必须继续只记录元数据，代码审查与导出清洗是防线。
- MCP/Plugin 生命周期目前没有 session 作用域的持久事件源；Metrics 只提供工具调用与配置状态，历史连接/加载次数标为不可用。实时状态使用 `bear mcp status`、`bear plugins list` 与 `doctor --active`。
- Metrics 按需聚合，任务/压缩/Checkpoint 查询仍有内存上限风险；事件查询 20 万行后为下界。
- Trace 数据本地 SQLite、无静态加密/外部完整性锚点；拥有数据库权限者可读取。
- Trace 保留裁剪在 Container 启动时与手动 `trace prune` 执行；长进程不会周期裁剪，running Trace 不被裁剪。
- Fencing 拒绝计数为进程内；重启重置。
- Cost 是配置价格估算，不是 Provider 账单；缓存计价与四舍五入因 Provider 而异。
- Benchmark 为单机、短样本；不代表生产吞吐/统计显著性；真实 Provider 受网络、模型版本、价格影响。
- 真实外部模型 Provider、macOS Seatbelt 未在本机验证；P6 改动未提交，macOS CI 结果待提交后确认。
- Python Plugin/外部 Hook 仍不是沙箱；MCP 子进程不受 Bear Sandbox/Claim/Recovery 管理。

## 5. 下一阶段建议

- P7：全屏 TUI、Trace/Metrics 面板、打包/发布工程。
- 增加 macOS Seatbelt 实机与真实多厂商 Provider 兼容矩阵。
- 为 MCP/Plugin 生命周期增加明确的作用域（系统事件存储）或只在 TUI 显示实时状态。
- 评估 Trace/Metrics 数据加密与外部完整性锚点。