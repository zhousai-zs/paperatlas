"""PaperBridge V1 file contract. No imports from PaperBridge."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import AtlasError

MARKER = re.compile(r'<!--\s*source_page:\s*(\d+)\s*-->')
INPUTS = ('document.json', 'document.md', 'report.json', 'evidence.json',
          'manifest.json', 'integrity.json', 'review/revisions.json', 'original-document.md')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(root, relative, required=True):
    value = urlsplit(str(relative))
    if value.scheme or value.netloc:
        raise AtlasError(f'证据路径必须是包内相对路径: {relative}')
    path = (root / unquote(value.path)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise AtlasError(f'路径越界: {relative}')
    if required and not path.is_file():
        raise AtlasError(f'缺失文件: {relative}')
    return path


def load_json(root, name, optional=False):
    path = safe_path(root, name, required=not optional)
    return json.loads(path.read_text('utf-8-sig')) if path.is_file() else None


def fingerprint(root):
    hashes = {name: digest(safe_path(root, name)) for name in INPUTS
              if (root / name).exists()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def discover(path):
    root = Path(path).resolve()
    if not root.is_dir():
        raise AtlasError(f'输入目录不存在: {root}')
    if (root / 'document.json').is_file():
        return [root]
    found = []
    # Avoid private state, parser intermediates, and directory junction traversal.
    import os
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in
                         {'chunks', 'pages', 'evidence', 'review'} and
                         not (Path(current) / d).is_symlink() and
                         not (Path(current) / d).is_junction())
        if 'document.json' in files:
            found.append(Path(current))
            dirs[:] = []
    return sorted(found)


def read_bundle(path):
    root = Path(path).resolve()
    document = load_json(root, 'document.json')
    if not isinstance(document, dict) or document.get('schema') != 'paperbridge.document' or document.get('schema_version') != 1:
        raise AtlasError('只支持 paperbridge.document schema_version=1')
    if (not isinstance(document.get('pages'), list) or
            any(not isinstance(p, dict) or not isinstance(p.get('blocks'), list) or
                any(not isinstance(b, dict) for b in p['blocks']) for p in document['pages'])):
        raise AtlasError('document.json 缺少有效的 pages/blocks')
    sha = document.get('source_sha256', '')
    if not re.fullmatch('[a-fA-F0-9]{64}', sha):
        raise AtlasError('source_sha256 缺失或无效')
    sha = sha.lower()
    pdf = safe_path(root, 'source.pdf')
    if digest(pdf) != sha:
        raise AtlasError('source.pdf SHA-256 与 document.json 不符')
    revisions = load_json(root, 'review/revisions.json', optional=True)
    if revisions and revisions.get('pages') and not (root / 'original-document.md').is_file():
        raise AtlasError('存在未合并修订；请先由上游导出有效正文')
    markdown = safe_path(root, 'document.md').read_text('utf-8-sig')
    markers = list(MARKER.finditer(markdown))
    indices = [p['page_idx'] for p in document['pages']]
    numbers = [int(m[1]) for m in markers]
    if (not indices or any(type(i) is not int or i < 0 for i in indices)
            or len(set(indices)) != len(indices) or numbers != [i + 1 for i in indices]
            or numbers != sorted(numbers)):
        raise AtlasError('Markdown 页码与结构化页码不一致或存在重复')
    report = load_json(root, 'report.json')
    if not isinstance(report, dict):
        raise AtlasError('report.json 应为对象')
    if report.get('status') not in {'complete', 'needs_review'}:
        raise AtlasError(f"转换尚未完成: {report.get('status')}")
    integrity = load_json(root, 'integrity.json', optional=True)
    if integrity is not None and not isinstance(integrity, dict):
        raise AtlasError('integrity.json 应为对象')
    if integrity and (integrity.get('passed') is False or integrity.get('assets_passed') is False):
        raise AtlasError('上游完整性报告未通过')
    warnings = ['OCR 内容未经逐项核实；关键数字、公式和表格请核对原页。']
    if report['status'] == 'needs_review':
        warnings.append('needs_review: ' + json.dumps(report.get('issues', []), ensure_ascii=False))
    evidence = load_json(root, 'evidence.json', optional=True) or []
    if not isinstance(evidence, list):
        raise AtlasError('evidence.json 应为数组')
    for item in evidence:
        if not isinstance(item, dict):
            raise AtlasError('evidence.json 包含无效条目')
        if item.get('page') not in numbers:
            raise AtlasError('证据页码不在文档中')
        safe_path(root, item['image'])
    # Validate local Markdown/HTML image references; remote URLs are not fetched.
    refs = re.findall(r'!\[[^\]]*\]\(\s*<?([^\s)>]+)|<img\b[^>]*\bsrc=["\']([^"\']+)', markdown, re.I)
    for ref in refs:
        value = next(v for v in ref if v)
        parsed = urlsplit(value)
        if not parsed.scheme and not parsed.netloc:
            safe_path(root, value)
    pages = []
    for i, marker in enumerate(markers):
        number = int(marker[1])
        image = safe_path(root, f'pages/page-{number:04d}.png')
        text = markdown[marker.end():markers[i + 1].start() if i + 1 < len(markers) else len(markdown)]
        pages.append(dict(page=number, text=text, image=str(image),
                          evidence=[e for e in evidence if e['page'] == number]))
    title = root.name
    for page in document['pages']:
        titles = [b for b in page['blocks'] if b.get('type') == 'doc_title']
        if titles:
            def flatten(value):
                if isinstance(value, str):
                    return value
                if isinstance(value, list):
                    return ''.join(flatten(v) for v in value)
                return flatten(value.get('content', '')) if isinstance(value, dict) else ''
            title = flatten(titles[0]).strip() or title
            break
    # Recheck input files after parsing, so concurrent edits cannot create a mixed snapshot.
    return dict(doc_id='sha256:' + sha, title=title, root=str(root), pdf=str(pdf),
                fingerprint=fingerprint(root), status=report['status'], warnings=warnings,
                integrity=integrity, pages=pages)
