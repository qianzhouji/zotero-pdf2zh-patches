# Citation Protect

本目录归档正式版 Citation Protect 的独立历史快照与差异。当前插件使用的主翻译器代码后来与 Reference Skip、Inline Math Protect 合并，实际运行文件仍是 `../Inline_Math_Protect_V2/payload/il_translator_llm_only.py` 对应的运行时版本。

## 文件

- `baseline/il_translator_llm_only.py`：加入 Citation Protect 前的本机源码。
- `payload/il_translator_llm_only.py`：正式 Citation Protect 已安装、Reference Skip 尚未加入时的源码快照。
- `diffs/il_translator_llm_only.py.patch`：该功能的独立差异。

## 功能

处理 BabelDOC 把上标数字引用误纳入公式占位符、或翻译模型移动/丢失引用标记的情况。正式逻辑会在模型输入中临时保护数字型引用占位符，返回后校验标记数量及顺序，再恢复原占位符。异常时进入安全回退。对话中曾加入的 CITE-HINTS/CITATION-DEBUG 大段诊断输出已从正式运行版本清理；不必要的尾随标点归一化建议没有安装。

当前集成运行位置：

```text
<PDF2ZH_ROOT>/server/zotero-pdf2zh-next-venv/lib/python3.12/site-packages/babeldoc/format/pdf/document_il/midend/il_translator_llm_only.py
```

此目录用于历史追溯，不是独立安装包，也不要把快照直接覆盖到运行目录。当前总功能和文件位置见 `../README.md` 与 `../Inline_Math_Protect_V2/运行时文件位置与功能.md`。
