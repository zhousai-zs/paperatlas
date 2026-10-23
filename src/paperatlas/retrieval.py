import json
import logging
import re

import jieba
import numpy as np

from .errors import AtlasError

jieba.setLogLevel(logging.WARNING)
WORDS = re.compile(r'[a-z0-9_]+|[\u4e00-\u9fff]+', re.I)


def lexical_text(text):
    tokens = []
    for match in WORDS.finditer(text.lower()):
        value = match[0]
        tokens.extend(jieba.cut(value, HMM=False) if re.search(r'[\u4e00-\u9fff]', value) else [value])
    return ' '.join(tokens)


def ranks(database, query, mode, embedder=None, doc_id=None):
    connection = database.conn
    from .query import expand
    query = expand(query)
    body_filter = "lower(trim(c.section)) NOT IN ('references','bibliography','参考文献')"
    filters = ' AND ' + body_filter + (' AND c.doc_id=?' if doc_id else '')
    extra = [doc_id] if doc_id else []
    lexical, semantic = [], []
    if mode in {'lexical', 'hybrid'}:
        terms = list(dict.fromkeys(lexical_text(query).split()))[:64]
        expression = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
        if expression:
            lexical = [r['id'] for r in connection.execute(
                'SELECT c.id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid '
                'WHERE chunks_fts MATCH ?' + filters + ' ORDER BY bm25(chunks_fts), c.id LIMIT 50',
                [expression] + extra)]
    if mode in {'semantic', 'hybrid'}:
        if embedder is None:
            raise AtlasError('混合/语义检索需要已准备的本地模型。')
        selected = ' WHERE ' + body_filter.replace('c.section', 'section') + (' AND doc_id=?' if doc_id else '')
        params = [doc_id] if doc_id else []
        mismatch = connection.execute('SELECT COUNT(*) FROM chunks' + selected +
                                      (' AND ' if selected else ' WHERE ') +
                                      '(vector IS NULL OR model_key!=?)', params + [embedder.key]).fetchone()[0]
        if mismatch:
            raise AtlasError('索引缺少当前模型向量；先运行 index 更新相关论文，或显式使用关键词模式。')
        rows = connection.execute('SELECT id,vector FROM chunks' + selected + ' ORDER BY id', params).fetchall()
        if rows:
            vectors = np.stack([np.frombuffer(row['vector'], dtype='float32') for row in rows])
            scores = vectors @ embedder.encode([query], query=True)[0]
            order = np.argsort(-scores, kind='stable')[:50]
            semantic = [rows[int(i)]['id'] for i in order]
    scores = {}
    for ranking in [lexical, semantic]:
        for position, identity in enumerate(ranking, 1):
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (60 + position)
    return sorted(scores.items(), key=lambda p: (-p[1], p[0]))


def result(database, identity, score):
    row = database.conn.execute('SELECT * FROM chunks WHERE id=?', (identity,)).fetchone()
    doc = database.document(row['doc_id'])
    metadata = json.loads(doc['metadata'])
    from pathlib import Path
    from .source import safe_path, load_json
    root = Path(doc['root'])
    evidence = load_json(root, 'evidence.json', optional=True) or []
    sources = [dict(e, image=str(safe_path(root, e['image']))) for e in evidence if e['page'] == row['page']]
    return dict(doc_id=row['doc_id'], title=doc['title'], page=row['page'], section=row['section'],
                chunk_id=row['id'], score=score, excerpt=row['excerpt'],
                source_range=dict(start=row['start'], end=row['end']), status=metadata['status'],
                warnings=metadata['warnings'] + json.loads(row['warnings']),
                source=dict(pdf=str(safe_path(root, 'source.pdf')),
                            page_image=str(safe_path(root, f"pages/page-{row['page']:04d}.png")), evidence=sources))
