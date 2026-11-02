# Inline Math Protect V2

适用于这台 Mac 当前经过定制的 BabelDOC 0.6.2。此目录用于检查、安装、卸载和归档补丁。配置和密钥不在包内。

## V2 功能

- 在原生 PDF 解析后排除完全位于可见页面以外的文字，防止裁剪 PDF 中隐藏文字进入翻译。
- 保留小数点、指数、独立斜体变量及特殊符号字体；不猜测替换源 PDF 字形。
- 避免公式跨过说明文字合并；将跨物理行的公式按实际行拆分；按正确基线恢复公式与上下标。
- 修复标题框边缘抢走正文字符的问题。
- 联合校验公式与引用标记及段落 ID。失败段落仅进行一轮受控片段重试；仍失败时恢复原段落和原始组成。
- 记录重试、恢复和页外过滤等审计信息。

## 文件结构

- `manage.py`：版本锁定的安装、卸载、状态检查脚本。
- `manifest.json`：目标相对路径、基线/目标哈希和允许的已知版本。
- `payload/`：五个 V2 运行源码文件。
- `baseline/`：四个 V2 修改前的文件；新模块 `inline_math_protect.py` 没有旧文件。
- `diffs/`：相对本目录 baseline 的逐文件差异。
- `development/`：开发期探测、异常注入、真实翻译和历史应用脚本。日常安装不要使用 `apply_v2.py`。
- `offline-checks.txt`、`validation-summary.json`：离线检查和四页验证摘要。

## 保留的既有功能

本补丁是在 Reference Skip 与 Citation Protect 已安装的源码上开发的。`payload/il_translator_llm_only.py` 因此也包含这两项既有逻辑：保护正文上标数字引用；在主翻译器中识别参考文献区并于后续章节恢复翻译。卸载 Inline Math V2 时会恢复到本包的 baseline，保留这两项既有功能。

Reference Skip 在自动术语抽取器中的对应代码位于 `../Reference_Skip/payload/automatic_term_extractor.py`，其运行时文件不由本目录 `manage.py` 管理。完整功能说明见 `../README.md`。

## 管理命令

在本目录打开终端运行：

```bash
python3 manage.py status
python3 manage.py install
python3 manage.py remove
```

默认目标为：`~/zotero-pdf2zh/server/zotero-pdf2zh-next-venv/lib/python3.12/site-packages/babeldoc/format/pdf`。如需操作其他副本，可追加 `--root /绝对路径`。安装器先检查所有文件哈希，遇到未知更改会停止而不写入，并在修改前创建备份。

更新 BabelDOC 后需重新适配补丁。单纯整理本补丁目录或文件不要求重新翻译；新任务会从运行时导入代码。

## 检查记录

离线故障注入检查覆盖标记丢失、重排、重复、引用异常、非法 JSON、错误段落 ID、公式恢复异常及页面映射；四页真实翻译和视觉检查摘要见 `validation-summary.json`。
