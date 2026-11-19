# ADR 0004：端口与适配器；单一组合根

- 状态：已采纳
- 日期：2026-11-19

## 背景

Agent Runtime 很容易坍缩成一个同时了解 Provider、数据库、UI 和工具的巨型 Loop，导致测试与替换成本很高。阶段 0 设计明确要求 Runtime 与 Provider、工具、UI 解耦。

## 决策

在内核定义端口，并以外围适配器实现：

- `ModelProvider`（providers/base.py）
- `Tool` + `ToolRegistry`（tools/base.py）
- `SessionService` + Repository（sessions、storage）
- `Approver`（policy/approver.py）
- `EventBus`（observability/bus.py）
- `PathGuard` 与 `PermissionEngine`（policy）

`bootstrap.py` 是唯一组合根。`ui/` 是外层。内核不 import 具体适配器或 Rich/Typer。

## 影响

- 测试使用脚本化 Provider 与临时 SQLite，不需要网络或 UI。
- 新增 Provider 或存储后端时改动局部化。
- `bootstrap.py` 的显式装配更多；这是清晰边界的成本。
- 用结构化协议（如 `policy.engine.ToolLike`）防止依赖环，而不是反向 import。

## 备选方案

- 框架管理的 DI 容器：当前规模没有必要，且隐藏对象图。
- Runtime 内直接 import：写起来快，但无法干净测试或替换。