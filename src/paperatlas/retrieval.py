import json
import logging
import re

import jieba
import numpy as np

from .embedding import MAX_TOKENS, fit_query
from .errors import AtlasError

jieba.setLogLevel(logging.WARNING)
WORDS = re.compile(r'[a-z0-9_]+|[\u4e00-\u9fff]+', re.I)


def lexical_text(text):
    tokens = []
    for match in WORDS.finditer(text.lower()):
        value = match[0]
        tokens.extend(jieba.cut(value, HMM=False) if re.search(r'[\u4e00-\u9fff]', value) else [value])
    return ' '.join(tokens)


def fuse_pages(rankings, pages):
    """Fuse the evidence unit we actually return: pages, not arbitrary split IDs."""
    page_scores, chunk_scores, members = {}, {}, {}
    for ranking in rankings:
        seen = set()
        for position, identity in enumerate(ranking, 1):
            page = pages[identity]
            chunk_scores[identity] = chunk_scores.get(identity, 0.0) + 1.0 / (60 + position)
            members.setdefault(page, set()).add(identity)
            if page not in seen:
                seen.add(page)
                page_scores[page] = page_scores.get(page, 0.0) + 1.0 / (60 + len(seen))
    output = []
    for page in sorted(page_scores, key=lambda p: (-page_scores[p], p)):
        anchor = min(members[page], key=lambda i: (-chunk_scores[i], i))
        output.append((anchor, page_scores[page]))
    return output


def ranks(database, query, mode, embedder=None, doc_id=None, warnings=None):
    connection = database.conn
    from .query import expand
    original, query = query, expand(query)
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
            # Lexical search keeps the whole query; only the model input is bounded.
            semantic_query = fit_query(embedder.tokenizer, query)
            if semantic_query != query and warnings is not None:
                used = (f'仅使用查询前 {len(semantic_query)} 字符，未含附加术语别名'
                        if len(semantic_query) <= len(original) else '使用完整查询，附加术语别名被截断')
                warnings.append(f'查询超过语义模型 {MAX_TOKENS} tokens 输入上限；语义检索{used}；关键词检索使用完整查询。')
            vectors = np.stack([np.frombuffer(row['vector'], dtype='float32') for row in rows])
            scores = vectors @ embedder.encode([semantic_query], query=True)[0]
            order = np.argsort(-scores, kind='stable')[:50]
            semantic = [rows[int(i)]['id'] for i in order]
    if mode == 'hybrid':
        identities = list(dict.fromkeys(lexical + semantic))
        if not identities:
            return []
        placeholders = ','.join('?' for _ in identities)
        pages = {r['id']: (r['doc_id'], r['page']) for r in connection.execute(
            f'SELECT id,doc_id,page FROM chunks WHERE id IN ({placeholders})', identities)}
        return fuse_pages([lexical, semantic], pages)
    scores = {}
    for ranking in [lexical, semantic]:
        for position, identity in enumerate(ranking, 1):
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (60 + position)
    return sorted(scores.items(), key=lambda p: (-p[1], p[0]))


def context_window(database, row, max_chars=6000):
    """Expand a retrieval anchor to nearby same-section source blocks on its page.

    Gaps containing excluded text (including bibliography) and headings are hard
    boundaries. Return literal source offsets; do not stitch unrelated passages.
    """
    text = database.conn.execute('SELECT text FROM pages WHERE doc_id=? AND page=?',
                                (row['doc_id'], row['page'])).fetchone()['text']
    siblings = database.conn.execute('SELECT id,start,end,section FROM chunks '
        'WHERE doc_id=? AND page=? ORDER BY start,end,id', (row['doc_id'], row['page'])).fetchall()
    position = next(i for i, sibling in enumerate(siblings) if sibling['id'] == row['id'])
    left = right = position
    start, end = row['start'], row['end']
    while True:
        choices = []
        if left > 0:
            previous = siblings[left - 1]
            if (previous['section'] == row['section']
                    and not text[previous['end']:start].strip()
                    and end - previous['start'] <= max_chars):
                choices.append((start - previous['start'], 'left', previous))
        if right + 1 < len(siblings):
            following = siblings[right + 1]
            if (following['section'] == row['section']
                    and not text[end:following['start']].strip()
                    and following['end'] - start <= max_chars):
                choices.append((following['end'] - end, 'right', following))
        if not choices:
            break
        _, direction, candidate = min(choices, key=lambda c: (c[0], c[1]))
        if direction == 'left':
            left -= 1
            start = min(start, candidate['start'])
        else:
            right += 1
            end = max(end, candidate['end'])
    return start, end, text[start:end]


def result(database, identity, score):
    row = database.conn.execute('SELECT * FROM chunks WHERE id=?', (identity,)).fetchone()
    doc = database.document(row['doc_id'])
    metadata = json.loads(doc['metadata'])
    from pathlib import Path
    from .source import safe_path, load_json
    root = Path(doc['root'])
    evidence = load_json(root, 'evidence.json', optional=True) or []
    sources = [dict(e, image=str(safe_path(root, e['image']))) for e in evidence if e['page'] == row['page']]
    start, end, excerpt = context_window(database, row)
    return dict(doc_id=row['doc_id'], title=doc['title'], page=row['page'], section=row['section'],
                chunk_id=row['id'], score=score, excerpt=excerpt,
                source_range=dict(start=start, end=end),
                match_range=dict(start=row['start'], end=row['end']), status=metadata['status'],
                warnings=metadata['warnings'] + json.loads(row['warnings']),
                source=dict(pdf=str(safe_path(root, 'source.pdf')),
                            page_image=str(safe_path(root, f"pages/page-{row['page']:04d}.png")), evidence=sources))
