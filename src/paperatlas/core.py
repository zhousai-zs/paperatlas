import json
from pathlib import Path
import time
import sqlite3

from .chunking import Chunker
from .embedding import Embedder
from .errors import AtlasError
from .source import discover, fingerprint, read_bundle, digest, safe_path, load_json
from .storage import Database


class Atlas:
    def __init__(self, data_dir='.paperatlas', create=False, read_only=False):
        self.db = Database(data_dir, create=create, read_only=read_only)
        self._embedder = None

    def close(self):
        self.db.close()

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = Embedder(self.db.directory)
        return self._embedder

    def index(self, path, mode='hybrid'):
        if mode not in {'hybrid', 'lexical'}:
            raise AtlasError('导入模式必须为 hybrid 或 lexical')
        embedder = self.embedder if mode == 'hybrid' else None
        chunker = Chunker(embedder.tokenizer if embedder else None)
        key = chunker.key + ':' + (embedder.key if embedder else 'none')
        results = []
        started = time.perf_counter()
        for root in discover(path):
            try:
                before = fingerprint(root)
                bundle = read_bundle(root)
                if bundle['fingerprint'] != before:
                    raise AtlasError('导入期间源文件变化；请重试')
                old = self.db.conn.execute('SELECT * FROM documents WHERE doc_id=?', (bundle['doc_id'],)).fetchone()
                if old and old['fingerprint'] == bundle['fingerprint'] and old['index_key'] == key:
                    status = 'skipped' if old['root'] == bundle['root'] else 'relocated'
                    if status == 'relocated':
                        metadata = {k: v for k, v in bundle.items() if k != 'pages'}
                        with self.db.conn:
                            self.db.conn.execute('UPDATE documents SET root=?,metadata=? WHERE doc_id=?',
                                                 (bundle['root'], json.dumps(metadata, ensure_ascii=False), bundle['doc_id']))
                else:
                    chunks, section = [], ''
                    for page in bundle['pages']:
                        parts, section = chunker.chunks(page['text'], bundle['title'], section,
                            bundle['reference_ranges'].get(str(page['page']), []))
                        chunks.extend(dict(part, page=page['page']) for part in parts)
                    if not chunks:
                        raise AtlasError('文档无可检索文本')
                    vectors = embedder.encode([c['retrieval'] for c in chunks]) if embedder else None
                    if fingerprint(root) != before:
                        raise AtlasError('构建索引期间源文件变化；旧索引保留，请重试')
                    self.db.put(bundle, chunks, key, vectors, embedder.key if embedder else None)
                    status = 'updated' if old else 'indexed'
                results.append(dict(path=str(root), doc_id=bundle['doc_id'], result=status,
                                    pages=len(bundle['pages']), status=bundle['status'], warnings=bundle['warnings']))
            except (AtlasError, OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
                results.append(dict(path=str(root), result='failed', error=str(exc)))
        return dict(results=results, elapsed_seconds=round(time.perf_counter() - started, 3))

    def list_papers(self, limit=50, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise AtlasError('limit 必须为 1–100，offset 必须非负')
        rows = self.db.conn.execute('''SELECT d.*,(SELECT COUNT(*) FROM pages p WHERE p.doc_id=d.doc_id) AS pages,
            (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.doc_id) AS chunks
            FROM documents d ORDER BY title,doc_id LIMIT ? OFFSET ?''', (limit, offset)).fetchall()
        return dict(total=self.db.conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0],
                    papers=[dict(doc_id=r['doc_id'], title=r['title'], pages=r['pages'], chunks=r['chunks'],
                                 status=json.loads(r['metadata'])['status'], warnings=json.loads(r['metadata'])['warnings']) for r in rows])

    def _fresh(self, doc_id):
        doc = self.db.document(doc_id)
        root = Path(doc['root'])
        try:
            if fingerprint(root) != doc['fingerprint'] or digest(safe_path(root, 'source.pdf')) != doc_id.removeprefix('sha256:'):
                raise AtlasError('源文件已变化，索引过期；请重新 index 后读取。')
        except OSError as exc:
            raise AtlasError('源文件不可用；请恢复来源目录或重新 index。') from exc
        return doc

    def search_papers(self, query, limit=5, doc_id=None, mode='hybrid', diverse=False):
        if not isinstance(query, str) or len(query) > 4000 or not query.strip():
            raise AtlasError('查询不能为空且不能超过 4000 字符')
        if not 1 <= limit <= 50 or mode not in {'lexical', 'semantic', 'hybrid'}:
            raise AtlasError('limit 必须为 1–50，模式为 lexical/semantic/hybrid')
        if doc_id:
            self.db.document(doc_id)
        from .retrieval import ranks, result
        ranked = ranks(self.db, query, mode, self.embedder if mode != 'lexical' else None, doc_id)
        checked, results, seen_pages, paper_counts = set(), [], set(), {}
        for identity, score in ranked:
            row = self.db.conn.execute('SELECT doc_id,page FROM chunks WHERE id=?', (identity,)).fetchone()
            page_key = (row['doc_id'], row['page'])
            if page_key in seen_pages:
                continue
            # Topic searches need evidence across papers; targeted searches may
            # return as many pages from the selected paper as the caller requests.
            if diverse and not doc_id and paper_counts.get(row['doc_id'], 0) >= 2:
                continue
            if row['doc_id'] not in checked:
                self._fresh(row['doc_id'])
                checked.add(row['doc_id'])
            results.append(result(self.db, identity, score))
            seen_pages.add(page_key)
            paper_counts[row['doc_id']] = paper_counts.get(row['doc_id'], 0) + 1
            if len(results) == limit:
                break
        return dict(query=query, mode=mode, results=results,
                    notice='结果为相关候选；排序分数不是事实置信度。excerpt 是连续扩展原文，match_range 是定位块。'
                           '回答前需读取原页；解释跨页时用 read_page(before=1, after=1)，信息不足时限定 doc_id 或换词继续检索。'
                           '主题比较可设 diverse=true 分散来源；单篇深入请指定 doc_id。'
                           '库外问题也可能返回候选，需判断证据是否充分。')

    def read_page(self, doc_id, page, before=0, after=0):
        if type(page) is not int or page < 1:
            raise AtlasError('page 必须是从 1 开始的 PDF 页码')
        if any(type(n) is not int or not 0 <= n <= 2 for n in (before, after)):
            raise AtlasError('before/after 必须为 0–2，最多读取前后各两页')
        doc = self._fresh(doc_id)
        row = self.db.conn.execute('SELECT text FROM pages WHERE doc_id=? AND page=?', (doc_id, page)).fetchone()
        if row is None:
            raise AtlasError('页码不存在于已入库文档')
        root = Path(doc['root'])
        metadata = json.loads(doc['metadata'])
        evidence = load_json(root, 'evidence.json', optional=True) or []
        def source(number):
            return dict(pdf=str(safe_path(root, 'source.pdf')),
                        page_image=str(safe_path(root, f'pages/page-{number:04d}.png')),
                        evidence=[dict(e, image=str(safe_path(root, e['image']))) for e in evidence if e['page'] == number])
        output = dict(doc_id=doc_id, title=doc['title'], page=page, markdown=row['text'],
                    status=metadata['status'], warnings=metadata['warnings'], integrity=metadata['integrity'],
                    source=source(page))
        if before or after:
            neighbors = self.db.conn.execute('SELECT page,text FROM pages WHERE doc_id=? '
                'AND page BETWEEN ? AND ? AND page!=? ORDER BY ABS(page-?),page',
                (doc_id, max(1, page-before), page+after, page, page)).fetchall()
            remaining = max(0, 24000 - len(row['text']))
            output['neighboring_pages'], output['omitted_pages'] = [], []
            for neighbor in neighbors:
                if len(neighbor['text']) > remaining:
                    output['omitted_pages'].append(neighbor['page'])
                    continue
                remaining -= len(neighbor['text'])
                output['neighboring_pages'].append(dict(page=neighbor['page'], markdown=neighbor['text'],
                                                        source=source(neighbor['page'])))
            output['neighboring_pages'].sort(key=lambda p: p['page'])
            if output['omitted_pages']:
                output['warnings'] = output['warnings'] + ['相邻页超过 24000 字符总预算；请按 omitted_pages 单独读取，未截断原页。']
        return output
