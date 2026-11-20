# ADR 0019：Plugin Framework 的定位与信任模型

- 状态：已采纳
- 日期：2026-11-20

## 背景

Plugin 容易退化成“万能接口”。本项目需要的是可版本化、可启停、可校验的扩展包，同时不能默认把不可信代码加载进主进程。

## 决策

Plugin 是**声明式扩展包**，以 `plugin.toml` 为 Manifest：

- ID、版本（PEP 440）、`requires_bear`、依赖（可带 specifier）、描述、权限声明。
- 可贡献：MCP Server 配置、外部 Hook 命令、可选的 Python 入口。
- Manifest-only Plugin（MCP/Hook）不需要 Python 权限，也不会在主进程执行代码。

可选 Python Plugin：

- 必须同时满足：`plugins.allow_python=true`、Manifest `permissions.python=true`、Plugin 显式 trusted。
- 入口为受信任 Python 文件，加载到主进程；可注册进程内 Hook 与 Tool。
- 没有沙箱：拥有与 Bear-Agent 相同的 OS 权限；风险在文档和 CLI 中明确。
- 默认关闭。Plugin 不能从模型输出或网络自动安装。

生命周期：

- 发现：配置目录 + 数据目录 `plugins/`，读取每个子目录的 Manifest。
- 校验：Manifest、版本、`requires_bear`、依赖、权限、贡献冲突。
- 启用状态：数据目录 `plugins/state.json`（原子写），覆盖 Manifest 默认值。
- 加载：按依赖拓扑顺序；先 staging 校验所有 Hook/Server/Tool 冲突，再一次性提交；失败 Plugin 不留下部分注册。
- 卸载：注销来源 Hook/Tool，关闭由 Plugin 贡献的 MCP 连接；Python 模块无法真正从解释器卸载，文档说明。
- CLI：list / info / validate / enable / disable。

关系：

- Plugin 与 MCP：Plugin 可以声明 MCP Server；Server 仍要求自身 trusted。
- Plugin 与 Hooks：Manifest Hook 走外部命令 Hook（需 hooks.allow_external）；Python Hook 仅在 HTTP/py 权限下注册。
- Plugin 与 ToolRegistry：Python Tool 仍通过 ToolExecutor 的权限/审批/Claim/Audit/Sandbox 链。
- Plugin 不是 Tool 的另一种命名；它只是打包扩展的边界。

## 影响

- 默认安全的路径是 Manifest + 外部 MCP/Hook；Python 扩展需要明确双重开关。
- 依赖冲突/版本不兼容在加载前失败。
- 插件状态是本地 JSON，不属于 Session 数据。
- 没有插件市场/远程安装/签名验证；这是后续工作。

## 备选方案

- 所有 Plugin 都加载 Python：拒绝；不可信代码进入主进程。
- Plugin 直接修改 Config/DB：拒绝；无法校验与回滚。
- 只支持 MCP 作为 Plugin：拒绝；Hooks/工具扩展被遗漏。