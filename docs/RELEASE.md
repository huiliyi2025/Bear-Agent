# Release Engineering

## Versioning

- `pyproject.toml` 与 `bear_agent.__version__` 必须一致；`uv.lock` 中的项目版本随 `uv lock` 更新。
- 当前版本：`0.7.0`。
- 阶段提交使用 `阶段 N：...` 消息；发布前运行完整门禁。

## 本地构建与安装验证

```bash
uv sync --frozen
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest --cov=bear_agent --cov-report=term
uv build
```

产物：

```text
dist/bear_agent-0.7.0.tar.gz
dist/bear_agent-0.7.0-py3-none-any.whl
```

安装到隔离环境：

```bash
uv venv /tmp/bear-release-venv
uv pip install --python /tmp/bear-release-venv/bin/python dist/bear_agent-0.7.0-py3-none-any.whl
/tmp/bear-release-venv/bin/bear --version
/tmp/bear-release-venv/bin/bear --help
/tmp/bear-release-venv/bin/bear tui --help
```

Windows 对应 `Scripts\bear.exe`。不要用当前工作区的 `.venv` 作为安装验证。

## 运行时验证

```bash
bear doctor --json
bear config show
bear bench list
bear trace stats
bear metrics show --json
```

基础 Session/Run/Tool/Task/MCP/Trace/Metrics/Benchmark 验证见 `P7-COMPLETION-REPORT.md`。真实 Provider 验证需要有效 Key；真实 MCP 验证需要可信 Server；二者都不应写入示例配置或测试数据。

## 发布检查清单

- [ ] 版本号一致（pyproject / `__init__` / lock / build 产物）
- [ ] `uv sync --frozen` 通过
- [ ] Ruff / Format / Mypy / Pytest / Coverage 通过
- [ ] sdist 与 wheel 构建通过
- [ ] 隔离安装后的 `bear --version/--help/tui --help` 通过
- [ ] `bear doctor` 无意外 FAIL
- [ ] CI Ubuntu/Windows/macOS 通过
- [ ] README / CLI / TUI / Security / Troubleshooting 更新
- [ ] macOS Seatbelt、Windows 文件系统隔离、MCP 进程回收、Plugin Python 风险在发布说明中明确
- [ ] 未提交真实密钥

## CI

`.github/workflows/ci.yml` 在 Ubuntu、Windows、macOS 上运行 Ruff、格式、Mypy 与 pytest（含覆盖率输出）。不删除/跳过测试，不降低门禁。

## 升级与迁移

- Schema 由 `storage/migrations.py` 自动迁移；未知更高版本数据库会 fail-closed。
- 配置新增字段有默认值；`config_version` 不兼容时启动失败并给出明确错误。
- 发布后若需要数据迁移，必须提供备份/回滚说明，不得静默删除历史。

## 已知限制

macOS Seatbelt 由 CI 测试覆盖，未做人工实机验证；Windows Job Object 不提供文件系统隔离；Plugin Python/外部 Hook 不是沙箱；MCP 子进程不受 Bear Sandbox/Recovery 管理。详见 `SECURITY.md`。