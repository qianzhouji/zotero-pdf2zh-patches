"""Local Inline Math Protect V2: marker checks, audit, and page boundary filter.

Contains no network calls. The caller owns the single guarded retry policy.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = "2.0"
_MARKER = re.compile(
    r"\[\[\s*(MATH\s*:\s*\d+|CITE\s*:\s*v\d+\s*:\s*[0-9,\s\-–—]+)\s*\]\]",
    re.IGNORECASE,
)


def canonical_marker(value):
    value = re.sub(r"\s+", "", value)
    return re.sub(r"(?i)MATH|CITE", lambda m: m.group().upper(), value).replace(":V", ":v")


def parse_outputs(value, expected_count):
    """Reject missing, duplicate, non-string, or unexpected paragraph results."""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("Translation response must be a JSON array")
    result = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Translation response item must be an object")
        index = item.get("id")
        if isinstance(index, str) and re.fullmatch(r"\d+", index):
            index = int(index)
        if type(index) is not int or index not in range(expected_count):
            raise ValueError(f"Unexpected paragraph id: {index!r}")
        if index in result:
            raise ValueError(f"Duplicate paragraph id: {index}")
        output = item.get("output", item.get("input"))
        if not isinstance(output, str):
            raise ValueError(f"Non-string output for paragraph {index}")
        result[index] = output
    if set(result) != set(range(expected_count)):
        raise ValueError(f"Missing paragraph ids: {sorted(set(range(expected_count)) - set(result))}")
    return result


def restore_markers(source, protected_input, output, marker_map):
    """Validate one combined CITE/MATH sequence before restoring PDF objects."""
    marker_map = {canonical_marker(k): v for k, v in marker_map.items()}
    expected = [canonical_marker(m.group()) for m in _MARKER.finditer(protected_input)]
    actual = [canonical_marker(m.group()) for m in _MARKER.finditer(output)]
    if actual != expected:
        raise ValueError(f"Protected marker mismatch: expected={expected}, actual={actual}")
    restored = _MARKER.sub(lambda m: marker_map[canonical_marker(m.group())], output)
    # Detect raw formula placeholders inserted in addition to the protected token.
    for placeholder in set(marker_map.values()):
        if restored.count(placeholder) != source.count(placeholder):
            raise ValueError(f"Formula placeholder count mismatch: {placeholder}")
    return restored


def locked_slots(text):
    """Separate immutable PDF markers from translatable prose, in source order."""
    parts, requests = [], []
    end = 0
    for marker in list(_MARKER.finditer(text)) + [None]:
        stop = marker.start() if marker else len(text)
        prose = text[end:stop]
        without_tags = re.sub(r'<[^>]*>', '', prose)
        if any(c.isalpha() for c in without_tags):
            index = len(requests)
            requests.append({'id': index, 'input': prose, 'layout_label': 'locked_text_slot'})
            parts.append(('slot', index))
        else:
            parts.append(('fixed', prose))
        if marker:
            parts.append(('fixed', marker.group()))
            end = marker.end()
    return parts, requests


def assemble_slots(parts, requests, results):
    for item in requests:
        value = results[item['id']]
        if re.search(r'\[\[\s*(?:MATH|CITE)\s*:', value, re.I):
            raise ValueError('Model inserted a protected marker into a prose slot')
        source_letters = sum(c.isalpha() for c in re.sub(r'<[^>]*>', '', item['input']))
        if source_letters >= 12 and not any(c.isalpha() for c in re.sub(r'<[^>]*>', '', value)):
            raise ValueError(f"Model omitted prose slot {item['id']}: {item['input']!r}")
    return ''.join(results[value] if kind == 'slot' else value for kind, value in parts)


class GuardAudit:
    def __init__(self, input_file):
        self.run_id = uuid.uuid4().hex[:12]
        self.input_file = str(input_file)
        self.path = Path(os.environ.get(
            "PDF2ZH_INLINE_MATH_LOG",
            str(Path.home() / "zotero-pdf2zh/server/inline-math-protect.jsonl"),
        ))
        self.counts = Counter()
        self.lock = threading.Lock()
        self._io_warning_printed = False

    def record(self, event, **fields):
        with self.lock:
            self.counts[event] += 1
            row = {"time": datetime.now(timezone.utc).isoformat(), "version": VERSION,
                   "run_id": self.run_id, "file": self.input_file, "event": event, **fields}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                blob = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, blob)
                finally:
                    os.close(fd)
            except OSError as exc:
                if not self._io_warning_printed:
                    print(f"[INLINE-MATH-PROTECT] audit write failed: {type(exc).__name__}", flush=True)
                    self._io_warning_printed = True
        if event in {"retry", "source_preserved", "offpage_filtered", "summary"}:
            # One stdout line survives Rich's narrow-column terminal wrapping.
            brief = {k: v for k, v in fields.items() if k not in {"source", "input", "output", "reason"}}
            print(f"[INLINE-MATH-PROTECT] {self.run_id} {event} {json.dumps(brief, ensure_ascii=False)}", flush=True)

    def summary(self):
        with self.lock:
            counts = dict(self.counts)
        self.record("summary", counts=counts, audit_path=str(self.path))


def get_audit(config):
    # Initialized by the parser before worker threads start.
    if not hasattr(config, "_inline_math_protect_audit"):
        config._inline_math_protect_audit = GuardAudit(config.input_file)
    return config._inline_math_protect_audit


def filter_offpage_text(docs, config):
    """Drop only glyphs wholly outside the visible crop; preserve edge glyphs.

    Cropped side-by-side PDFs can retain the other page in a clipped Form
    XObject. The native parser currently emits those off-page glyphs too.
    They must not become fallback paragraphs and reappear in the translation.
    Original PDF drawing instructions remain untouched.
    """
    audit = get_audit(config)
    for page in docs.page:
        crop = page.cropbox or page.mediabox
        if not crop or not crop.box:
            continue
        box = crop.box
        kept, removed = [], []
        for char in page.pdf_character:
            visual = char.visual_bbox.box if char.visual_bbox else char.box
            boxes = tuple(b for b in (char.box, visual) if b is not None)
            visible = any(b.x2 >= box.x - 0.5 and b.x <= box.x2 + 0.5
                          and b.y2 >= box.y - 0.5 and b.y <= box.y2 + 0.5 for b in boxes)
            visible = visible or not boxes
            (kept if visible else removed).append(char)
        page.pdf_character = kept
        if removed:
            audit.record("offpage_filtered", page=page.page_number + 1, count=len(removed))
    return docs


def complete_math_runs(tagged, font_map):
    """Keep source glyphs for encoded symbols and unambiguous inline variables.

    Publisher AdvTT/AdvP subsets can draw theta with a character labelled 'q'.
    A zero-ascent, tiny glyph inventory identifies their opaque symbol subsets;
    we preserve the PDF glyph, rather than guessing a Unicode replacement.
    Ordinary multi-letter italic prose is left translatable.
    """
    if not tagged or not font_map:
        return tagged
    chars = [x[0] for x in tagged]
    flags = [bool(x[1]) for x in tagged]
    texts = [c.char_unicode or ' ' for c in chars]

    for i, char in enumerate(chars):
        font = font_map.get(char.pdf_style.font_id)
        if font is None:
            continue
        name = re.sub(r'^[A-Z]{6}\+', '', font.name or '')
        inventory = getattr(font, 'pdf_font_char_bounding_box', ()) or ()
        opaque = (name.startswith(('AdvTT', 'AdvP'))
                  and font.ascent == 0 and 0 < len(inventory) <= 32
                  and bool(re.fullmatch(r'[A-Za-z]', texts[i])))
        if opaque:
            flags[i] = True
            continue
        italic = bool(font.italic) or bool(re.search(r'(?:\.I|Italic|Oblique)$', name, re.I))
        if not italic or not re.fullmatch(r'[A-Za-z]', texts[i]):
            continue
        # A subscript in a smaller font is part of a variable, not prose.
        adjacent = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(chars):
                other = chars[j]
                if (re.fullmatch(r'[A-Za-z]', texts[j])
                    and other.pdf_style.font_id == char.pdf_style.font_id
                    and abs(other.pdf_style.font_size - char.pdf_style.font_size) < 0.5):
                    adjacent.append(j)
        if not adjacent:
            flags[i] = True

    # Preserve standard math operators/units when directly next to a math span.
    joined = ''.join(t if len(t) == 1 else '\0' for t in texts)
    functions = {'ln', 'log', 'exp', 'trace', 'sin', 'cos', 'tan'}
    units = {'s', 'ms', 'Hz', 'nA', 'nC', 'TE', 'mm', 'T', 'n', 'C'}
    for match in re.finditer(r'[A-Za-z]+', joined):
        start, end = match.span(); token = match.group()
        left = start - 1
        while left >= 0 and texts[left].isspace(): left -= 1
        right = end
        while right < len(chars) and texts[right].isspace(): right += 1
        nearby = (left >= 0 and flags[left]) or (right < len(chars) and flags[right])
        if token in functions and right < len(chars) and texts[right] == '(':
            nearby = nearby or (right + 1 < len(chars) and flags[right + 1])
        if nearby and (token in functions or token in units):
            flags[start:end] = [True] * (end - start)

    # A comma between protected variables/numbers belongs to their math list.
    for i, text in enumerate(texts):
        if text != ',': continue
        left, right = i - 1, i + 1
        while left >= 0 and texts[left].isspace(): left -= 1
        while right < len(chars) and texts[right].isspace(): right += 1
        if left >= 0 and right < len(chars) and flags[left] and flags[right]:
            flags[i] = True

    # Include balanced parentheses for variable expressions or N(0, 0.25).
    stack = []
    for i, text in enumerate(texts):
        if text == '(':
            stack.append(i)
        elif text == ')' and stack:
            start = stack.pop()
            inside = range(start + 1, i)
            has_variable = any(re.fullmatch(r'[A-Za-z]|[\u0370-\u03ff]', texts[j]) and flags[j] for j in inside)
            prefixed = start > 0 and flags[start-1] and bool(re.fullmatch(r'[A-Za-z]', texts[start-1]))
            if ((has_variable or prefixed)
                and all(flags[j] or texts[j].isspace() or texts[j] == ',' for j in inside)):
                flags[start] = flags[i] = True

    # Whitespace between protected glyphs belongs to the same PDF span.
    for match in re.finditer(r'\s+', joined):
        start, end = match.span()
        if start > 0 and end < len(chars) and flags[start-1] and flags[end]:
            flags[start:end] = [True] * (end-start)
    return [(char, flag, corner) for (char, _, corner), flag in zip(tagged, flags)]
