# 贡献指南

感谢参与 Bear-Agent。项目按阶段推进；请让改动与当前阶段一致，不要把推迟的功能说成已完成。

## 开始之前

- 阅读 `docs/ARCHITECTURE.md`、`docs/ROADMAP.md` 与 `docs/SECURITY.md`。
- 在 `docs/ROADMAP.md` 确认当前阶段。
- 修复 Bug 时先补回归测试。
- 安全改动必须附带能证明边界的测试。

## 开发环境

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

## Pull Request

1. 改动保持聚焦，避免无关重构。
2. 新增或更新测试。没有测试的新行为不算完成。
3. 范围或行为变化时更新文档与 `ROADMAP.md`。
4. 不要提交 `.env`、API Key、本地数据库、生成缓存或用户特定路径。
5. 不要为了让测试通过而削弱安全边界。例外必须在 PR 中解释。
6. 没有明确需要不要在 PR 中新增依赖。

## 代码风格

- Python 3.13，`from __future__ import annotations`，公共 API 必须有类型标注。
- 跨边界数据使用 Pydantic v2 模型；内部 value object 在不需要校验时用 dataclass。
- I/O 使用 async；协程中不得直接阻塞事件循环做文件/网络操作，应使用 `asyncio.to_thread` 或注入适配器。
- 显式 SQL 优于 ORM 魔法；多行不变量使用事务。
- 注释解释**为什么**与不变量，不复述代码。
- 错误信息应可行动且安全（默认不把密钥或堆栈返回给模型）。

## 添加工具

见 `docs/DEVELOPMENT.md#adding-a-tool`。必须覆盖：

- 成功路径；
- 非法参数；
- 路径拒绝（如果访问文件系统）；
- 超时/取消行为；
- 有副作用时的事件/tool_call 持久化。

## 添加 Provider

见 `docs/DEVELOPMENT.md#adding-a-provider`。Provider 必须：

- 实现 `ModelProvider` 端口；
- 归一化 usage；
- 分类可重试失败；
- 不在错误中泄露 API Key；
- 通过基于 fixture 的一致性测试。

## 架构规则

- `core` 不依赖 Harness。
- `ui` 可以向内依赖；内核绝不能 import `ui`。
- `tools` 不能 import `runtime`。
- `policy` 不能 import `tools.base`（使用 `ToolLike` 协议）。
- 适配器只在 `bootstrap.py` 构造。
- 改变边界的跨层依赖需要 ADR。

## 安全改动

- 有副作用的工具需要 `preflight` 分析、`claim_paths`、审计记录与安全测试。
- 绝不在 `shell/runner.py` 之外 spawn Shell 命令。
- 绝不增加明文密钥回退；使用 `SecretManager` 并把值注册到脱敏器。
- 安全决策必须在副作用前持久化；执行前审计失败会中止动作。
- 不要把仅进程级 Sandbox 描述成文件系统隔离。

## Commit Message

使用简短祈使句标题，正文解释原因与验证内容。示例：

```text
Add bounded tool executor retries

Tool timeouts now return a structured error and persist a failed tool_call row.
Tests cover timeout, denial and unknown-tool paths.
```

## 报告问题

请包含命令、配置形态（已脱敏）、期望行为与实际行为，以及相关 event/session id。绝不要粘贴 API Key 或私有源码。

## 许可证

参与贡献即表示你同意以 Apache-2.0 授权你的贡献（见 `LICENSE`）。