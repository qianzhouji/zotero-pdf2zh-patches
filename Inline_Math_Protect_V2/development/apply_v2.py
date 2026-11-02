from pathlib import Path
from datetime import datetime
import os
import shutil

ROOT = Path(os.environ.get(
    'PDF2ZH_BABELDOC_PDF_ROOT',
    str(Path.home() / 'zotero-pdf2zh/server/zotero-pdf2zh-next-venv/lib/python3.12/site-packages/babeldoc/format/pdf'),
)).expanduser()
BACKUP = Path('work/inline-math-review/before-v2')
BACKUP.mkdir(exist_ok=True)


def replace_once(s, old, new):
    assert s.count(old) == 1, (old[:100], s.count(old))
    return s.replace(old, new, 1)


target = ROOT / 'document_il/midend/il_translator_llm_only.py'
s = target.read_text()
original = s
s = replace_once(s, 'logger = logging.getLogger(__name__)\n# INLINE MATH PROTECT V1', '''from babeldoc.format.pdf.document_il.utils.inline_math_protect import (
    get_audit, parse_outputs, restore_markers,
)

logger = logging.getLogger(__name__)
# INLINE MATH PROTECT V2''')
s = replace_once(s, '\n\nclass ILTranslatorLLMOnly:', '''

    @classmethod
    def for_guarded_retry(cls, paragraph, page, tracker):
        batch = cls.__new__(cls)
        batch.paragraphs = [paragraph]
        batch.pages = [page]
        batch.trackers = [tracker]
        return batch


class ILTranslatorLLMOnly:''')
s = replace_once(s, '        self.total_count = 0\n', '        self.total_count = 0\n        self._math_audit = get_audit(translation_config)\n')
s = replace_once(s, '        logger.info(\n            f"Translation completed. Total:', '        self._math_audit.summary()\n        logger.info(\n            f"Translation completed. Total:')
anchor = '    def translate_paragraph(\n        self,\n        batch_paragraph: BatchParagraph,'
helper = '''    def _guarded_fallback(
        self, paragraph, page, tracker, pbar, page_font_map, xobj_font_map,
        title_paragraph, local_title_paragraph, executor, mp_id, already_retried, reason,
    ):
        """All formula/citation failures share one guarded retry; then keep source."""
        last_tracker = tracker.last_llm_translate_tracker()
        if last_tracker:
            last_tracker.set_fallback_to_translate()
        if already_retried:
            self.total_count += 1
            self.fallback_count += 1
            self._math_audit.record(
                "source_preserved", paragraph_id=paragraph.debug_id,
                page=page.page_number + 1, reason=reason,
                source=(paragraph.unicode or "")[:4000],
            )
            if pbar:
                pbar.advance(1)
            return
        self._math_audit.record(
            "retry", paragraph_id=paragraph.debug_id, page=page.page_number + 1,
            reason=reason, source=(paragraph.unicode or "")[:4000],
        )
        batch = BatchParagraph.for_guarded_retry(paragraph, page, tracker)
        kwargs = dict(
            pbar=pbar, page_font_map=page_font_map, xobj_font_map=xobj_font_map,
            title_paragraph=title_paragraph, local_title_paragraph=local_title_paragraph,
            executor=executor, paragraph_token_count=self.calc_token_count(paragraph.unicode),
            mp_id=mp_id, _guard_retry=True, _guard_reason=reason[:1000],
        )
        if executor is None:
            self.translate_paragraph(batch, **kwargs)
        else:
            executor.submit(self.translate_paragraph, batch, **kwargs)

'''
s = replace_once(s, anchor, helper + anchor)
s = replace_once(s, '        mp_id: int = 0,\n    ):', '        mp_id: int = 0,\n        _guard_retry: bool = False,\n        _guard_reason: str = "",\n    ):')
s = replace_once(s, '            paragraph_unicodes = []\n', '            paragraph_unicodes = []\n            paragraph_compositions = []\n')
s = replace_once(s, '                paragraph_unicodes.append(paragraph.unicode)\n', '                paragraph_unicodes.append(paragraph.unicode)\n                paragraph_compositions.append(paragraph.pdf_paragraph_composition)\n')
s = replace_once(s, '            math_guard_maps = []\n', '            math_guard_maps = []\n            protected_marker_maps = []\n')
old = '''                    _marker_id = str(len(_math_map) + 1)
                    _marker = f"[[MATH:{_marker_id}]]"
                    protected_input = protected_input.replace(_raw_placeholder, _marker, 1)
                    _math_map[_marker_id] = _raw_placeholder
                    if _raw_placeholder in _protected_hints:
                        _protected_hints[_marker] = _protected_hints.pop(_raw_placeholder)
                math_guard_maps.append(_math_map)'''
new = '''                    while _raw_placeholder in protected_input:
                        _marker_id = str(len(_math_map) + 1)
                        _marker = f"[[MATH:{_marker_id}]]"
                        protected_input = protected_input.replace(_raw_placeholder, _marker, 1)
                        _math_map[_marker_id] = _raw_placeholder
                        if _raw_placeholder in _protected_hints:
                            _protected_hints[_marker] = _protected_hints[_raw_placeholder]
                    _protected_hints.pop(_raw_placeholder, None)
                math_guard_maps.append(_math_map)
                _marker_map = {f"[[CITE:{ph.strip('{}')}:{value}]]": ph
                               for ph, value in protected_refs}
                _marker_map.update({f"[[MATH:{key}]]": value for key, value in _math_map.items()})
                protected_marker_maps.append(_marker_map)
                if _marker_map and not _guard_retry:
                    self._math_audit.record("guarded", paragraph_id=input_text[2].debug_id)'''
s = replace_once(s, old, new)
s = replace_once(s, '            for llm_translate_tracker in llm_translate_trackers:\n                llm_translate_tracker.set_input(final_input)', '''            if _guard_retry:
                expected = list(protected_marker_maps[0])
                expected.sort(key=lambda token: json_format_input[0]["input"].find(token))
                correction = (
                    "## Required correction for this single-paragraph retry\\n"
                    "The previous attempt failed validation. Translate the prose clause by clause "
                    "in source order. Keep every immutable marker with its original clause, "
                    "even when another Chinese word order would sound more natural. "
                    "Never leave the entire English paragraph untranslated.\\n"
                    f"Required marker sequence: {json.dumps(expected, ensure_ascii=False)}\\n"
                    f"Validation failure: {_guard_reason}\\n\\n"
                )
                final_input = final_input.replace("## Here is the input:", correction + "## Here is the input:", 1)
            for llm_translate_tracker in llm_translate_trackers:
                llm_translate_tracker.set_input(final_input)''')
start = s.index('            if isinstance(parsed_output, dict) and parsed_output.get(')
end = s.index('            for id_, output in translation_results.items():', start)
s = s[:start] + '            translation_results = parse_outputs(parsed_output, len(inputs))\n\n' + s[end:]
s = s.replace('                preserve_source_for_math = False\n', '')
start = s.index('                    # CITATION PROTECT:\n                    # Verify numeric citation markers')
end = s.index('                    input_unicode = inputs[id_][0]', start)
s = s[:start] + '''                    # Validate CITE and MATH together before restoring any PDF object.
                    translated_text = restore_markers(
                        inputs[id_][0], json_format_input[id_]["input"], translated_text,
                        protected_marker_maps[id_],
                    )

''' + s[end:]
start = s.index('                except Exception as e:\n                    error_message = f"Error translating paragraph.')
end = s.index('    def _build_llm_prompt(', start)
s = s[:start] + '''                except Exception as e:
                    error_message = f"Error translating paragraph. Error: {e}."
                    logger.warning(error_message)
                    inputs[id_][4].set_error_message(error_message)
                    if protected_marker_maps[id_]:
                        self._math_audit.record(
                            "rejected", paragraph_id=inputs[id_][2].debug_id,
                            retry=_guard_retry, reason=error_message,
                            input=json_format_input[id_]["input"], output=str(output)[:8000],
                        )
                finally:
                    if should_fallback:
                        # post_translate can set unicode before parsing raises; restore both.
                        inputs[id_][2].unicode = paragraph_unicodes[id_]
                        inputs[id_][2].pdf_paragraph_composition = paragraph_compositions[id_]
                        inputs[id_][4].set_fallback_to_translate()
                        page = batch_paragraph.pages[should_translate_paragraph[id_]]
                        guarded = any(isinstance(p, il_translator.FormulaPlaceholder)
                                      for p in inputs[id_][1].placeholders)
                        if guarded:
                            self._guarded_fallback(
                                inputs[id_][2], page, inputs[id_][3], pbar,
                                page_font_map, xobj_font_map, title_paragraph,
                                local_title_paragraph, executor, mp_id, _guard_retry,
                                inputs[id_][4].error_message or "Translation validation failed",
                            )
                        else:
                            self.total_count += 1
                            self.fallback_count += 1
                            paragraph_token_count = self.calc_token_count(inputs[id_][2].unicode)
                            executor.submit(
                                self.il_translator.translate_paragraph,
                                inputs[id_][2], page, pbar, inputs[id_][3],
                                page_font_map, xobj_font_map,
                                priority=1048576 - paragraph_token_count,
                                paragraph_token_count=paragraph_token_count,
                                title_paragraph=title_paragraph,
                                local_title_paragraph=local_title_paragraph,
                            )
                    else:
                        self.total_count += 1
                        self.ok_count += 1
                        if protected_marker_maps[id_]:
                            self._math_audit.record(
                                "restored", paragraph_id=inputs[id_][2].debug_id,
                                retry=_guard_retry, marker_count=len(protected_marker_maps[id_]),
                            )

        except Exception as e:
            error_message = f"Error {e} during batch translation"
            logger.warning(error_message)
            for index, input_ in enumerate(inputs):
                input_[2].unicode = paragraph_unicodes[index]
                input_[2].pdf_paragraph_composition = paragraph_compositions[index]
                input_[4].set_error_message(error_message)
                input_[4].set_fallback_to_translate()
                page = batch_paragraph.pages[should_translate_paragraph[index]]
                guarded = any(isinstance(p, il_translator.FormulaPlaceholder)
                              for p in input_[1].placeholders)
                if guarded:
                    self._guarded_fallback(
                        input_[2], page, input_[3], pbar, page_font_map, xobj_font_map,
                        title_paragraph, local_title_paragraph, executor, mp_id,
                        _guard_retry, error_message,
                    )
                else:
                    self.total_count += 1
                    self.fallback_count += 1
                    paragraph_token_count = self.calc_token_count(input_[2].unicode)
                    executor.submit(
                        self.il_translator.translate_paragraph,
                        input_[2], page, pbar, input_[3], page_font_map, xobj_font_map,
                        priority=1048576 - paragraph_token_count,
                        paragraph_token_count=paragraph_token_count,
                        title_paragraph=title_paragraph,
                        local_title_paragraph=local_title_paragraph,
                    )

''' + s[end:]
compile(s, str(target), 'exec')
assert 'REFERENCE-SKIP' in s and 'CITATION-PROTECT' in s
assert 'preserve_source_for_math' not in s
(BACKUP / target.name).write_text(original)
target.with_name(target.name + '.bak-before-inline-math-v2-' + datetime.now().strftime('%Y%m%d-%H%M%S')).write_text(original)
target.write_text(s)

target = ROOT / 'new_parser/native_parse.py'
s = target.read_text(); original = s
old = '''        return run_prepared_pages(
            sink,
            config.should_translate_page,
            prepared_pages,
            page_interpreter,
        )'''
new = '''        docs = run_prepared_pages(
            sink,
            config.should_translate_page,
            prepared_pages,
            page_interpreter,
        )
        # INLINE MATH PROTECT V2: clipped dual-page content is not translatable.
        from babeldoc.format.pdf.document_il.utils.inline_math_protect import filter_offpage_text
        return filter_offpage_text(docs, config)'''
s = replace_once(s, old, new)
compile(s, str(target), 'exec')
(BACKUP / target.name).write_text(original)
target.with_name(target.name + '.bak-before-inline-math-v2-' + datetime.now().strftime('%Y%m%d-%H%M%S')).write_text(original)
target.write_text(s)
print('V2 applied. Existing Reference Skip and Citation Protect preserved.')
