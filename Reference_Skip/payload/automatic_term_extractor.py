from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import tiktoken
from tqdm import tqdm

from babeldoc.format.pdf.document_il import (
    Document as ILDocument,  # Renamed to avoid conflict
)
from babeldoc.format.pdf.document_il import PdfParagraph  # Renamed to avoid conflict
from babeldoc.format.pdf.document_il.midend.il_translator import Page
from babeldoc.format.pdf.document_il.utils.paragraph_helper import is_cid_paragraph
from babeldoc.format.pdf.document_il.utils.paragraph_helper import (
    is_placeholder_only_paragraph,
)
from babeldoc.format.pdf.document_il.utils.paragraph_helper import (
    is_pure_numeric_paragraph,
)
from babeldoc.utils.priority_thread_pool_executor import PriorityThreadPoolExecutor

if TYPE_CHECKING:
    from babeldoc.format.pdf.translation_config import TranslationConfig
    from babeldoc.translator.translator import BaseTranslator

logger = logging.getLogger(__name__)

LLM_PROMPT_TEMPLATE: str = """
You are an expert multilingual terminologist. Extract key terms from the text and translate them into {target_language}.

### Extraction Rules
1. Include only: named entities (people, orgs, locations, theorem/algorithm names, dates) and domain-specific nouns/noun phrases essential to meaning.
2. No full sentences. Ignore function words.
3. Use minimal noun phrases (≤5 words unless a named entity). No generic academic nouns (e.g., model, case, property) unless part of a standard term.
4. No mathematical items: variables (X1, a, ε), symbols (=, +, →, ⊥⊥, ∈), subscripts/superscripts, formula fragments, mappings (T: H1→H2), etc. Keep only natural-language concepts.
5. Extract each term once. Keep order of first appearance.

### Translation Rules
1. Translate each term into {target_language}.
2. If in the reference glossary, use its translation exactly.
3. Keep proper names in original language unless a well-known translation exists.
4. Ensure consistent translations.

{reference_glossary_section}

### Output Format
- Return ONLY a valid JSON array.
- Each element: {{"src": "...", "tgt": "..."}}.
- No comments, no backticks, no extra text.
- If no terms: [].

### Example
For terms “LLM”, “GPT”:
{example_output}

Input Text:
```
{text_to_process}
```

Return JSON ONLY. NO OTHER TEXT.
Result:
"""


class BatchParagraph:
    def __init__(
        self,
        paragraphs: list[PdfParagraph],
        page_tracker: PageTermExtractTracker,
    ):
        self.paragraphs = paragraphs
        self.tracker = page_tracker.new_paragraph()


class DocumentTermExtractTracker:
    def __init__(self):
        self.page = []

    def new_page(self):
        page = PageTermExtractTracker()
        self.page.append(page)
        return page

    def to_json(self):
        pages = []
        for page in self.page:
            paragraphs = []
            for para in page.paragraph:
                o_str = getattr(para, "output", None)
                i_str = getattr(para, "input", None)
                pdf_unicodes = getattr(para, "pdf_unicodes", None)
                if not pdf_unicodes:
                    continue
                paragraphs.append(
                    {
                        "pdf_unicodes": pdf_unicodes,
                        "output": o_str,
                        "input": i_str,
                    },
                )
            pages.append({"paragraph": paragraphs})
        return json.dumps({"page": pages}, ensure_ascii=False, indent=2)


class PageTermExtractTracker:
    def __init__(self):
        self.paragraph = []

    def new_paragraph(self):
        paragraph = ParagraphTermExtractTracker()
        self.paragraph.append(paragraph)
        return paragraph


class ParagraphTermExtractTracker:
    def __init__(self):
        self.pdf_unicodes = []

    def append_paragraph_unicode(self, unicode: str):
        self.pdf_unicodes.append(unicode)

    def set_output(self, output: str):
        self.output = output

    def set_input(self, _input: str):
        self.input = _input


class AutomaticTermExtractor:
    stage_name = "Automatic Term Extraction"

    def __init__(
        self,
        translate_engine: BaseTranslator,
        translation_config: TranslationConfig,
    ):
        self.translate_engine = translate_engine
        self.translation_config = translation_config
        self.shared_context = translation_config.shared_context_cross_split_part
        self.tokenizer = tiktoken.encoding_for_model("gpt-4o")
        self._reference_skip_ids: set[int] = set()

        # Check if the translate_engine has llm_translate capability
        if not hasattr(self.translate_engine, "llm_translate") or not callable(
            self.translate_engine.llm_translate
        ):
            raise ValueError(
                "The provided translate_engine does not support LLM-based translation, which is required for AutomaticTermExtractor."
            )



    # >>> REFERENCE SKIP HELPERS >>>

    _REFERENCE_HEADINGS = {
        "references",
        "reference",
        "bibliography",
        "reference list",
        "references cited",
        "literature cited",
        "cited literature",
        "works cited",
        "references and notes",
        "references & notes",
        "notes and references",
        "references and further reading",
        "further reading",
        "supplementary references",
        "supplemental references",
        "appendix references",
    }

    # Text rules are fallback only.
    # Structural detection is preferred.
    _REFERENCE_END_HEADINGS = {
        "appendix",
        "appendices",
        "online appendix",
        "web appendix",
        "eappendix",
        "supplement",
        "supplements",
        "supplementary material",
        "supplementary materials",
        "supplementary information",
        "supplementary appendix",
        "supplemental material",
        "supplemental materials",
        "supplemental information",
        "supplemental appendix",
        "supporting information",
        "electronic supplementary material",
        "online supplementary material",
        "online supplement",
        "additional file",
        "additional files",
        "author contributions",
        "author contribution",
        "funding",
        "funding information",
        "institutional review board statement",
        "informed consent statement",
        "data availability statement",
        "data availability",
        "availability of data and materials",
        "acknowledgments",
        "acknowledgements",
        "acknowledge",
        "acknowledgment",
        "acknowledgement",
        "conflicts of interest",
        "conflict of interest",
        "competing interests",
        "declaration of competing interest",
        "ethics statement",
        "ethical approval",

        "figure titles and legends",
        "figure titles & legends",
        "figure legends",
        "figure legend",
        "figure captions",
        "figure captions and legends",
        "figure legends and captions",
        "table titles and legends",
        "table titles & legends",
        "table legends",
        "table legend",
        "table captions",
        "table captions and legends",
        "table legends and captions",
        "figures",
        "tables",
    }

    @staticmethod
    def _normalize_reference_heading(text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip().lower()

        # "8. References" / "12 References" -> "references"
        text = re.sub(
            r"^\s*(?:section\s+)?\d+(?:\.\d+)*[.)]?\s+",
            "",
            text,
            flags=re.IGNORECASE,
        )

        return text.strip(" .:：;；-_—–")

    @staticmethod
    def _normalize_layout_label(paragraph) -> str:
        label = getattr(paragraph, "layout_label", None) or ""
        return re.sub(
            r"[\s_-]+",
            " ",
            str(label).strip().lower(),
        )

    def _is_reference_heading(self, text: str) -> bool:
        norm = self._normalize_reference_heading(text)

        if norm in self._REFERENCE_HEADINGS:
            return True

        # Also tolerate: R E F E R E N C E S
        if len(norm) <= 60:
            compact = re.sub(r"[^a-z]", "", norm)

            if compact in {
                "references",
                "reference",
                "bibliography",
                "referencelist",
                "referencescited",
                "literaturecited",
                "citedliterature",
                "workscited",
                "referencesandnotes",
                "notesandreferences",
                "referencesandfurtherreading",
                "furtherreading",
                "supplementaryreferences",
                "supplementalreferences",
                "appendixreferences",
            }:
                return True

        return False

    def _is_reference_end_heading(self, text: str) -> bool:
        """
        Text fallback.
        Structural detection is the primary mechanism.
        """
        norm = self._normalize_reference_heading(text)

        if norm in self._REFERENCE_END_HEADINGS:
            return True


        # Common post-reference figure/table sections.
        # Use controlled prefix matching rather than arbitrary substring
        # matching, so normal bibliography text is not accidentally treated
        # as a new section.
        _legend_section_prefixes = (
            "figure titles and legends",
            "figure titles & legends",
            "figure legends",
            "figure legend",
            "figure captions",
            "figure captions and legends",
            "figure legends and captions",
            "table titles and legends",
            "table titles & legends",
            "table legends",
            "table legend",
            "table captions",
            "table captions and legends",
            "table legends and captions",
        )

        if any(
            norm == heading or norm.startswith(heading + " ")
            for heading in _legend_section_prefixes
        ):
            return True

        if norm.startswith((
            "appendix ",
            "online appendix ",
            "web appendix ",
            "eappendix ",
            "supplement ",
            "supplements ",
            "supplementary ",
            "supplemental ",
            "supporting information ",
            "electronic supplementary ",
            "online supplementary ",
            "online supplement ",
            "additional file ",
        )):
            return True

        return False

    def _is_reference_noise(self, paragraph, text: str) -> bool:
        """
        Ignore page numbers / headers / abandoned fragments when deciding
        whether a block is the first meaningful object of a new page.
        """
        label = self._normalize_layout_label(paragraph)
        stripped = re.sub(r"\s+", " ", text or "").strip()

        if label in {
            "abandon",
            "header",
            "footer",
            "page number",
            "page",
        }:
            return True

        if re.fullmatch(r"\d{1,4}", stripped):
            return True

        if re.fullmatch(
            r"(?i)[ivxlcdm]{1,8}",
            stripped,
        ):
            return True

        return False

    def _is_strong_post_reference_structure(self, paragraph) -> bool:
        """
        Strong evidence that References has ended.

        Confirmed example from the McKinnon test:
            Table 1 -> layout_label='table_caption'
        """
        label = self._normalize_layout_label(paragraph)

        if label in {
            "table caption",
            "figure caption",
            "image caption",
        }:
            return True

        if "caption" in label:
            return True

        return False

    def _is_heading_like_layout(self, paragraph) -> bool:
        """
        Heading/title is a weaker signal because BabelDOC may occasionally
        label fragments inside a reference entry as 'title'.

        Therefore heading labels only terminate References when they are
        the first meaningful block of a new page.
        """
        label = self._normalize_layout_label(paragraph)

        if label in {
            "title",
            "heading",
            "section",
            "section title",
            "section heading",
            "section header",
            "chapter title",
        }:
            return True

        if "heading" in label:
            return True

        if label.endswith(" title"):
            return True

        return False

    def _prepare_reference_skip_ids(self, docs) -> None:
        """
        Structure-first bibliography detection.

        1. References heading itself remains translatable.
        2. Bibliography entries bypass the LLM.
        3. Captions strongly terminate Reference Mode.
        4. A heading terminates Reference Mode only when it is the first
           meaningful block on a new page.
        5. Explicit heading text remains a fallback.
        """
        self._reference_skip_ids = set()

        pages = list(docs.page)

        if not pages:
            return

        min_page_index = int(len(pages) * 0.35)

        in_references = False
        detected_reference = False
        skipped = 0

        for page_index, page in enumerate(pages):
            meaningful_count = 0

            for paragraph in page.pdf_paragraph:
                text = getattr(
                    paragraph,
                    "unicode",
                    None,
                )

                if not text:
                    continue

                is_noise = self._is_reference_noise(
                    paragraph,
                    text,
                )

                is_first_meaningful = (
                    not is_noise
                    and meaningful_count == 0
                )

                if not is_noise:
                    meaningful_count += 1

                # ENTER REFERENCES
                if (
                    not in_references
                    and page_index >= min_page_index
                    and self._is_reference_heading(text)
                ):
                    in_references = True
                    detected_reference = True

                    logger.info(
                        "[REFERENCE-SKIP] bibliography detected "
                        f"on page {page_index + 1}: {text!r}"
                    )

                    # Keep References heading translatable.
                    continue

                if not in_references:
                    continue

                # EXIT 1: explicit text fallback
                if self._is_reference_end_heading(text):
                    logger.info(
                        "[REFERENCE-SKIP] bibliography ended "
                        f"before page {page_index + 1} "
                        f"by heading text: {text!r}"
                    )

                    in_references = False
                    continue

                # EXIT 2: strong structural signal
                if self._is_strong_post_reference_structure(
                    paragraph
                ):
                    logger.info(
                        "[REFERENCE-SKIP] bibliography ended "
                        f"before page {page_index + 1} "
                        "by structural label "
                        f"{getattr(paragraph, 'layout_label', None)!r}: "
                        f"{text[:160]!r}"
                    )

                    in_references = False
                    continue

                # EXIT 3: new-page heading
                if (
                    is_first_meaningful
                    and self._is_heading_like_layout(
                        paragraph
                    )
                ):
                    logger.info(
                        "[REFERENCE-SKIP] bibliography ended "
                        f"before page {page_index + 1} "
                        "by first-page heading label "
                        f"{getattr(paragraph, 'layout_label', None)!r}: "
                        f"{text[:160]!r}"
                    )

                    in_references = False
                    continue

                # Still inside bibliography
                self._reference_skip_ids.add(
                    id(paragraph)
                )
                skipped += 1

        if not detected_reference:
            logger.info(
                "[REFERENCE-SKIP] no bibliography heading detected; "
                "normal translation will be used."
            )
        else:
            logger.info(
                "[REFERENCE-SKIP] "
                f"{skipped} bibliography paragraph(s) "
                "will bypass the LLM."
            )

    # <<< REFERENCE SKIP HELPERS <<<


    def calc_token_count(self, text: str) -> int:
        try:
            return len(self.tokenizer.encode(text, disallowed_special=()))
        except Exception:
            return 0

    def _snapshot_token_usage(self) -> tuple[int, int, int, int]:
        if not self.translate_engine:
            return 0, 0, 0, 0
        token_counter = getattr(self.translate_engine, "token_count", None)
        prompt_counter = getattr(self.translate_engine, "prompt_token_count", None)
        completion_counter = getattr(
            self.translate_engine, "completion_token_count", None
        )
        cache_hit_prompt_counter = getattr(
            self.translate_engine, "cache_hit_prompt_token_count", None
        )
        total_tokens = token_counter.value if token_counter else 0
        prompt_tokens = prompt_counter.value if prompt_counter else 0
        completion_tokens = completion_counter.value if completion_counter else 0
        cache_hit_prompt_tokens = (
            cache_hit_prompt_counter.value if cache_hit_prompt_counter else 0
        )
        return total_tokens, prompt_tokens, completion_tokens, cache_hit_prompt_tokens

    def _clean_json_output(self, llm_output: str) -> str:
        llm_output = llm_output.strip()
        if llm_output.startswith("<json>"):
            llm_output = llm_output[6:]
        if llm_output.endswith("</json>"):
            llm_output = llm_output[:-7]
        if llm_output.startswith("```json"):
            llm_output = llm_output[7:]
        if llm_output.startswith("```"):
            llm_output = llm_output[3:]
        if llm_output.endswith("```"):
            llm_output = llm_output[:-3]
        return llm_output.strip()

    def _process_llm_response(self, llm_response_text: str, request_id: str):
        try:
            cleaned_response_text = self._clean_json_output(llm_response_text)
            extracted_data = json.loads(cleaned_response_text)

            if not isinstance(extracted_data, list):
                logger.warning(
                    f"Request ID {request_id}: LLM response was not a JSON list, but type: {type(extracted_data)}. Content: {cleaned_response_text[:200]}"
                )
                return

            for item in extracted_data:
                if isinstance(item, dict) and "src" in item and "tgt" in item:
                    src_term = str(item["src"]).strip()
                    tgt_term = str(item["tgt"]).strip()
                    if (
                        src_term and tgt_term and len(src_term) < 100
                    ):  # Basic validation
                        self.shared_context.add_raw_extracted_term_pair(
                            src_term, tgt_term
                        )
                else:
                    logger.warning(
                        f"Request ID {request_id}: Skipping malformed item in LLM JSON response: {item}"
                    )

        except json.JSONDecodeError as e:
            logger.error(
                f"Request ID {request_id}: JSON Parsing Error: {e}. Problematic LLM Response after cleaning (start): {cleaned_response_text[:200]}..."
            )
        except Exception as e:
            logger.error(f"Request ID {request_id}: Error processing LLM response: {e}")

    def process_page(
        self,
        page: Page,
        executor: PriorityThreadPoolExecutor,
        pbar: tqdm | None = None,
        tracker: PageTermExtractTracker = None,
    ):
        self.translation_config.raise_if_cancelled()
        paragraphs = []
        total_token_count = 0
        for paragraph in page.pdf_paragraph:

            # REFERENCE SKIP:
            # Bibliography entries should not consume terminology-extraction
            # tokens either.
            if id(paragraph) in self._reference_skip_ids:
                if pbar:
                    pbar.advance(1)
                continue

            if paragraph.debug_id is None or paragraph.unicode is None:
                pbar.advance(1)
                continue
            if is_cid_paragraph(paragraph):
                pbar.advance(1)
                continue
            if is_pure_numeric_paragraph(paragraph):
                pbar.advance(1)
                continue
            if is_placeholder_only_paragraph(paragraph):
                pbar.advance(1)
                continue
            # if len(paragraph.unicode) < self.translation_config.min_text_length:
            #     pbar.advance(1)
            #     continue
            total_token_count += self.calc_token_count(paragraph.unicode)
            paragraphs.append(paragraph)
            if total_token_count > 600 or len(paragraphs) > 12:
                executor.submit(
                    self.extract_terms_from_paragraphs,
                    BatchParagraph(paragraphs, tracker),
                    pbar,
                    total_token_count,
                    priority=1048576 - total_token_count,
                )
                paragraphs = []
                total_token_count = 0

        if paragraphs:
            executor.submit(
                self.extract_terms_from_paragraphs,
                BatchParagraph(paragraphs, tracker),
                pbar,
                total_token_count,
                priority=1048576 - total_token_count,
            )

    def extract_terms_from_paragraphs(
        self,
        paragraphs: BatchParagraph,
        pbar: tqdm | None = None,
        paragraph_token_count: int = 0,
    ):
        self.translation_config.raise_if_cancelled()
        try:
            inputs = [p.unicode for p in paragraphs.paragraphs if p.unicode]
            tracker = paragraphs.tracker
            for u in inputs:
                tracker.append_paragraph_unicode(u)
            if not inputs:
                return

            # Build reference glossary section
            reference_glossary_section = ""
            user_glossaries = self.shared_context.user_glossaries
            if user_glossaries:
                text_for_glossary = "\n\n".join(inputs)

                # Group entries by glossary name
                glossary_entries = {}
                for glossary in user_glossaries:
                    active_entries = glossary.get_active_entries_for_text(
                        text_for_glossary
                    )
                    if active_entries:
                        glossary_entries[glossary.name] = active_entries

                if glossary_entries:
                    reference_glossary_section = (
                        "Reference Glossaries (for consistency and quality):\n"
                    )

                    # Add entries grouped by glossary name
                    for glossary_name, entries in glossary_entries.items():
                        reference_glossary_section += f"\n{glossary_name}:\n"
                        for src, tgt in sorted(set(entries)):
                            reference_glossary_section += f"- {src} → {tgt}\n"

                    reference_glossary_section += "\nPlease consider these existing translations for consistency when extracting new terms. IMPORTANT: You should also extract terms that appear in the reference glossaries above if they are found in the input text - don't skip them just because they already exist in the reference."

            prompt = LLM_PROMPT_TEMPLATE.format(
                target_language=self.translation_config.lang_out,
                text_to_process="\n\n".join(inputs),
                reference_glossary_section=reference_glossary_section,
                example_output="""[
  {"src": "LLM", "tgt": "大语言模型"},
  {"src": "GPT", "tgt": "GPT"}
]""",
            )
            tracker.set_input(prompt)
            output = self.translate_engine.llm_translate(
                prompt,
                rate_limit_params={
                    "paragraph_token_count": paragraph_token_count,
                    "request_json_mode": True,
                },
            )
            tracker.set_output(output)
            cleaned_output = self._clean_json_output(output)
            response = json.loads(cleaned_output)
            if not isinstance(response, list):
                response = [response]  # Ensure we have a list

            for term in response:
                if isinstance(term, dict) and "src" in term and "tgt" in term:
                    src_term = str(term["src"]).strip()
                    tgt_term = str(term["tgt"]).strip()
                    if src_term == tgt_term and len(src_term) < 3:
                        continue
                    if src_term and tgt_term and len(src_term) < 100:
                        self.shared_context.add_raw_extracted_term_pair(
                            src_term, tgt_term
                        )

        except Exception as e:
            logger.warning(f"Error during automatic terms extract: {e}")
            return
        finally:
            pbar.advance(len(paragraphs.paragraphs))

    def procress(self, doc_il: ILDocument):
        logger.info(f"{self.stage_name}: Starting term extraction for document.")

        # REFERENCE SKIP:
        # Detect bibliography before sending text to the terminology LLM.
        self._prepare_reference_skip_ids(doc_il)
        start_total, start_prompt, start_completion, start_cache_hit_prompt = (
            self._snapshot_token_usage()
        )
        tracker = DocumentTermExtractTracker()
        total = sum(len(page.pdf_paragraph) for page in doc_il.page)
        with self.translation_config.progress_monitor.stage_start(
            self.stage_name,
            total,
        ) as pbar:
            max_workers = self.translation_config.term_pool_max_workers
            logger.info(
                f"Using {max_workers} worker threads for automatic term extraction."
            )
            with PriorityThreadPoolExecutor(
                max_workers=max_workers,
            ) as executor:
                for page in doc_il.page:
                    self.process_page(page, executor, pbar, tracker.new_page())

        self.shared_context.finalize_auto_extracted_glossary()
        end_total, end_prompt, end_completion, end_cache_hit_prompt = (
            self._snapshot_token_usage()
        )
        self.translation_config.record_term_extraction_usage(
            end_total - start_total,
            end_prompt - start_prompt,
            end_completion - start_completion,
            end_cache_hit_prompt - start_cache_hit_prompt,
        )

        if (
            self.translation_config.debug
            or self.translation_config.working_dir is not None
        ):
            path = self.translation_config.get_working_file_path(
                "term_extractor_tracking.json"
            )
            logger.debug(f"save translate tracking to {path}")
            with Path(path).open("w", encoding="utf-8") as f:
                f.write(tracker.to_json())

            path = self.translation_config.get_working_file_path(
                "term_extractor_freq.json"
            )
            logger.debug(f"save term frequency to {path}")
            with Path(path).open("w", encoding="utf-8") as f:
                json.dump(
                    self.shared_context.raw_extracted_terms,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            path = self.translation_config.get_working_file_path(
                "auto_extractor_glossary.csv"
            )
            logger.debug(f"save auto extracted glossary to {path}")
            with Path(path).open("w", encoding="utf-8-sig") as f:
                auto_extracted_glossary = self.shared_context.auto_extracted_glossary
                if auto_extracted_glossary:
                    f.write(auto_extracted_glossary.to_csv())
