#!/usr/bin/env python3
"""Install/remove this version-locked local BabelDOC repair; no API calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
from datetime import datetime
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
DEFAULT_ROOT = (Path.home() / 'zotero-pdf2zh/server/zotero-pdf2zh-next-venv'
                '/lib/python3.12/site-packages/babeldoc/format/pdf')


def digest(blob):
    return hashlib.sha256(blob).hexdigest() if blob is not None else None


def apply(action, root, manifest):
    changes = []
    for row in manifest['files']:
        target = root / row['path']
        current = target.read_bytes() if target.exists() else None
        wanted = row['installed'] if action == 'install' else row['baseline']
        content = (BUNDLE / wanted).read_bytes() if wanted else None
        if digest(content) != row['installed_hash' if action == 'install' else 'baseline_hash']:
            raise RuntimeError('Bundle checksum mismatch: ' + row['path'])
        if current == content:
            continue
        if digest(current) not in row['accepted_hashes']:
            raise RuntimeError('Source has changed or package version differs; nothing written: ' + str(target))
        if content is not None:
            compile(content, str(target), 'exec')
        changes.append((target, current, content))
    if not changes:
        print('Already installed.' if action == 'install' else 'Already removed.')
        return
    backup = root / ('.inline-math-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir()
    history = []
    for target, old, new in changes:
        relative = str(target.relative_to(root))
        if old is not None:
            dest = backup / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(old)
        history.append({'path': relative, 'existed': old is not None, 'sha256': digest(old)})
    (backup / 'manifest.json').write_text(json.dumps(history, indent=2))
    completed = []
    try:
        for target, old, new in changes:
            if new is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + '.inline-math.tmp')
                if temp.exists():
                    raise RuntimeError('Temporary file already exists: ' + str(temp))
                temp.write_bytes(new)
                temp.chmod(stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644)
                os.replace(temp, target)
            completed.append((target, old))
            # Avoid timestamp-based bytecode reuse after rapid install/remove.
            for cache in (target.parent / '__pycache__').glob(target.stem + '.*.pyc'):
                cache.unlink()
    except Exception:
        for target, old in reversed(completed):
            if old is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(old)
        raise
    print('Installed V2.' if action == 'install' else 'Removed all Inline Math changes; original Reference Skip/Citation Protect retained.')
    print('Backup:', backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'remove', 'status'])
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    manifest = json.loads((BUNDLE / 'manifest.json').read_text())
    if args.action == 'status':
        for row in manifest['files']:
            path = args.root / row['path']
            value = digest(path.read_bytes() if path.exists() else None)
            state = 'V2' if value == row['installed_hash'] else 'baseline' if value == row['baseline_hash'] else 'known prior' if value in row['accepted_hashes'] else 'unknown changes'
            print(state, row['path'])
        return
    if not args.root.is_dir():
        parser.error('Package root does not exist: ' + str(args.root))
    apply(args.action, args.root, manifest)


if __name__ == '__main__':
    main()
