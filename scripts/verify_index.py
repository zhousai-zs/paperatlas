"""Mechanically verify every chunk against its indexed page and current sources."""
import argparse
import json
from pathlib import Path

from paperatlas.core import Atlas
from paperatlas.chunking import Chunker
import numpy as np


def verify(directory):
    atlas = Atlas(directory)
    try:
        rows = atlas.db.conn.execute('SELECT c.*,p.text FROM chunks c JOIN pages p USING(doc_id,page)').fetchall()
        chunker = Chunker(atlas.embedder.tokenizer)
        for row in rows:
            assert row['text'][row['start']:row['end']] == row['excerpt'], row['id']
            assert chunker.count('passage: ' + row['retrieval']) + 2 <= 512, row['id']
            assert row['vector'] is not None and row['model_key'] == atlas.embedder.key, row['id']
            vector = np.frombuffer(row['vector'], dtype='float32')
            assert len(vector) == 384 and np.isfinite(vector).all(), row['id']
            assert abs(np.linalg.norm(vector) - 1.0) < 1e-4, row['id']
        for doc in atlas.list_papers(limit=100)['papers']:
            atlas._fresh(doc['doc_id'])
        assert atlas.db.conn.execute('SELECT COUNT(*) FROM chunks_fts').fetchone()[0] == len(rows)
        return dict(chunks=len(rows), literal_ranges_passed=True, tokenizer_limits_passed=True,
                    current_sources_passed=True, fts_count_passed=True,
                    normalized_vectors_passed=True,
                    pages=atlas.db.conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0],
                    papers=atlas.list_papers()['total'])
    finally:
        atlas.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='.paperatlas')
    parser.add_argument('--output', default='.cache/index-verification.json')
    args = parser.parse_args()
    output = verify(args.data_dir)
    Path(args.output).write_text(json.dumps(output, indent=2), 'utf-8')
    print(json.dumps(output, indent=2))
