"""Hash source files without writing anything to the library."""
import argparse
import hashlib
import json
from pathlib import Path


def snapshot(root):
    root = Path(root).resolve()
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root')
    parser.add_argument('output')
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(snapshot(args.root), ensure_ascii=False, indent=2), encoding='utf-8')
