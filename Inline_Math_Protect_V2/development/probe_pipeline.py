"""Stop the real local pipeline after formula extraction; never call an API."""
import json
import pickle
import sys
from pathlib import Path

from babeldoc.docvision.base_doclayout import DocLayoutModel
from babeldoc.format.pdf.high_level import translate
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly
from babeldoc.translator.translator import BaseTranslator


class StopProbe(Exception):
    pass


class NoAPI(BaseTranslator):
    def get_formular_placeholder(self, placeholder_id):
        return "{v" + str(placeholder_id) + "}", r"\{\s*v\s*" + str(placeholder_id) + r"\s*\}"

    def get_rich_text_left_placeholder(self, placeholder_id):
        return f"<style id='{placeholder_id}'>", f"<style id='{placeholder_id}'>"

    def get_rich_text_right_placeholder(self, placeholder_id):
        return "</style>", "</style>"

    def do_translate(self, text, rate_limit_params=None):
        raise AssertionError("API forbidden")

    def do_llm_translate(self, text, rate_limit_params=None):
        if text is None:
            return None
        raise AssertionError("API forbidden")


def main():
    source, dest = map(Path, sys.argv[1:])
    dest.mkdir(parents=True, exist_ok=True)

    def capture(self, docs):
        (dest / "document.pickle").write_bytes(pickle.dumps(docs))
        rows = []
        for page in docs.page:
            fonts = {f.font_id: f for f in page.pdf_font}
            xfonts = {x.xobj_id: {f.font_id: f for f in x.pdf_font} for x in page.pdf_xobject}
            for paragraph in page.pdf_paragraph:
                from babeldoc.format.pdf.document_il.midend.il_translator import ParagraphTranslateTracker
                text, ti = self.il_translator.pre_translate_paragraph(paragraph, ParagraphTranslateTracker(), fonts, xfonts)
                rows.append({"page": page.page_number, "id": paragraph.debug_id,
                             "label": paragraph.layout_label, "source": paragraph.unicode,
                             "input": text, "hints": ti.get_placeholders_hint() if ti else {}})
        (dest / "formula-inputs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print("PROBE", len(rows), "paragraphs; Chinese source chars", sum(sum("\u4e00" <= c <= "\u9fff" for c in (r['source'] or '')) for r in rows), flush=True)
        raise StopProbe()

    ILTranslatorLLMOnly.translate = capture
    config = TranslationConfig(NoAPI("en", "zh-CN", True), source, "en", "zh-CN",
                               DocLayoutModel.load_available(), working_dir=dest,
                               output_dir=dest, debug=True, auto_extract_glossary=False,
                               use_rich_pbar=False, pool_max_workers=2)
    try:
        translate(config)
    except StopProbe:
        print("STOPPED before translation; API calls = 0", flush=True)


if __name__ == "__main__":
    main()
