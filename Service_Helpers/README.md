# PDF2zh 服务管理辅助命令

`pdf2zh_helpers.zsh` 是当前 `~/.zshrc` 中 PDF2zh HELPERS V2 区块的归档副本。它不是 PDF2zh 插件代码，不参与 PDF 翻译。当前生效版本仍在 `~/.zshrc`。

包含四个 shell 函数：

- `pdf2zh-start`：启动 LaunchAgent，最多每秒检查一次服务健康状态，等待上限 60 秒；超时显示服务状态和最近错误日志。
- `pdf2zh-stop`：停止 LaunchAgent 并等待端口释放。
- `pdf2zh-status`：查询 8890 端口进程及 `/health` 状态。
- `pdf2zh-log`：持续跟踪 PDF2zh 服务日志，按 Ctrl+C 只退出日志查看。

这份副本用于汇总和查阅；修改它不会改变 `~/.zshrc` 中的当前函数定义。

这组命令依赖 macOS 的 LaunchAgent 和 zsh；Windows 上需使用 PowerShell 和该机器自己的服务/任务计划配置，不能直接运行此 `.zsh` 文件。
