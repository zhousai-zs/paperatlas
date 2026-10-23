import hashlib
import json
from pathlib import Path

import pytest

from paperatlas.core import Atlas


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / 'papers' / 'paper'
    (root / 'pages').mkdir(parents=True)
    (root / 'source.pdf').write_bytes(b'%PDF-test source')
    (root / 'pages/page-0001.png').write_bytes(b'image')
    (root / 'pages/page-0002.png').write_bytes(b'image2')
    document = dict(schema='paperbridge.document', schema_version=1,
                    source_sha256=hashlib.sha256((root / 'source.pdf').read_bytes()).hexdigest(),
                    pages=[dict(page_idx=0, blocks=[dict(type='doc_title', content=[dict(content='Example restoration')])]),
                           dict(page_idx=1, blocks=[])])
    (root / 'document.json').write_text(json.dumps(document), 'utf-8')
    (root / 'report.json').write_text(json.dumps(dict(status='complete', accuracy_verified=False)), 'utf-8')
    (root / 'document.md').write_text('Header\n<!-- source_page: 1 -->\n# Method\n\n'
        'PromptIR restores noisy images using degradation prompts.\n\n'
        '<!-- source_page: 2 -->\n# Experiments\n\nLOL-v2 benchmark achieves PSNR 31.2.\n', 'utf-8')
    (root / 'evidence.json').write_text('[]', 'utf-8')
    return root


@pytest.fixture
def atlas(tmp_path):
    instance = Atlas(tmp_path / 'data', create=True)
    yield instance
    instance.close()
