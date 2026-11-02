# Source and modification notice

This repository contains modified source files from the Zotero PDF2zh / bundled BabelDOC project. The upstream Zotero PDF2zh repository is distributed under the GNU Affero General Public License version 3.0 (AGPL-3.0); the locally installed BabelDOC 0.6.2 package also declares `AGPL-3.0`. The full license text is in `LICENSE`.

These files contain local modifications made during October–November 2026 and collected for publication on 2026-11-03:

- `Citation_Protect/payload/il_translator_llm_only.py`: historical Citation Protect version.
- `Inline_Math_Protect_V2/payload/il_translator_llm_only.py`: integrated Citation Protect, Reference Skip, and Inline Math Protect V2 changes.
- `Inline_Math_Protect_V2/payload/styles_and_formulas.py`: Inline Math formula detection and layout changes.
- `Inline_Math_Protect_V2/payload/layout_helper.py`: text-box/character assignment fix.
- `Inline_Math_Protect_V2/payload/native_parse.py`: off-page text filtering.
- `Inline_Math_Protect_V2/payload/inline_math_protect.py`: new Inline Math Protect helper module.
- `Reference_Skip/payload/automatic_term_extractor.py`: Reference Skip in automatic terminology extraction.

The `baseline/` files are patch-specific comparison/rollback snapshots. In particular, `Inline_Math_Protect_V2/baseline/il_translator_llm_only.py` already contains the earlier Citation Protect and main-translator Reference Skip changes; it is a pre-Inline-Math baseline, not an unmodified upstream file.

This repository is an unofficial collection of personal modifications and is not endorsed by the upstream project. The modified source is provided under AGPL-3.0. Upstream: https://github.com/guaguastandup/zotero-pdf2zh
