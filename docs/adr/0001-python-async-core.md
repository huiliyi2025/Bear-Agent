# ADR 0001：内核使用 Python 3.13 + asyncio

- 状态：已采纳
- 日期：2026-11-19

## 背景

仓库最初是 Python 骨架。Harness 属于 I/O 密集型（模型流、工具、SQLite），需要快速迭代、成熟生态和简单的安装体验。改用 TypeScript 会贴近参考项目，但需要重写现有代码与工具链。

## 决策

使用 Python 3.13、asyncio、Pydantic v2、httpx 与 uv。内核保持 Provider 中立、与 UI 无关。任何性能敏感的原生组件都推迟到 profiling 证明必要时再引入。

## 影响

- 开发快，并通过 mypy/Pydantic 获得较强类型约束。
- 通过 `uv tool install` / wheel 分发，不要求 Node 运行时。
- CPU 密集工作必须交给线程/进程；Runtime 面向 I/O 并发，不面向并行 CPU。
- 未来的单二进制分发可以使用 PyInstaller/Nuitka，而无需改变架构。

## 备选方案

- TypeScript（天枢技术栈）：因迁移成本被拒绝；只有项目需要浏览器原生扩展或 Node 专属依赖时再考虑。
- Rust：当前阶段因迭代速度被拒绝。