import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest

from paperatlas.core import Atlas
from paperatlas.embedding import Embedder


def pytest_configure(config):
    # A clean checkout has no ignored .cache parent; pytest creates only basetemp.
    if config.option.basetemp:
        Path(config.option.basetemp).parent.mkdir(parents=True, exist_ok=True)


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


class FakeTokenizer:
    """Offline stand-in with the HuggingFace call shape used by Chunker and fit_query."""

    def __call__(self, text, add_special_tokens=True, return_offsets_mapping=False, verbose=True):
        if isinstance(text, (list, tuple)):
            tokenized = [self(t, add_special_tokens=add_special_tokens,
                              return_offsets_mapping=return_offsets_mapping,
                              verbose=verbose) for t in text]
            return dict(input_ids=[item['input_ids'] for item in tokenized],
                        offset_mapping=[item['offset_mapping'] for item in tokenized])
        offsets = [(m.start(), m.end()) for m in re.finditer(r'[一-鿿]|\w+|[^\w\s]', text)]
        extra = 2 if add_special_tokens else 0
        return dict(input_ids=[0] * (len(offsets) + extra), offset_mapping=offsets)

    def encode(self, text):
        return [0] * (len(self(text, add_special_tokens=False)['offset_mapping']) + 2)


class FakeModel:
    def __init__(self):
        self.inputs = []

    def encode(self, inputs, **_):
        self.inputs.extend(inputs)
        vectors = np.array([[len(t) % 7 + 1, 1, 0, 0] for t in inputs], dtype='float32')
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


@pytest.fixture
def fake_embedder():
    embedder = Embedder.__new__(Embedder)
    embedder.tokenizer, embedder.model, embedder.key = FakeTokenizer(), FakeModel(), 'fake@test'
    return embedder
