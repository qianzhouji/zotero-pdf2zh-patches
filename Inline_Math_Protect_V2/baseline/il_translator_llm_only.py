import json
import logging
import re
from pathlib import Path
from string import Template

import Levenshtein
import tiktoken
from tqdm import tqdm

from babeldoc.format.pdf.document_il import Document
from babeldoc.format.pdf.document_il import Page
from babeldoc.format.pdf.document_il import PdfFont
from babeldoc.format.pdf.document_il import PdfParagraph
from babeldoc.format.pdf.document_il.midend import il_translator
from babeldoc.format.pdf.document_il.midend.il_translator import (
    DocumentTranslateTracker,
)
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from babeldoc.format.pdf.document_il.midend.il_translator import PageTranslateTracker
from babeldoc.format.pdf.document_il.midend.il_translator import (
    ParagraphTranslateTracker,
)
from babeldoc.format.pdf.document_il.utils.fontmap import FontMapper
from babeldoc.format.pdf.document_il.utils.paragraph_helper import is_cid_paragraph
from babeldoc.format.pdf.document_il.utils.paragraph_helper import (
    is_placeholder_only_paragraph,
)
from babeldoc.format.pdf.document_il.utils.paragraph_helper import (
    is_pure_numeric_paragraph,
)
from babeldoc.format.pdf.translation_config import TitleContextSnapshot
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.translator.translator import BaseTranslator
from babeldoc.utils.priority_thread_pool_executor import PriorityThreadPoolExecutor

logger = logging.getLogger(__name__)


PROMPT_TEMPLATE = Template(
    """$role_block

## Structure Rules
1. Keep **the same number of paragraphs as the input**.
2. Input paragraphs may be **sliced pieces of the same original paragraph**.  
   → You MUST treat each input paragraph **as an independent, fixed unit**.  
   → Do NOT merge paragraphs, split paragraphs, or move content between paragraphs.
3. Inside each paragraph, you may adjust word order for fluency, but:
   - Do NOT change the meaning.
   - Do NOT move placeholders, tags, or code outside their paragraph.
   - Markers like [[CITE:v1:96,120]] are immutable inline citations or numeric superscripts.
   - Copy every CITE marker EXACTLY ONCE.
   - Keep each CITE marker attached to the SAME preceding word, phrase, or clause as in the input.
   - NEVER move a CITE marker to the beginning of a sentence, before a verb, or onto another phrase.
4. Translate ALL human-readable content into $lang_out.

## Do NOT Modify
- Tags (e.g., <style>, <b>, <code>): keep them exactly the same.  
  *Translate tag-internal text except code blocks (<code>…</code>)*.
- Placeholders: `{v1}`, `{name}`, `%s`, `%d`, `[[...]]`, `%%...%%` — keep exactly unchanged.
- JSON keys or structure.

$glossary_usage_rules_block
## Output Format
Return a JSON array of the same length.  
For each item:
- Keep the same "id" and remove other fields like "input" and "layout_label".
- Add "output" with the translated text only.
- No extra text, no ```json blocks.

## Style
- Produce fluent, professional $lang_out.
- Preserve punctuation unless needed for target language fluency.

### Example
Input:
[
    {
    "id": 0,
    "input": "{v1}<style id='2'>hello</style>, world!",
    "layout_label": "text"
    }
]
Output:
[
    {
    "id": 0,
    "output": "{v1}<style id='2'>你好</style>，世界！"
    }
]

$contextual_hints_block

$glossary_tables_block

## Here is the input:

$json_input_str"""
)


class BatchParagraph:
    def __init__(
        self,
        paragraphs: list[PdfParagraph],
        pages: list[Page],
        page_tracker: PageTranslateTracker,
    ):
        self.paragraphs = paragraphs
        self.pages = pages
        self.trackers = [page_tracker.new_paragraph() for _ in paragraphs]


class ILTranslatorLLMOnly:
    stage_name = "Translate Paragraphs"

    def __init__(
        self,
        translate_engine: BaseTranslator,
        translation_config: TranslationConfig,
        tokenizer=None,
    ):
        self.translate_engine = translate_engine
        self.translation_config = translation_config
        self.font_mapper = FontMapper(translation_config)
        self.shared_context_cross_split_part = (
            translation_config.shared_context_cross_split_part
        )

        if tokenizer is None:
            self.tokenizer = tiktoken.encoding_for_model("gpt-4o")
        else:
            self.tokenizer = tokenizer

        # Cache glossaries at initialization
        self._cached_glossaries = (
            self.shared_context_cross_split_part.get_glossaries_for_translation(
                translation_config.auto_extract_glossary
            )
        )

        self.il_translator = ILTranslator(
            translate_engine=translate_engine,
            translation_config=translation_config,
            tokenizer=self.tokenizer,
        )
        self.il_translator.use_as_fallback = True
        try:
            self.translate_engine.do_llm_translate(None)
        except NotImplementedError as e:
            raise ValueError("LLM translator not supported") from e

        self.ok_count = 0
        self.fallback_count = 0
        self.total_count = 0
        self._reference_skip_ids: set[int] = set()



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

    def find_title_paragraph(self, docs: Document) -> PdfParagraph | None:
        """Find the first paragraph with layout_label 'title' in the document.

        Args:
            docs: The document to search in

        Returns:
            The first title paragraph found, or None if no title paragraph exists
        """
        for page in docs.page:
            for paragraph in page.pdf_paragraph:
                if paragraph.layout_label == "title":
                    logger.info(f"Found title paragraph: {paragraph.unicode}")
                    return paragraph
        return None

    def translate(self, docs: Document) -> None:
        self.il_translator.docs = docs
        tracker = DocumentTranslateTracker()
        self.mid = 0

        # REFERENCE SKIP:
        # Detect bibliography before any translation task is submitted.
        self._prepare_reference_skip_ids(docs)

        if not self.translation_config.shared_context_cross_split_part.first_paragraph:
            # Try to find the first title paragraph
            title_paragraph = self.find_title_paragraph(docs)
            self.translation_config.shared_context_cross_split_part.first_paragraph = (
                self.shared_context_cross_split_part.snapshot_title_paragraph(
                    title_paragraph
                )
            )
            self.translation_config.shared_context_cross_split_part.recent_title_paragraph = self.shared_context_cross_split_part.snapshot_title_paragraph(
                title_paragraph
            )
            if title_paragraph:
                logger.info(f"Found first title paragraph: {title_paragraph.unicode}")

        # count total paragraph
        total = sum(
            [
                len(
                    [
                        p
                        for p in page.pdf_paragraph
                        if p.debug_id is not None and p.unicode is not None
                    ]
                )
                for page in docs.page
            ]
        )
        translated_ids = set()
        with self.translation_config.progress_monitor.stage_start(
            self.stage_name,
            total,
        ) as pbar:
            with PriorityThreadPoolExecutor(
                max_workers=self.translation_config.pool_max_workers,
            ) as executor2:
                with PriorityThreadPoolExecutor(
                    max_workers=self.translation_config.pool_max_workers,
                ) as executor:
                    self.process_cross_page_paragraph(
                        docs,
                        executor,
                        pbar,
                        tracker,
                        executor2,
                        translated_ids,
                    )
                    # Cross-column detection per page (after cross-page processing)
                    for page in docs.page:
                        self.process_cross_column_paragraph(
                            page,
                            executor,
                            pbar,
                            tracker,
                            executor2,
                            translated_ids,
                        )
                    for page in docs.page:
                        self.process_page(
                            page,
                            executor,
                            pbar,
                            tracker.new_page(),
                            executor2,
                            translated_ids,
                        )

        path = self.translation_config.get_working_file_path("translate_tracking.json")

        if (
            self.translation_config.debug
            or self.translation_config.working_dir is not None
        ):
            logger.debug(f"save translate tracking to {path}")
            with Path(path).open("w", encoding="utf-8") as f:
                f.write(tracker.to_json())
        logger.info(
            f"Translation completed. Total: {self.total_count}, Successful: {self.ok_count}, Fallback: {self.fallback_count}"
        )

    def _is_body_text_paragraph(self, paragraph: PdfParagraph) -> bool:
        """判断正文段落（当前仅 layout_label == 'text'）。

        Args:
            paragraph: PDF paragraph to check

        Returns:
            True if this is a body text paragraph, False otherwise
        """
        return paragraph.layout_label in (
            "text",
            "plain text",
            "paragraph_hybrid",
        )

    def _should_translate_paragraph(
        self,
        paragraph: PdfParagraph,
        translated_ids: set[int] | None = None,
        require_body_text: bool = False,
    ) -> bool:
        """Check if a paragraph should be translated based on common filtering criteria.

        Args:
            paragraph: PDF paragraph to check
            translated_ids: Set of already translated paragraph IDs
            require_body_text: Whether to additionally check if paragraph is body text

        Returns:
            True if paragraph should be translated, False otherwise
        """
        # Basic validation checks
        if paragraph.debug_id is None or paragraph.unicode is None:
            return False

        # Check if already translated
        if translated_ids is not None and id(paragraph) in translated_ids:
            return False

        # REFERENCE SKIP
        if id(paragraph) in self._reference_skip_ids:
            return False

        # CID paragraph check
        if is_cid_paragraph(paragraph):
            return False

        # Minimum length check
        if len(paragraph.unicode) < self.translation_config.min_text_length:
            return False

        # Body text check if requested
        if require_body_text and not self._is_body_text_paragraph(paragraph):
            return False

        return True

    def _filter_paragraphs(
        self,
        page: Page,
        translated_ids: set[int] | None = None,
        require_body_text: bool = False,
    ) -> list[PdfParagraph]:
        """Get list of paragraphs that should be translated from a page.

        Args:
            page: Page to get paragraphs from
            translated_ids: Set of already translated paragraph IDs
            require_body_text: Whether to filter for body text paragraphs only

        Returns:
            List of paragraphs that should be translated
        """
        return [
            paragraph
            for paragraph in page.pdf_paragraph
            if self._should_translate_paragraph(
                paragraph, translated_ids, require_body_text
            )
        ]

    def _build_font_maps(
        self, page: Page
    ) -> tuple[dict[str, PdfFont], dict[int, dict[str, PdfFont]]]:
        """Build font maps for a page.

        Args:
            page: The page to build font maps for

        Returns:
            Tuple of (page_font_map, page_xobj_font_map)
        """
        page_font_map = {}
        for font in page.pdf_font:
            page_font_map[font.font_id] = font

        page_xobj_font_map = {}
        for xobj in page.pdf_xobject:
            page_xobj_font_map[xobj.xobj_id] = page_font_map.copy()
            for font in xobj.pdf_font:
                page_xobj_font_map[xobj.xobj_id][font.font_id] = font

        return page_font_map, page_xobj_font_map

    def process_cross_page_paragraph(
        self,
        docs: Document,
        executor: PriorityThreadPoolExecutor,
        pbar: tqdm | None = None,
        tracker: DocumentTranslateTracker | None = None,
        executor2: PriorityThreadPoolExecutor | None = None,
        translated_ids: set[int] | None = None,
    ):
        """Process cross-page paragraphs by combining last body text paragraph of current page
        with first body text paragraph of next page.

        Args:
            docs: Document containing pages to process
            executor: Thread pool executor for translation tasks
            pbar: Progress bar for tracking translation progress
            tracker: Page translation tracker
            executor2: Secondary executor for fallback translation
            translated_ids: Set of already translated paragraph IDs
        """
        self.translation_config.raise_if_cancelled()

        if tracker is None:
            tracker = DocumentTranslateTracker()

        if translated_ids is None:
            translated_ids = set()

        # Process adjacent page pairs
        for i in range(len(docs.page) - 1):
            page_curr = docs.page[i]
            page_next = docs.page[i + 1]

            # Find body text paragraphs in current page
            curr_body_paragraphs = self._filter_paragraphs(
                page_curr, translated_ids, require_body_text=True
            )

            # Find body text paragraphs in next page
            next_body_paragraphs = self._filter_paragraphs(
                page_next, translated_ids, require_body_text=True
            )

            # Get last paragraph from current page and first paragraph from next page
            if not curr_body_paragraphs or not next_body_paragraphs:
                continue

            last_curr_paragraph = curr_body_paragraphs[-1]
            first_next_paragraph = next_body_paragraphs[0]

            # Skip if either paragraph is already translated
            if (
                id(last_curr_paragraph) in translated_ids
                or id(first_next_paragraph) in translated_ids
            ):
                continue

            # Build font maps for both pages
            curr_font_map, curr_xobj_font_map = self._build_font_maps(page_curr)
            next_font_map, next_xobj_font_map = self._build_font_maps(page_next)

            # Merge font maps
            merged_font_map = {**curr_font_map, **next_font_map}
            merged_xobj_font_map = {**curr_xobj_font_map, **next_xobj_font_map}

            # Calculate total token count
            total_token_count = self.calc_token_count(
                last_curr_paragraph.unicode
            ) + self.calc_token_count(first_next_paragraph.unicode)

            # Create batch with both paragraphs
            cross_page_paragraphs = [last_curr_paragraph, first_next_paragraph]
            cross_page_pages = [page_curr, page_next]
            batch_paragraph = BatchParagraph(
                cross_page_paragraphs, cross_page_pages, tracker.new_cross_page()
            )

            self.mid += 1
            # Submit translation task (force submit regardless of token count)
            executor.submit(
                self.translate_paragraph,
                batch_paragraph,
                pbar,
                merged_font_map,
                merged_xobj_font_map,
                self.translation_config.shared_context_cross_split_part.first_paragraph,
                self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                executor2,
                priority=1048576 - total_token_count,
                paragraph_token_count=total_token_count,
                mp_id=self.mid,
            )

            # Mark paragraphs as translated
            translated_ids.add(id(last_curr_paragraph))
            translated_ids.add(id(first_next_paragraph))

    def process_cross_column_paragraph(
        self,
        page: Page,
        executor: PriorityThreadPoolExecutor,
        pbar: tqdm | None = None,
        tracker: DocumentTranslateTracker | None = None,
        executor2: PriorityThreadPoolExecutor | None = None,
        translated_ids: set[int] | None = None,
    ):
        """Process cross-column paragraphs within the same page.

        If two adjacent body-text paragraphs have a gap in their y2 coordinate
        greater than 20 units, they are considered split across columns and
        will be translated together.
        """
        self.translation_config.raise_if_cancelled()

        if tracker is None:
            tracker = DocumentTranslateTracker()
        if translated_ids is None:
            translated_ids = set()

        # Filter body-text paragraphs maintaining original order
        body_paragraphs = self._filter_paragraphs(
            page, translated_ids, require_body_text=True
        )
        if len(body_paragraphs) < 2:
            return

        # Build font maps once for the whole page
        page_font_map, page_xobj_font_map = self._build_font_maps(page)

        for idx in range(len(body_paragraphs) - 1):
            p1 = body_paragraphs[idx]
            p2 = body_paragraphs[idx + 1]

            # Skip already translated
            if id(p1) in translated_ids or id(p2) in translated_ids:
                continue

            # Safety checks for box information
            if not (
                p1.box and p2.box and p1.box.y2 is not None and p2.box.y2 is not None
            ):
                continue

            if p2.box.y2 - p1.box.y2 <= 20:
                continue

            total_token_count = self.calc_token_count(
                p1.unicode
            ) + self.calc_token_count(p2.unicode)

            batch = BatchParagraph([p1, p2], [page, page], tracker.new_cross_column())
            self.mid += 1
            executor.submit(
                self.translate_paragraph,
                batch,
                pbar,
                page_font_map,
                page_xobj_font_map,
                self.translation_config.shared_context_cross_split_part.first_paragraph,
                self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                executor2,
                priority=1048576 - total_token_count,
                paragraph_token_count=total_token_count,
                mp_id=self.mid,
            )

            translated_ids.add(id(p1))
            translated_ids.add(id(p2))

    def process_page(
        self,
        page: Page,
        executor: PriorityThreadPoolExecutor,
        pbar: tqdm | None = None,
        tracker: PageTranslateTracker = None,
        executor2: PriorityThreadPoolExecutor | None = None,
        translated_ids: set | None = None,
    ):
        self.translation_config.raise_if_cancelled()
        page_font_map = {}
        for font in page.pdf_font:
            page_font_map[font.font_id] = font
        page_xobj_font_map = {}
        for xobj in page.pdf_xobject:
            page_xobj_font_map[xobj.xobj_id] = page_font_map.copy()
            for font in xobj.pdf_font:
                page_xobj_font_map[xobj.xobj_id][font.font_id] = font

        paragraphs = []

        total_token_count = 0
        for paragraph in page.pdf_paragraph:

            # REFERENCE SKIP:
            # Keep bibliography text exactly as in the source PDF and do not
            # send it to the LLM.
            if id(paragraph) in self._reference_skip_ids:
                if translated_ids is not None:
                    translated_ids.add(id(paragraph))
                if pbar:
                    pbar.advance(1)
                continue

            # Check if already translated
            if id(paragraph) in translated_ids:
                continue

            # Check basic validation
            if paragraph.debug_id is None or paragraph.unicode is None:
                continue

            # Check CID paragraph - advance progress bar if filtered out
            if is_cid_paragraph(paragraph):
                if pbar:
                    pbar.advance(1)
                continue

            # Check minimum length - advance progress bar if filtered out
            if len(paragraph.unicode) < self.translation_config.min_text_length:
                if pbar:
                    pbar.advance(1)
                continue

            if is_pure_numeric_paragraph(paragraph):
                if pbar:
                    pbar.advance(1)
                continue

            if is_placeholder_only_paragraph(paragraph):
                if pbar:
                    pbar.advance(1)
                continue

            # self.translate_paragraph(paragraph, pbar,tracker.new_paragraph(), page_font_map, page_xobj_font_map)
            total_token_count += self.calc_token_count(paragraph.unicode)
            paragraphs.append(paragraph)
            translated_ids.add(id(paragraph))
            if paragraph.layout_label == "title":
                self.shared_context_cross_split_part.recent_title_paragraph = (
                    self.shared_context_cross_split_part.snapshot_title_paragraph(
                        paragraph
                    )
                )

            if total_token_count > 200 or len(paragraphs) > 5:
                self.mid += 1
                executor.submit(
                    self.translate_paragraph,
                    BatchParagraph(paragraphs, [page] * len(paragraphs), tracker),
                    pbar,
                    page_font_map,
                    page_xobj_font_map,
                    self.translation_config.shared_context_cross_split_part.first_paragraph,
                    self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                    executor2,
                    priority=1048576 - total_token_count,
                    paragraph_token_count=total_token_count,
                    mp_id=self.mid,
                )
                paragraphs = []
                total_token_count = 0

        if paragraphs:
            self.mid += 1
            executor.submit(
                self.translate_paragraph,
                BatchParagraph(paragraphs, [page] * len(paragraphs), tracker),
                pbar,
                page_font_map,
                page_xobj_font_map,
                self.translation_config.shared_context_cross_split_part.first_paragraph,
                self.translation_config.shared_context_cross_split_part.recent_title_paragraph,
                executor2,
                priority=1048576 - total_token_count,
                paragraph_token_count=total_token_count,
                mp_id=self.mid,
            )

    def translate_paragraph(
        self,
        batch_paragraph: BatchParagraph,
        pbar: tqdm | None = None,
        page_font_map: dict[str, PdfFont] = None,
        xobj_font_map: dict[int, dict[str, PdfFont]] = None,
        title_paragraph: TitleContextSnapshot | None = None,
        local_title_paragraph: TitleContextSnapshot | None = None,
        executor: PriorityThreadPoolExecutor | None = None,
        paragraph_token_count: int = 0,
        mp_id: int = 0,
    ):
        """Translate a paragraph using pre and post processing functions."""
        self.translation_config.raise_if_cancelled()
        should_translate_paragraph = []
        try:
            inputs = []
            llm_translate_trackers = []
            paragraph_unicodes = []
            for i in range(len(batch_paragraph.paragraphs)):
                paragraph = batch_paragraph.paragraphs[i]
                tracker = batch_paragraph.trackers[i]
                text, translate_input = self.il_translator.pre_translate_paragraph(
                    paragraph, tracker, page_font_map, xobj_font_map
                )
                if text is None:
                    pbar.advance(1)
                    continue

                tracker.record_multi_paragraph_id(mp_id)

                llm_translate_tracker = tracker.new_llm_translate_tracker()
                should_translate_paragraph.append(i)
                llm_translate_trackers.append(llm_translate_tracker)
                inputs.append(
                    (
                        text,
                        translate_input,
                        paragraph,
                        tracker,
                        llm_translate_tracker,
                        paragraph_unicodes,
                    )
                )
                paragraph_unicodes.append(paragraph.unicode)
            if not inputs:
                return
            json_format_input = []

            for id_, input_text in enumerate(inputs):
                ti: il_translator.ILTranslator.TranslateInput = input_text[1]
                tracker: ParagraphTranslateTracker = input_text[3]
                tracker.record_multi_paragraph_index(id_)
                placeholders_hint = ti.get_placeholders_hint()


                # CITATION PROTECT:
                # Convert numeric superscript/formula placeholders such as
                # {v1} -> [[CITE:v1:96,120]]
                # so the LLM knows this is an immutable inline citation.
                protected_input = input_text[0]
                protected_refs = []

                if placeholders_hint:
                    for _ph, _value in placeholders_hint.items():
                        _value_s = str(_value).strip()

                        # Matches:
                        # 22
                        # 96,120
                        # 82–84
                        # 143-145
                        # 20,98,99
                        if re.fullmatch(
                            r"\d+(?:\s*[-–—]\s*\d+)?"
                            r"(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*(?:\s*,)?",
                            _value_s,
                        ):
                            _pid = _ph.strip("{}")
                            _marker = f"[[CITE:{_pid}:{_value_s}]]"
                            protected_input = protected_input.replace(_ph, _marker)
                            protected_refs.append((_ph, _value_s))

                if protected_refs:
                    print(
                        f"[CITATION-PROTECT] id={id_} refs={protected_refs!r}",
                        flush=True,
                    )

                obj = {
                    "id": id_,
                    "input": protected_input,
                    "layout_label": input_text[2].layout_label,
                }
                if (
                    placeholders_hint
                    and self.translation_config.add_formula_placehold_hint
                ):
                    obj["formula_placeholders_hint"] = placeholders_hint
                json_format_input.append(obj)

            json_format_input_str = json.dumps(
                json_format_input, ensure_ascii=False, indent=2
            )

            batch_text_for_glossary_matching = "\n".join(
                item.get("input", "") for item in json_format_input
            )

            final_input = self._build_llm_prompt(
                json_input_str=json_format_input_str,
                title_paragraph=title_paragraph,
                local_title_paragraph=local_title_paragraph,
                batch_text_for_glossary_matching=batch_text_for_glossary_matching,
            )

            for llm_translate_tracker in llm_translate_trackers:
                llm_translate_tracker.set_input(final_input)
            llm_output = self.translate_engine.llm_translate(
                final_input,
                rate_limit_params={
                    "paragraph_token_count": paragraph_token_count,
                    "request_json_mode": True,
                },
            )
            for llm_translate_tracker in llm_translate_trackers:
                llm_translate_tracker.set_output(llm_output)
            llm_output = llm_output.strip()

            llm_output = self._clean_json_output(llm_output)

            parsed_output = json.loads(llm_output)

            if isinstance(parsed_output, dict) and parsed_output.get(
                "output", parsed_output.get("input", False)
            ):
                parsed_output = [parsed_output]

            translation_results = {
                item["id"]: item.get("output", item.get("input"))
                for item in parsed_output
            }

            if len(translation_results) != len(inputs):
                raise Exception(
                    f"Translation results length mismatch. Expected: {len(inputs)}, Got: {len(translation_results)}"
                )

            for id_, output in translation_results.items():
                should_fallback = True
                try:
                    if not isinstance(output, str):
                        logger.warning(
                            f"Translation result is not a string. Output: {output}"
                        )
                        continue

                    id_ = int(id_)  # Ensure id is an integer
                    if id_ >= len(inputs):
                        logger.warning(f"Invalid id {id_}, skipping")
                        continue

                    # Clean up any excessive punctuation in the translated text
                    translated_text = re.sub(r"[. 。…，]{20,}", ".", output)

                    # Get the original input for this translation
                    translate_input = inputs[id_][1]
                    llm_translate_tracker = inputs[id_][4]

                    # CITATION PROTECT:
                    # Verify numeric citation markers survived the LLM,
                    # then restore them to BabelDOC's original {vN} placeholders.
                    _citation_value_re = re.compile(
                        r"^\d+(?:\s*[-–—]\s*\d+)?"
                        r"(?:\s*,\s*\d+(?:\s*[-–—]\s*\d+)?)*(?:\s*,)?$"
                    )
                    _citation_marker_re = re.compile(
                        r"\[\[\s*CITE\s*:\s*(v\d+)\s*:"
                        r"\s*([0-9,\s\-–—]+)\s*\]\]"
                    )

                    _expected_cite_ids = []
                    _hints = translate_input.get_placeholders_hint()

                    if _hints:
                        for _ph, _value in _hints.items():
                            _value_s = str(_value).strip()
                            if _citation_value_re.fullmatch(_value_s):
                                _expected_cite_ids.append(_ph.strip("{}"))

                    if _expected_cite_ids:
                        _actual = _citation_marker_re.findall(translated_text)
                        _actual_cite_ids = [x[0] for x in _actual]

                        if _actual_cite_ids != _expected_cite_ids:
                            raise ValueError(
                                "Citation marker mismatch: "
                                f"expected={_expected_cite_ids}, "
                                f"actual={_actual_cite_ids}, "
                                f"output={translated_text!r}"
                            )

                        translated_text = _citation_marker_re.sub(
                            lambda m: "{" + m.group(1) + "}",
                            translated_text,
                        )

                        print(
                            f"[CITATION-PROTECT] restored "
                            f"{len(_expected_cite_ids)} citation marker(s)",
                            flush=True,
                        )

                    input_unicode = inputs[id_][0]
                    output_unicode = translated_text

                    trimed_input = re.sub(r"[. 。…，]{20,}", ".", input_unicode)

                    input_token_count = self.calc_token_count(trimed_input)
                    output_token_count = self.calc_token_count(output_unicode)

                    same_as_input = trimed_input == output_unicode
                    if (
                        same_as_input
                        and input_token_count > 10
                        and not self.translation_config.disable_same_text_fallback
                    ):
                        llm_translate_tracker.set_error_message(
                            "Translation result is the same as input, fallback."
                        )
                        llm_translate_tracker.set_placeholder_full_match()
                        logger.warning(
                            "Translation result is the same as input, fallback."
                        )
                        continue

                    if not (0.3 < output_token_count / input_token_count < 3):
                        llm_translate_tracker.set_error_message(
                            f"Translation result is too long or too short. Input: {input_token_count}, Output: {output_token_count}"
                        )
                        logger.warning(
                            f"Translation result is too long or too short. Input: {input_token_count}, Output: {output_token_count}"
                        )
                        llm_translate_tracker.set_placeholder_full_match()
                        continue

                    if not self.translation_config.disable_same_text_fallback:
                        edit_distance = Levenshtein.distance(
                            input_unicode, output_unicode
                        )
                        if edit_distance < 5 and input_token_count > 20:
                            llm_translate_tracker.set_error_message(
                                f"Translation result edit distance is too small. distance: {edit_distance}, input: {input_unicode}, output: {output_unicode}"
                            )
                            logger.warning(
                                f"Translation result edit distance is too small. distance: {edit_distance}, input: {input_unicode}, output: {output_unicode}"
                            )
                            llm_translate_tracker.set_placeholder_full_match()
                            continue
                    # Apply the translation to the paragraph
                    self.il_translator.post_translate_paragraph(
                        inputs[id_][2],
                        inputs[id_][3],
                        translate_input,
                        translated_text,
                    )
                    should_fallback = False
                    if pbar:
                        pbar.advance(1)
                except Exception as e:
                    error_message = f"Error translating paragraph. Error: {e}."
                    logger.exception(error_message)
                    # Ignore error and continue
                    for llm_translate_tracker in llm_translate_trackers:
                        llm_translate_tracker.set_error_message(error_message)
                    continue
                finally:
                    self.total_count += 1
                    if should_fallback:
                        self.fallback_count += 1
                        inputs[id_][4].set_fallback_to_translate()
                        logger.warning(
                            f"Fallback to simple translation. paragraph id: {inputs[id_][2].debug_id}"
                        )
                        paragraph_token_count = self.calc_token_count(
                            inputs[id_][2].unicode
                        )
                        paragraph_unicodes = inputs[id_][5]
                        inputs[id_][2].unicode = paragraph_unicodes[id_]
                        executor.submit(
                            self.il_translator.translate_paragraph,
                            inputs[id_][2],
                            batch_paragraph.pages[id_],
                            pbar,
                            inputs[id_][3],
                            page_font_map,
                            xobj_font_map,
                            priority=1048576 - paragraph_token_count,
                            paragraph_token_count=paragraph_token_count,
                            title_paragraph=title_paragraph,
                            local_title_paragraph=local_title_paragraph,
                        )
                    else:
                        self.ok_count += 1

        except Exception as e:
            error_message = f"Error {e} during translation. try fallback"
            logger.warning(error_message)
            for llm_translate_tracker in llm_translate_trackers:
                llm_translate_tracker.set_error_message(error_message)
                llm_translate_tracker.set_fallback_to_translate()
            self.total_count += len(llm_translate_trackers)
            self.fallback_count += len(llm_translate_trackers)
            for input_ in inputs:
                input_[2].unicode = input_[5]
            if not should_translate_paragraph:
                should_translate_paragraph = list(
                    range(len(batch_paragraph.paragraphs))
                )
            for i in should_translate_paragraph:
                paragraph = batch_paragraph.paragraphs[i]
                tracker = batch_paragraph.trackers[i]
                if paragraph.debug_id is None:
                    continue
                paragraph_token_count = self.calc_token_count(paragraph.unicode)
                executor.submit(
                    self.il_translator.translate_paragraph,
                    paragraph,
                    batch_paragraph.pages[i],
                    pbar,
                    tracker,
                    page_font_map,
                    xobj_font_map,
                    priority=1048576 - paragraph_token_count,
                    paragraph_token_count=paragraph_token_count,
                    title_paragraph=title_paragraph,
                    local_title_paragraph=local_title_paragraph,
                )

    def _build_llm_prompt(
        self,
        json_input_str: str,
        title_paragraph: TitleContextSnapshot | None,
        local_title_paragraph: TitleContextSnapshot | None,
        batch_text_for_glossary_matching: str,
    ) -> str:
        """Build LLM prompt using a single template for easier maintenance."""
        # Build role block, honoring custom_system_prompt if provided.
        custom_prompt = getattr(self.translation_config, "custom_system_prompt", None)
        if custom_prompt:
            role_block = custom_prompt.strip()
            if "Follow all rules strictly." not in role_block:
                if not role_block.endswith("\n"):
                    role_block += "\n"
                role_block += "Follow all rules strictly."
        else:
            role_block = (
                f"You are a professional {self.translation_config.lang_out} native translator who needs to fluently translate text "
                f"into {self.translation_config.lang_out}.\n\n"
                "Follow all rules strictly."
            )

        # Build contextual hints section.
        contextual_lines: list[str] = []
        hint_idx = 1
        if title_paragraph:
            contextual_lines.append(
                f"{hint_idx}. First title in full text: {title_paragraph.unicode}"
            )
            hint_idx += 1

        if local_title_paragraph:
            is_different_from_global = True
            if title_paragraph:
                if local_title_paragraph.debug_id == title_paragraph.debug_id:
                    is_different_from_global = False

            if is_different_from_global:
                contextual_lines.append(
                    f"{hint_idx}. The most recent title is: {local_title_paragraph.unicode}"
                )

        if contextual_lines:
            contextual_hints_block = (
                "## Contextual Hints for Better Translation\n"
                + "\n".join(contextual_lines)
                + "\n"
            )
        else:
            contextual_hints_block = ""

        # Build glossary usage rules and glossary tables.
        glossary_usage_rules_block = ""
        glossary_tables_block = ""
        glossary_entries_per_glossary: dict[str, list[tuple[str, str]]] = {}

        if self._cached_glossaries:
            for glossary in self._cached_glossaries:
                active_entries = glossary.get_active_entries_for_text(
                    batch_text_for_glossary_matching
                )
                if active_entries:
                    glossary_entries_per_glossary[glossary.name] = sorted(
                        active_entries
                    )

        if glossary_entries_per_glossary:
            glossary_usage_rules_block = (
                "## Glossary\n"
                "If a glossary is provided:\n"
                "- Always use the exact target term.\n"
                "- Apply glossary items even inside tags or when broken by hyphens/line breaks.\n"
                "- If glossary does NOT include a term, translate it naturally.\n\n"
            )

            glossary_table_lines: list[str] = ["## Glossary Tables", ""]
            for glossary_name, entries in glossary_entries_per_glossary.items():
                glossary_table_lines.append(f"### Glossary: {glossary_name}")
                glossary_table_lines.append("")
                glossary_table_lines.append(
                    "| Source Term | Target Term |\n|-------------|-------------|"
                )
                for original_source, target_text in entries:
                    glossary_table_lines.append(
                        f"| {original_source} | {target_text} |"
                    )
                glossary_table_lines.append("")
            glossary_tables_block = "\n".join(glossary_table_lines)

        return PROMPT_TEMPLATE.substitute(
            role_block=role_block,
            glossary_usage_rules_block=glossary_usage_rules_block,
            contextual_hints_block=contextual_hints_block,
            json_input_str=json_input_str,
            glossary_tables_block=glossary_tables_block,
            lang_out=self.translation_config.lang_out,
        )

    def _clean_json_output(self, llm_output: str) -> str:
        # Clean up JSON output by removing common wrapper tags
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
