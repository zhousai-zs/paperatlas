import json
import os
from pathlib import Path

from .errors import AtlasError

MODEL_ID = 'intfloat/multilingual-e5-small'
MAX_TOKENS = 512


def fit_query(tokenizer, text, prefix='query: '):
    """Cut text at a token boundary so prefix, text and special tokens fit the model."""
    # Margin for tokenizer boundary effects when prefix and text are joined.
    budget = MAX_TOKENS - 2 - len(tokenizer(prefix, add_special_tokens=False)['input_ids']) - 4
    offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True,
                        verbose=False)['offset_mapping']
    if len(offsets) <= budget:
        return text
    return text[:offsets[budget - 1][1]]


def prepare(data_dir, revision='main'):
    """Only this explicit command is allowed to download model files."""
    from huggingface_hub import HfApi, snapshot_download
    destination = Path(data_dir) / 'models'
    destination.mkdir(parents=True, exist_ok=True)
    sha = HfApi().model_info(MODEL_ID, revision=revision).sha
    path = snapshot_download(MODEL_ID, revision=sha, cache_dir=destination / 'cache',
                             ignore_patterns=['onnx/*', 'openvino/*', '*.bin', '*.h5', '*.ot'])
    info = dict(model_id=MODEL_ID, revision=sha, path=str(Path(path).resolve()))
    # Preserve the last prepared model if a download fails.
    temp = destination / 'model.json.tmp'
    temp.write_text(json.dumps(info, indent=2), 'utf-8')
    temp.replace(destination / 'model.json')
    return info


class Embedder:
    def __init__(self, data_dir):
        path = Path(data_dir) / 'models' / 'model.json'
        if not path.is_file():
            raise AtlasError('本地模型未准备；运行 paperatlas models prepare，或显式使用关键词模式。')
        self.info = json.loads(path.read_text('utf-8'))
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        os.environ['TOKENIZERS_PARALLELISM'] = 'false'
        from sentence_transformers import SentenceTransformer
        import torch
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        try:
            self.model = SentenceTransformer(self.info['path'], device='cpu', local_files_only=True)
        except (OSError, ValueError) as exc:
            raise AtlasError(f'本地模型不可用；重新运行 models prepare: {exc}') from exc
        self.tokenizer = self.model.tokenizer
        self.key = self.info['model_id'] + '@' + self.info['revision']

    def encode(self, texts, query=False):
        prefix = 'query: ' if query else 'passage: '
        inputs = [prefix + t for t in texts]
        if any(len(self.tokenizer.encode(t)) > MAX_TOKENS for t in inputs):
            raise AtlasError('模型输入超过 512 tokens；请缩短查询或重新切块。')
        return self.model.encode(inputs, normalize_embeddings=True, batch_size=16,
                                 show_progress_bar=False, convert_to_numpy=True).astype('float32')
