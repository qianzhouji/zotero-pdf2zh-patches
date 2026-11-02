"""Fault injection through real paragraph restoration, with the real Step 2 IR."""
import copy
import json
import os
import re
import pickle
from pathlib import Path
from types import SimpleNamespace

from xsdata.formats.dataclass.parsers import JsonParser
from babeldoc.format.pdf.document_il import Document
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.document_il.midend.il_translator import PageTranslateTracker
from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import ILTranslatorLLMOnly, BatchParagraph
from babeldoc.format.pdf.document_il.utils.inline_math_protect import filter_offpage_text, parse_outputs
from probe_pipeline import NoAPI

os.environ['PDF2ZH_INLINE_MATH_LOG'] = str(Path('work/inline-math-review/offline-audit.jsonl').resolve())
root = Path('work/inline-math-review')
ir = pickle.loads((root / 'layout-probe/document.pickle').read_bytes())
page0 = ir.page[3]
step2 = next(p for p in page0.pdf_paragraph if 'Step 2:' in (p.unicode or ''))
formula_texts = [''.join(c.char_unicode or ' ' for c in x.pdf_formula.pdf_character)
                 for x in step2.pdf_paragraph_composition if x.pdf_formula]
assert any('ln' in s and s.count('q') == 2 for s in formula_texts), formula_texts
print('PASS entire source phi/log/fraction expression is one protected PDF object')
parameters = next(p for p in ir.page[2].pdf_paragraph if 'resting oxygen' in (p.unicode or ''))
param_formulas = [''.join(c.char_unicode or '' for c in x.pdf_formula.pdf_character)
                  for x in parameters.pdf_paragraph_composition if x.pdf_formula]
assert any('0.41' in x for x in param_formulas)
assert any('0.98' in x for x in param_formulas)
assert not any('0.41' in x and '0.98' in x for x in param_formulas)
print('PASS gamma and transit-time parameters remain separate across descriptive prose')
assert all((c.pdf_formula.box.x2 - c.pdf_formula.box.x) < 100
           for c in parameters.pdf_paragraph_composition if c.pdf_formula)
print('PASS wrapped gamma expression has compact physical-line objects')
texts = [p.unicode or '' for p in ir.page[3].pdf_paragraph]
assert any(s.startswith('In the MFM, the recurrent connection') for s in texts)
assert any(s.startswith('Cognitive components') and len(s) > 200 for s in texts)
assert any(s.startswith('The relative cortical myelin') and len(s) > 150 for s in texts)
assert not any((p.unicode or '').strip() in ['h', 'd'] and p.layout_label == 'title' for p in ir.page[3].pdf_paragraph)
print('PASS title boundaries no longer split the first body lines into fragments')


class Engine(NoAPI):
    def __init__(self, errors):
        super().__init__('en', 'zh-CN', True)
        self.errors = errors
        self.calls = 0

    def llm_translate(self, prompt, **kwargs):
        self.calls += 1
        items = json.loads(prompt.rsplit('## Here is the input:', 1)[1])
        result = [{'id': x['id'], 'output': '译文：' + x['input'].replace('To ensure', '为确保')} for x in items]
        mode = self.errors[min(self.calls - 1, len(self.errors) - 1)]
        if mode == 'json': return '{broken'
        if mode == 'id': result[0]['id'] = 999
        for item in result:
            tokens = re.findall(r'\[\[(?:MATH|CITE):[^\]]+\]\]', item['output'])
            if mode == 'drop': item['output'] = item['output'].replace(next(t for t in tokens if 'MATH' in t), '', 1)
            if mode == 'citation': item['output'] = item['output'].replace(next(t for t in tokens if 'CITE' in t), '', 1)
            if mode == 'reorder':
                a, b = [t for t in tokens if 'MATH' in t][:2]
                item['output'] = item['output'].replace(a, '__TEMP__').replace(b, a).replace('__TEMP__', b)
            if mode == 'duplicate': item['output'] += next(t for t in tokens if 'MATH' in t)
        return json.dumps(result, ensure_ascii=False)


def run_case(errors, preserved=False, post_error=False, skipped_first=False):
    page = copy.deepcopy(page0)
    paragraph = copy.deepcopy(step2)
    before = copy.deepcopy(paragraph)
    engine = Engine(errors)
    config = TranslationConfig(engine, 'offline-Step2.pdf', 'en', 'zh-CN', object(),
                               auto_extract_glossary=False, use_rich_pbar=False,
                               working_dir=root/'offline', output_dir=root/'offline')
    translator = ILTranslatorLLMOnly(engine, config)
    def no_legacy(*args, **kwargs): raise AssertionError('Unguarded fallback is forbidden')
    translator.il_translator.translate_paragraph = no_legacy
    if post_error:
        def fail(*args, **kwargs): raise ValueError('Injected formula restoration failure')
        translator.il_translator.parse_translate_output = fail
    paragraphs = [paragraph]
    pages = [page]
    if skipped_first:
        skipped = copy.deepcopy(paragraph); skipped.vertical = True
        paragraphs.insert(0, skipped)
        pages.insert(0, SimpleNamespace(page_number=88))
    batch = BatchParagraph(paragraphs, pages, PageTranslateTracker())
    progress = SimpleNamespace(count=0)
    def advance(n): progress.count += n
    progress.advance = advance
    fonts = {f.font_id: f for f in page.pdf_font}
    xfonts = {x.xobj_id: {f.font_id: f for f in x.pdf_font} for x in page.pdf_xobject}
    translator.translate_paragraph(batch, pbar=progress, page_font_map=fonts, xobj_font_map=xfonts)
    assert engine.calls == 1 if errors == ['valid'] else engine.calls >= 2, (errors, engine.calls)
    assert config._inline_math_protect_audit.counts['retry'] == (0 if errors == ['valid'] else 1)
    assert progress.count == len(paragraphs), (errors, progress.count)
    assert translator.total_count == 1, (errors, translator.total_count)
    if preserved:
        assert paragraph == before, errors
        assert translator.fallback_count == 1, errors
        assert config._inline_math_protect_audit.counts['source_preserved'] == 1
    else:
        assert paragraph.unicode != before.unicode, errors
        old = [c.pdf_formula for c in before.pdf_paragraph_composition if c.pdf_formula]
        new = [c.pdf_formula for c in paragraph.pdf_paragraph_composition if c.pdf_formula]
        assert old == new, (errors, 'PDF formula geometry or sequence changed')
        assert translator.ok_count == 1
    print('PASS', errors, 'source preserved' if preserved else 'formula objects identical')


for mode in ['drop', 'citation', 'reorder', 'duplicate', 'json', 'id']:
    run_case([mode, 'valid'])
run_case(['valid'])
run_case(['drop', 'drop'], preserved=True)
run_case(['valid', 'valid'], preserved=True, post_error=True)
run_case(['drop', 'valid'], skipped_first=True)

def objectify(value):
    if isinstance(value, dict): return SimpleNamespace(**{k: objectify(v) for k, v in value.items()})
    if isinstance(value, list): return [objectify(x) for x in value]
    return value
dirty = objectify(json.loads(next((root/'dirty-probe').rglob('create_il.debug.json')).read_text()))
config = SimpleNamespace(input_file='offline-clipped-pdf.pdf')
visible_before = [[c for c in p.pdf_character if c.box.x >= 0 and c.box.x2 <= p.cropbox.box.x2] for p in dirty.page]
filter_offpage_text(dirty, config)
assert not any('\u4e00' <= ch <= '\u9fff' for p in dirty.page for c in p.pdf_character for ch in c.char_unicode or '')
assert all(all(c in p.pdf_character for c in chars) for p,chars in zip(dirty.page,visible_before))
print('PASS hidden Chinese excluded, visible glyphs preserved')
for value in [[{'id': 0, 'output': 'a'}, {'id': 0, 'output': 'b'}], [{'id': True, 'output': 'a'}]]:
    try: parse_outputs(value, 1)
    except ValueError: pass
    else: raise AssertionError('Bad ids accepted')
print('PASS duplicate and boolean response ids rejected')
print('ALL OFFLINE CHECKS PASSED; API calls = 0')
