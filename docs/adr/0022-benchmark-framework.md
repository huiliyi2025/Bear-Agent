# ADR 0022：可重复 Benchmark Framework

- 状态：已采纳
- 日期：2026-11-20

## 背景

“打印几个耗时”无法比较版本、Provider、模型或配置。Benchmark 必须明确测什么、在什么环境、用什么输入、是否包含 Setup、如何统计、结果是否可重复，以及真实 Provider 与脚本化 Provider 的差异。

## 决策

### 场景（Scenario）

- 场景在 `BenchmarkRegistry` 注册，必须声明 `id`、名称、类别、`kind`（`synthetic`/`scripted`/`real`）、是否需要网络、默认参数。
- 场景生命周期：
  - `prepare(ctx)`：创建数据库/容器/文件/Plan 等Fixture，**不计时**；
  - `run(ctx)`：唯一计时区间；
  - `cleanup(ctx)`：释放资源，不计时，也不改变结果。
- 每次迭代使用全新的临时 `workspace`/`data_dir`，避免上一轮的行、缓存、文件影响下一轮。用户真实数据目录不被写入（结果库除外）。
- 内置场景覆盖：
  - Kernel：Session 创建、事件写入、Trace/Span 写入、Registry 查询、Context Budget。
  - Tool：read/write/grep 经真实 ToolExecutor；Shell/Sandbox 在能力不可用时 SKIP，而不是降低安全设置。
  - Context：L1/L2/L3、Checkpoint 创建与验证。
  - Task：Plan 生成、调度/完成、修订、崩溃对账。
  - Provider：请求、流装配、工具参数流、Retry、Fallback、Circuit；真实请求单列。
  - MCP：真实 stdio Server 初始化/发现、调用、多 Server、重连。
  - E2E：真实 Runtime 单/多工具、多 Task Plan、Context 压缩、Provider Retry/Fallback、Recovery、MCP Tool、Plugin Hook。

### Runner

- Warmup 迭代执行但不计入统计；统计只使用状态为 `ok` 的样本。
- 记录 Mean/Median/P50/P95/P99/Min/Max/Stdev、Throughput、Ops/s、Error Rate、Token、估算成本。
- 单次异常记录为 error 样本并继续后续迭代；全部失败时运行状态为 `failed`。Fixture 不可用（如无 OS Sandbox、未配置真实 Provider）明确 `skipped`，不伪造 0 耗时。
- 结果包含：
  - Run ID、场景定义、合并后的参数、开始/结束时间；
  - 环境：平台、Python、CPU 数、SQLite、应用版本、Git commit、uv.lock 哈希；
  - 设置快照（脱敏，不含 Key/Headers）；
  - 每轮样本与聚合统计；
  - notes（是否 trace、是否真实网络、统计口径）。
- 结果保存在 `benchmark_runs`（Schema v6），JSON 字段包含完整结果；支持历史查询、导出与比较。
- `compare` 要求同一场景且参数一致；比较 Mean/P50/P95/P99/Max、Throughput、Error Rate，按阈值标记 improvement/stable/regression；环境字段不一致时标记 `compatible_environment=false`。

### 真实与脚本化分离

- `kind=scripted` 使用 ScriptedProvider/假 Provider，结果可重复，不依赖网络。
- `kind=real` 必须显式 `--allow-network`，并已配置真实 Provider；记录真实 Provider/Model、Token 与按配置价格估算的成本。真实结果受网络、模型版本、负载、随机性与价格变化影响，不能与 scripted 结果混为一谈。
- 默认 `--all` 不执行 real 场景（SKIP）。

### Trace 集成

- `--trace` 为每次迭代创建独立 Trace/Span，用于验证可观测路径；这会给计时引入额外开销，结果 notes 明确标注。默认关闭，避免测量污染。

## 安全边界

- Benchmark 不修改生产权限/审批/Claim/Audit/Sandbox 默认值来“跑得更快”。
- 场景在独立临时目录运行；真实 Runtime 场景仍经过 ToolExecutor、PermissionEngine、审批、Claim、Audit 与 Sandbox。Fixture 使用 AutoApprover 仅用于无人值守的临时 Benchmark，配置会写入结果快照。
- Shell/Sandbox 场景在安全能力不可用时 SKIP；绝不为了产出数字而切换 `sandbox.mode=off`。
- 结果与参数经 Redactor；不保存 Prompt、模型输出、工具结果或密钥。
- Benchmark Storage 与 Agent 运行共享 SQLite，但场景数据目录独立；结果写入失败只影响 Benchmark。

## 影响

- 统计是单机、单进程、短时间样本；不等于生产负载或统计显著性检验。
- JIT/OS 缓存/磁盘状态仍会影响结果；Warmup 与多迭代只能缓解。
- SQLite Benchmark 结果会增长；需按策略清理（当前 CLI 未提供自动清理，保留在数据目录）。
- 真实 Provider Benchmark 可能产生费用，必须显式授权。
- `Benchmark` 的“成功”是场景可执行且无异常，不代表 Agent 任务质量；质量评估属于后续 Eval 套件。

## 备选方案

- 只跑一次并输出平均值：拒绝；无法反映尾延迟与错误率。
- 把真实 Provider 与 Fake Provider 混在同一基线：拒绝；不可比较。
- 直接调用内部函数避开 ToolExecutor/审批：拒绝；测到的不是生产路径。