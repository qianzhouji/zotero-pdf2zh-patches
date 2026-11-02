"""Run one four-page validation using the user's existing translation provider."""
import json
import logging
import os
from pathlib import Path

from pdf2zh_next.config.main import ConfigManager
from pdf2zh_next.high_level import create_babeldoc_config
from babeldoc.format.pdf.high_level import translate
from pdf2zh_next.translator.cache import init_db


def main():
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    settings = ConfigManager().initialize_config()
    settings.validate_settings()
    init_db()
    source = next(iter(settings.basic.input_files))
    config = create_babeldoc_config(settings, Path(source))
    config.use_rich_pbar = False
    tag = os.environ.get('PDF2ZH_VALIDATION_TAG', 'validation')
    config.working_dir = Path('work/inline-math-review/' + tag + '-ir').resolve()
    config.working_dir.mkdir(parents=True, exist_ok=True)
    config._is_temp_dir = False
    result = translate(config)
    print('RESULT', result, flush=True)
    counts = dict(config._inline_math_protect_audit.counts)
    Path('work/inline-math-review/' + tag + '-counts.json').write_text(json.dumps(counts, indent=2))


if __name__ == '__main__':
    main()
