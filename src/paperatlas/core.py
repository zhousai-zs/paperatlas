import json
from pathlib import Path
import time
import sqlite3

from .chunking import Chunker
from .embedding import Embedder
from .errors import AtlasError
from .source import REQUIRED, discover, fingerprint, read_bundle, digest, safe_path, load_json
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

    STALE = dict(missing='源文件不可用；请恢复来源目录后重新 index，或用 remove/prune 移出索引。',
                 changed='源文件已变化，索引过期；请重新 index 后读取。')

    def _source_problem(self, doc):
        """Return None for a usable source, 'missing' when it cannot be read, or 'changed'."""
        root = Path(doc['root'])
        try:
            if not all((root / name).is_file() for name in REQUIRED):
                return 'missing'
            if (fingerprint(root) != doc['fingerprint']
                    or digest(safe_path(root, 'source.pdf')) != doc['doc_id'].removeprefix('sha256:')):
                return 'changed'
        except (OSError, AtlasError):
            return 'missing'
        return None

    def _fresh(self, doc_id):
        doc = self.db.document(doc_id)
        problem = self._source_problem(doc)
        if problem:
            raise AtlasError(self.STALE[problem])
        return doc

    def remove(self, doc_ids):
        missing = [d for d in doc_ids if self.db.conn.execute('SELECT 1 FROM documents WHERE doc_id=?', (d,)).fetchone() is None]
        if missing:
            raise AtlasError('论文 ID 不存在；使用 list 查看完整 doc_id: ' + ', '.join(missing))
        removed = []
        with self.db.conn:
            for doc_id in dict.fromkeys(doc_ids):
                doc = self.db.document(doc_id)
                # Pages, chunks and FTS rows follow through cascades and the delete trigger.
                self.db.conn.execute('DELETE FROM documents WHERE doc_id=?', (doc_id,))
                removed.append(dict(doc_id=doc_id, title=doc['title'], path=doc['root']))
        return dict(removed=removed)

    def prune(self, dry_run=False):
        """Drop documents whose source bundle is gone; changed sources need index, not pruning."""
        candidates = [dict(doc_id=r['doc_id'], title=r['title'], path=r['root'])
                      for r in self.db.conn.execute('SELECT * FROM documents ORDER BY title,doc_id')
                      if self._source_problem(dict(r)) == 'missing']
        if dry_run:
            return dict(dry_run=True, candidates=candidates)
        removed = self.remove([c['doc_id'] for c in candidates])['removed'] if candidates else []
        return dict(dry_run=False, removed=removed)

    def search_papers(self, query, limit=5, doc_id=None, mode='hybrid', diverse=False):
        if not isinstance(query, str) or len(query) > 4000 or not query.strip():
            raise AtlasError('查询不能为空且不能超过 4000 字符')
        if not 1 <= limit <= 50 or mode not in {'lexical', 'semantic', 'hybrid'}:
            raise AtlasError('limit 必须为 1–50，模式为 lexical/semantic/hybrid')
        if doc_id:
            self._fresh(doc_id)
        from .retrieval import ranks, result
        warnings = []
        ranked = ranks(self.db, query, mode, self.embedder if mode != 'lexical' else None, doc_id, warnings)
        checked, stale, results, seen_pages, paper_counts = {doc_id} if doc_id else set(), {}, [], set(), {}
        for identity, score in ranked:
            row = self.db.conn.execute('SELECT doc_id,page FROM chunks WHERE id=?', (identity,)).fetchone()
            page_key = (row['doc_id'], row['page'])
            if page_key in seen_pages or row['doc_id'] in stale:
                continue
            # Topic searches need evidence across papers; targeted searches may
            # return as many pages from the selected paper as the caller requests.
            if diverse and not doc_id and paper_counts.get(row['doc_id'], 0) >= 2:
                continue
            if row['doc_id'] not in checked:
                doc = self.db.document(row['doc_id'])
                problem = self._source_problem(doc)
                if problem:
                    # Never mix stale text into results, but keep other papers' evidence usable.
                    stale[row['doc_id']] = dict(doc_id=row['doc_id'], title=doc['title'], path=doc['root'],
                                                reason=problem, message=self.STALE[problem])
                    continue
                checked.add(row['doc_id'])
            results.append(result(self.db, identity, score))
            seen_pages.add(page_key)
            paper_counts[row['doc_id']] = paper_counts.get(row['doc_id'], 0) + 1
            if len(results) == limit:
                break
        if stale:
            warnings.append(f'已跳过 {len(stale)} 篇来源失效的论文，见 stale_documents；其结果未返回。')
        return dict(query=query, mode=mode, results=results, warnings=warnings, stale_documents=list(stale.values()),
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
