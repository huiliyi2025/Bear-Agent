# 开发指南

## 环境准备

```bash
git clone https://github.com/huiliyi2025/Bear-Agent.git
cd Bear-Agent
uv sync
```

需要 Python 3.13+。`uv sync` 会根据 `pyproject.toml` / `uv.lock` 安装运行时与开发依赖。

## 质量门禁

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest
```

运行单个测试：

```bash
uv run pytest tests/unit/test_storage.py -v
uv run pytest tests/integration/test_runtime.py -k cancel -v
```

测试使用脚本化 Provider 与临时数据目录，绝不能调用真实模型、网络或用户的 `~/.config`。

## 目录结构

```text
src/bear_agent/
  core/          消息分段、事件、取消、错误、用量、枚举
  config/        Settings 模型、TOML 加载器、初始化模板
  storage/       Database、迁移、Repository
  sessions/      SessionRecord、SessionService、转录 + 租约
  providers/     ModelProvider 端口、OpenAI 兼容适配器、重试
  tools/         Tool 契约、注册表、执行器、builtin/（含 shell）
  shell/         命令策略、进程运行器、环境、sandbox/
  policy/        PermissionEngine、PathGuard、审批服务、approver 端口
  secrets/       keyring 存储、密钥引用、脱敏
  audit/         哈希链安全日志
  context/       System Prompt、Token 估算、ContextBuilder
  runtime/       AgentRuntime、TurnExecutor、ModelCaller、StopPolicy
  observability/ EventBus、EventRecorder
  ui/cli/        Typer 命令、Rich 渲染器、CLI approver
  bootstrap.py   组合根
tests/
  unit/ integration/ security/ e2e/ support.py conftest.py
```

## 依赖规则

- `core` 不依赖包内任何其他模块。
- 内核模块可以 import `core`，但不能 import `ui`。
- `tools` 不能 import `runtime`。
- `policy` 不能 import `tools.base`（使用结构化 `ToolLike`）。
- `shell` 可以 import `config`、`core` 及其 Sandbox 适配器；不能 import `runtime` 或 `ui`。
- `audit` 与 `secrets` 不能 import `ui`；它们是安全服务。
- `sessions` 不能 import `ui` 或 providers。
- `bootstrap.py` 是唯一构造具体适配器的地方。

快速检查：

```bash
rg "from bear_agent\.(ui|runtime)" src/bear_agent/{core,tools,policy,sessions}
```

如果功能需要新的跨模块依赖，请在 `core` 增加端口，或在消费模块中定义小协议，而不是 import 具体适配器。

## 添加工具

1. 用 `Field(description=...)` 定义参数 `BaseModel`。
2. 继承 `Tool`，设置 `name`、`description`、`args_model`、`side_effect`、`idempotent`、`concurrency_safe`、`timeout_s`。
3. 实现 `async def run(self, args, ctx) -> ToolResult`。
4. 所有路径都通过 `ctx.path_guard` 解析（`mode="read"` / `"write"`）。
5. 有副作用的工具必须实现 `claim_paths(args)`，以串行化跨会话写入；返回 `ClaimRequest(path, scope="file"|"tree")`。在实际变更前调用 `ctx.assert_write_allowed(path)`（如果已设置），在审批后再次检查 Claim 与 session lease，缩小 check-to-write 窗口。
6. 需要分析命令/参数的工具实现 `preflight(args, ctx)`，返回 allow/ask/deny。Preflight 在审批前运行；deny 永远优先。Shell 是参考实现。
7. 返回结构化错误（`is_error=True`、`error_kind=...`）；预期内的用户错误不要抛异常。
8. 在 `tools/builtin/__init__.py` 注册（以后可改为插件）。
9. 为成功、非法参数、路径拒绝、超时以及所有安全边界补充测试；有副作用的工具还要增加审计安全的集成测试。

示例骨架：

```python
class MyArgs(BaseModel):
    path: str = Field(description="File path.")

class MyTool(Tool):
    name = "my_tool"
    description = "Does one thing."
    args_model = MyArgs
    side_effect = ToolSideEffect.READ

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MyArgs)
        path = ctx.path_guard.resolve(args.path, mode="read")
        return ToolResult(content=f"path={path}")
```

## Shell、Sandbox、审批、审计与 Claim

- 命令策略位于 `shell/policy.py`，由 `BashTool` 通过 `Tool.preflight` 调用。它默认保守：未知构造需要询问。
- `shell/runner.py` 负责进程启动、有界读取、超时和进程树终止。不要在工具中直接 spawn Shell 命令。
- Sandbox 适配器位于 `shell/sandbox/`。`SandboxManager` 负责模式选择与 fail-closed 行为。新增后端需实现 `Sandbox`、如实声明 capabilities，并补充构造测试。
- 审批由 `policy/approvals.py` 处理。工具中绝不能绕过 `ApprovalService`；executor 在副作用前调用它。
- 审计记录由 `AuditService` 写入。安全相关动作必须在执行前审计；审计失败会中止动作。
- Claim 由 `sessions/claims.py` 管理；Run 心跳续租，Runtime 在 `finally` 释放。写已 Claim 的路径前必须 `assert_owned`。
- 密钥来自 `secrets/store.py`；绝不能写明文回退或记录解析后的值。已知值应注册到 `SecretRedactor`。

### 调试安全路径

```bash
bear doctor                 # Sandbox/keyring/审计状态
bear audit verify           # 哈希链完整性
bear audit tail             # 最近安全决策
bear secret list            # 只看名称，不显示值
```

平台测试：

- Linux：`bwrap` 可用时运行真实 bwrap 集成测试；CI 会安装它。
- Windows：Job Object 执行测试在 Windows 上运行。
- macOS：Seatbelt profile 构造测试全平台运行；实际执行依赖环境。

## 添加 Provider

1. 实现 `ModelProvider.stream()`，返回 `ModelEvent` 异步生成器（`TextDelta`、`ToolCallStart`、`ToolCallArgumentsDelta`、`UsageEvent`、`FinishEvent`）。
2. 把 `input_tokens` 归一化为 cache-inclusive 语义。
3. 抛出带 category 与 `retryable` 标记的 `ProviderError`。
4. 用录制好的 SSE/JSON fixture 增加一致性测试。
5. 在 `providers/registry.py` 接入；Runtime 绝不能直接 import 具体 Provider。

## 添加命令

CLI 命令是薄适配器。业务逻辑放在 Service 或 Runtime；命令只负责加载配置、构建 Container、订阅渲染器并调用服务。退出码使用 `typer.Exit(code=...)`。

## 调试

- `bear doctor` 检查配置、数据目录、数据库、Provider 与工具。
- `bear sessions show <id>` 打印转录与用量。
- `bear run "..." --json` 把每个事件输出为 NDJSON。
- SQLite 数据库位于 `<data_dir>/bear.db`，可用 `sqlite3` 检查：
  `sqlite3 ~/.local/share/bear-agent/bear.db "select * from sessions;"`。
- 事件 payload 为 JSON；`model.request` 只携带 prompt hash，不记录完整 Prompt，避免日志过大和内容泄露。
- 用 `BEAR_DATA_DIR` 隔离实验数据。

## 数据库迁移

- Schema 版本保存在 `schema_meta`。
- 在 `storage/migrations.py` 增加迁移块，递增 `SCHEMA_VERSION`，并保持旧版本可读。
- 不要原地修改已经发布的迁移。
- 迁移失败必须中止启动并保留备份，不能静默继续。

## 提交卫生

- 不要提交 API Key、`.env`、本地数据库或生成产物。
- 改动保持在当前阶段范围内。
- 修复应附带回归测试。
- 行为或范围变化时同步更新文档。