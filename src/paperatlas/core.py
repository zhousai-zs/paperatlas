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
                        parts, section = chunker.chunks(page['text'], bundle['title'], section)
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

    def search_papers(self, query, limit=5, doc_id=None, mode='hybrid'):
        if not isinstance(query, str) or len(query) > 4000 or not query.strip():
            raise AtlasError('查询不能为空且不能超过 4000 字符')
        if not 1 <= limit <= 50 or mode not in {'lexical', 'semantic', 'hybrid'}:
            raise AtlasError('limit 必须为 1–50，模式为 lexical/semantic/hybrid')
        if doc_id:
            self.db.document(doc_id)
        from .retrieval import ranks, result
        ranked = ranks(self.db, query, mode, self.embedder if mode != 'lexical' else None, doc_id)
        checked, results, seen_pages = set(), [], set()
        for identity, score in ranked:
            row = self.db.conn.execute('SELECT doc_id,page FROM chunks WHERE id=?', (identity,)).fetchone()
            page_key = (row['doc_id'], row['page'])
            if page_key in seen_pages:
                continue
            if row['doc_id'] not in checked:
                self._fresh(row['doc_id'])
                checked.add(row['doc_id'])
            results.append(result(self.db, identity, score))
            seen_pages.add(page_key)
            if len(results) == limit:
                break
        return dict(query=query, mode=mode, results=results,
                    notice='结果为相关候选；排序分数不是事实置信度。库外问题也可能返回候选，需读取原文判断。')

    def read_page(self, doc_id, page):
        if type(page) is not int or page < 1:
            raise AtlasError('page 必须是从 1 开始的 PDF 页码')
        doc = self._fresh(doc_id)
        row = self.db.conn.execute('SELECT text FROM pages WHERE doc_id=? AND page=?', (doc_id, page)).fetchone()
        if row is None:
            raise AtlasError('页码不存在于已入库文档')
        root = Path(doc['root'])
        metadata = json.loads(doc['metadata'])
        evidence = load_json(root, 'evidence.json', optional=True) or []
        return dict(doc_id=doc_id, title=doc['title'], page=page, markdown=row['text'],
                    status=metadata['status'], warnings=metadata['warnings'], integrity=metadata['integrity'],
                    source=dict(pdf=str(safe_path(root, 'source.pdf')),
                                page_image=str(safe_path(root, f'pages/page-{page:04d}.png')),
                                evidence=[dict(e, image=str(safe_path(root, e['image']))) for e in evidence if e['page'] == page]))
