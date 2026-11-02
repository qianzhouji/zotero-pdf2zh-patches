# Reference Skip（含附录/补充材料恢复翻译）

本目录归档 Reference Skip 在自动术语抽取器中的源码改动。主翻译器中的对应逻辑位于 `../Inline_Math_Protect_V2/payload/il_translator_llm_only.py`；该文件同时含 Citation Protect 和 Inline Math Protect V2，因此作为完整当前文件保存。

## 文件说明

- `payload/automatic_term_extractor.py`：当前运行中的自动术语抽取器源码，其中含 Reference Skip。
- `baseline/automatic_term_extractor.py`：安装 Reference Skip 前的本机源码备份。
- `diffs/automatic_term_extractor.py.patch`：以上基线到当前运行文件的完整差异。

## 功能

1. 识别 References、Bibliography 等参考文献标题；标题本身保留翻译。
2. 参考文献条目跳过主翻译器与自动术语抽取器，减少文献题名被翻译和术语抽取 token 消耗。
3. 从参考文献模式恢复正常翻译：先使用附录、Supplementary/Supporting Material、Data Availability、Acknowledgments、Funding、Conflicts of Interest 等标题作为文本信号；也使用布局结构识别新章节。若识别到图注、表注等强结构标记，或新页首个有效内容块是章节标题，也会结束参考文献跳过。
4. 标题匹配结合页码比例和页眉、页脚、页码、残片过滤，降低目录或参考文献条目被误判为章节标题的概率。

## 运行位置

```text
<PDF2ZH_ROOT>/server/zotero-pdf2zh-next-venv/lib/python3.12/site-packages/babeldoc/format/pdf/document_il/midend/automatic_term_extractor.py
```

本目录是归档副本。插件仍从上述 BabelDOC 路径导入代码。`payload/` 文件未经专用安装器校验，不要手动覆盖运行文件。总补丁说明见 `../README.md`。
