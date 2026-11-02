# Zotero PDF2zh 本机补丁与辅助功能

**上游原始项目：** [guaguastandup/zotero-pdf2zh（GitHub）](https://github.com/guaguastandup/zotero-pdf2zh)  
**项目文档：** [Zotero PDF2zh 官方文档](https://zotero-pdf2zh.github.io/zh/)

本目录中的源码、脚本和差异文件，都是针对 Zotero PDF2zh 插件本机翻译后端（PDF2zh_next / BabelDOC）开发的补丁或维护工具；它们不是上游仓库自带功能。插件运行源码仍位于 Python 虚拟环境的 BabelDOC 固定导入路径中；本目录保存补丁安装包、源码快照、差异和说明。

补丁包含基于上游 AGPL-3.0 源码的修改；许可文本和来源说明见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。

## 目录结构

```text
patches/
├── README.md
├── Inline_Math_Protect_V2/
│   ├── manage.py、manifest.json
│   ├── payload/       五个 V2 源码文件；主翻译器还承载 Citation Protect 与主翻译 Reference Skip
│   ├── baseline/      安装 V2 前的四个基线文件
│   ├── diffs/         逐文件差异
│   ├── development/   开发期探测、离线检查、验证翻译和历史应用脚本
│   └── README.md、运行时文件位置与功能.md
├── Citation_Protect/
│   ├── payload/       独立历史快照：正式 Citation Protect 版本
│   ├── baseline/      安装前版本
│   ├── diffs/         Citation Protect 差异
│   └── README.md
├── Reference_Skip/
│   ├── payload/       自动术语抽取器的当前源码，含文献跳过
│   ├── baseline/      Reference Skip 前的本机源码
│   ├── diffs/         完整差异
│   └── README.md
└── Service_Helpers/
    ├── pdf2zh_helpers.zsh
    └── README.md
```

## 已开发的翻译功能

### Citation Protect：保护正文中的上标数字引用

针对 BabelDOC 将上标数字引用误识别为公式占位符，以及 LLM 翻译后引用语义位置漂移的问题。主翻译器在发送文本前，将识别到的引用占位符包成显式 CITE 标记；返回后核对数量和顺序，再恢复 BabelDOC 占位符。标记缺失、重复或重排时进入安全回退。正式逻辑保留了轻量记录，开发期的大段诊断输出已清理。

当前实现位于 `Inline_Math_Protect_V2/payload/il_translator_llm_only.py`。独立历史快照、补丁前基线和差异位于 `Citation_Protect/`。Citation Protect 在 Inline Math 开发前已存在，因此 Inline Math 安装器卸载 V2 时会保留它。

### Reference Skip：参考文献跳过，后续章节继续翻译

- 识别 References、Bibliography 等参考文献标题；标题本身仍可翻译。
- 参考文献条目不送往主翻译器，也不送往自动术语抽取器。
- 通过章节标题与 PDF 布局结构判断参考文献区结束，而不按“最后 N 页”推测。Appendix、Supplementary/Supporting Material、Data Availability、Acknowledgments、Funding、Conflicts of Interest 等后续标题会恢复翻译。
- 图注、表注等强布局信号以及新页首个有效内容块的标题，也可结束参考文献跳过状态；参考文献中的孤立碎片不会仅凭 `title` 标签就触发退出。
- 这适用于同一 PDF 中参考文献之后的附录或补充材料。单独下载的补充 PDF 不会由此规则自动合并处理。

主翻译器逻辑位于 `Inline_Math_Protect_V2/payload/il_translator_llm_only.py`；术语抽取器位于 `Reference_Skip/payload/automatic_term_extractor.py`。

### Inline Math Protect V2：行内数学与版面保护

保留源 PDF 公式对象与位置；识别数字小数点、变量和特殊字形；避免公式跨说明文字合并；拆分跨物理行公式并修正基线；排除页外隐藏文字、修正标题框抢正文字符；严格校验公式与引用标记，发生异常时受控重试，仍异常则恢复原段落。详见 `Inline_Math_Protect_V2/README.md`。

## 服务管理辅助命令

`Service_Helpers/pdf2zh_helpers.zsh` 归档了 `~/.zshrc` 中的 `pdf2zh-start`、`pdf2zh-stop`、`pdf2zh-status`、`pdf2zh-log`。启动命令最多等待 60 秒并轮询健康检查；其余分别管理服务停止、运行状态和日志查看。这些是本机 shell 工具，不参与 PDF 翻译；修改归档副本不会自动修改 `~/.zshrc`。

## 实际运行位置

PDF2zh 从此 BabelDOC 目录导入运行代码：

```text
<PDF2ZH_ROOT>/server/zotero-pdf2zh-next-venv/lib/python3.12/site-packages/babeldoc/format/pdf/
```

六个相关运行文件及职责见 `Inline_Math_Protect_V2/运行时文件位置与功能.md`。不要把运行时源码移出各自的 Python 子目录。

## Windows 迁移说明

核心 Python 翻译补丁没有发现 macOS 专用系统调用，原则上可以移植；但当前目录不是已经验证过的 Windows 一键安装包。需要处理以下环境差异：

1. **Windows 独立安装 PDF2zh/BabelDOC。** 不要复制这台 Mac 的 Python 虚拟环境。确认 Windows 上的 PDF2zh/BabelDOC 版本和模块路径，再定位它自己的 `babeldoc/format/pdf/` 目录。
2. **校准安装目标。** `Inline_Math_Protect_V2/manage.py` 的默认路径按本机目录布局设置，但支持 `--root` 指向 Windows 实际的 `format/pdf` 目录。安装器会逐文件核验 SHA-256；若 Windows 安装包源码版本不同或文件被改过，它会停止，需要先适配补丁，不能绕过哈希检查强行覆盖。传输时应保留源码字节和换行符。
3. **补齐 Reference Skip 安装步骤。** 目前正式 `manage.py` 只管理 Inline Math V2 的五个文件。Reference Skip 在自动术语抽取器中的源码已归档，但没有独立安装器；迁移时要把它纳入 Windows 安装流程。Citation Protect 和主翻译器 Reference Skip 已集成在主翻译器文件中。
4. **设置日志位置。** Inline Math 默认把审计日志写到用户目录下的 `zotero-pdf2zh/server/inline-math-protect.jsonl`；若 Windows 目录布局不同，可通过 `PDF2ZH_INLINE_MATH_LOG` 指向 Windows 日志路径。
5. **替换服务管理命令。** `Service_Helpers/pdf2zh_helpers.zsh` 使用 zsh、`launchctl` 和 `lsof`，仅适用于当前 macOS 服务配置。Windows 需要按实际部署方式另写 PowerShell 命令和服务启动方式。
6. **调整开发脚本路径并在 Windows 验证。** `development/apply_v2.py` 含本机绝对路径，不适合直接运行；其他开发脚本依赖原工作区路径和本地验证中间文件。安装后应在 Windows 上做一次状态核对、离线保护检查和小 PDF 翻译，再作为 Windows 版本归档。

因此，Python 补丁主体可复用，但迁移时要制作 Windows 专用安装配置并验证目标 BabelDOC 版本。仅复制归档目录不会修改 Windows 插件。

## 安装、卸载与重跑

- Inline Math V2 的安装、卸载和状态检查使用 `Inline_Math_Protect_V2/manage.py`。卸载 Inline Math 会恢复本包基线，其中已有的 Citation Protect 与主翻译 Reference Skip 会保留。
- `Citation_Protect/` 是独立历史快照和差异，用于追溯最初实现；当前功能已并入主翻译器。
- `Reference_Skip/` 是当前自动术语抽取器的源码、补丁前备份和差异归档；它不带独立安装器，当前功能已在 BabelDOC 运行目录中。
- 移动或整理本补丁目录里的说明、源码副本和差异文件，不影响插件，也不需要重跑翻译。不要移动运行时文件。
- 运行 `development/run_translation.py` 会调用现有翻译服务；普通归档和文档更新无需运行它。
- 模型/API 配置和密钥不纳入本目录。
