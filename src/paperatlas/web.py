"""Local index browser. Separate process from MCP; does not load the embedding model."""
import json
import re
import sqlite3
import threading
import traceback
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import Atlas
from .errors import AtlasError

DOC_ID = re.compile(r'sha256:[a-f0-9]{64}')
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
PAGE = Path(__file__).resolve().parent / 'static' / 'index.html'


@contextmanager
def open_atlas(data_dir, write=False):
    atlas = Atlas(data_dir, read_only=not write)
    try:
        yield atlas
    finally:
        atlas.close()


def index_label(key):
    if ':e5' in key:
        return '含向量'
    if ':lexical' in key:
        return '仅关键词'
    return '其他'


def summary(atlas):
    conn = atlas.db.conn
    statuses = {}
    for row in conn.execute('SELECT metadata FROM documents'):
        status = json.loads(row['metadata']).get('status') or 'unknown'
        statuses[status] = statuses.get(status, 0) + 1
    keys = [row['index_key'] for row in conn.execute('SELECT DISTINCT index_key FROM documents ORDER BY index_key')]
    models = [row['model_key'] for row in conn.execute(
        'SELECT DISTINCT model_key FROM chunks WHERE model_key IS NOT NULL ORDER BY model_key')]
    return dict(
        database=str(atlas.db.directory / 'atlas.db'),
        documents=conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0],
        pages=conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0],
        chunks=conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],
        vectors=conn.execute('SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL').fetchone()[0],
        statuses=statuses,
        modes=list(dict.fromkeys(index_label(key) for key in keys)),
        models=models)


def paper_list(atlas):
    rows = atlas.db.conn.execute('''SELECT d.doc_id, d.title, d.root, d.index_key,
        (SELECT COUNT(*) FROM pages p WHERE p.doc_id=d.doc_id) AS pages,
        (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.doc_id) AS chunks,
        (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.doc_id AND c.vector IS NOT NULL) AS vectors,
        d.metadata FROM documents d ORDER BY d.title, d.doc_id''').fetchall()
    papers = []
    for row in rows:
        metadata = json.loads(row['metadata'])
        papers.append(dict(doc_id=row['doc_id'], title=row['title'], root=row['root'],
                           index_key=row['index_key'], index_label=index_label(row['index_key']),
                           status=metadata.get('status'), pages=row['pages'], chunks=row['chunks'],
                           vectors=row['vectors']))
    return dict(papers=papers)


def integrity_brief(value):
    if not isinstance(value, dict):
        return None
    codes = [item.get('code') for item in value.get('issues') or []
             if isinstance(item, dict) and item.get('code')]
    return dict(passed=value.get('passed'), assets_passed=value.get('assets_passed'), issues=codes)


def paper_detail(atlas, doc_id):
    _require_doc_id(doc_id)
    doc = atlas.db.document(doc_id)
    metadata = json.loads(doc['metadata'])
    pages = [dict(page=row['page'], chars=row['chars'], chunks=row['chunks'], vectors=row['vectors'])
             for row in atlas.db.conn.execute('''SELECT p.page, length(p.text) AS chars,
        (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=p.doc_id AND c.page=p.page) AS chunks,
        (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=p.doc_id AND c.page=p.page AND c.vector IS NOT NULL) AS vectors
        FROM pages p WHERE p.doc_id=? ORDER BY p.page''', (doc_id,))]
    models = [row['model_key'] for row in atlas.db.conn.execute(
        'SELECT DISTINCT model_key FROM chunks WHERE doc_id=? AND model_key IS NOT NULL ORDER BY model_key', (doc_id,))]
    return dict(doc_id=doc['doc_id'], title=doc['title'], root=doc['root'], pdf=metadata.get('pdf'),
                status=metadata.get('status'), warnings=metadata.get('warnings') or [],
                index_key=doc['index_key'], index_label=index_label(doc['index_key']),
                integrity=integrity_brief(metadata.get('integrity')), models=models, pages=pages)


def page_detail(atlas, doc_id, page):
    _require_doc_id(doc_id)
    if type(page) is not int or page < 1:
        raise AtlasError('page 必须是从 1 开始的页码')
    atlas.db.document(doc_id)
    row = atlas.db.conn.execute('SELECT text FROM pages WHERE doc_id=? AND page=?', (doc_id, page)).fetchone()
    if row is None:
        raise AtlasError('页码不存在于已入库文档')
    chunks = []
    for chunk in atlas.db.conn.execute('''SELECT id, start, end, section, excerpt, warnings,
        model_key, vector IS NOT NULL AS has_vector FROM chunks
        WHERE doc_id=? AND page=? ORDER BY start, id''', (doc_id, page)):
        chunks.append(dict(id=chunk['id'], start=chunk['start'], end=chunk['end'], section=chunk['section'],
                           excerpt=chunk['excerpt'], warnings=json.loads(chunk['warnings']),
                           has_vector=bool(chunk['has_vector']), model_key=chunk['model_key']))
    return dict(doc_id=doc_id, page=page, chars=len(row['text']), text=row['text'], chunks=chunks)


def source_report(atlas):
    papers = []
    for row in atlas.db.conn.execute('SELECT * FROM documents ORDER BY title, doc_id'):
        doc = dict(row)
        problem = atlas._source_problem(doc)
        papers.append(dict(doc_id=doc['doc_id'], title=doc['title'], problem=problem,
                           message=Atlas.STALE[problem] if problem else ''))
    return dict(checked=len(papers),
                missing=sum(item['problem'] == 'missing' for item in papers),
                changed=sum(item['problem'] == 'changed' for item in papers),
                papers=papers)


def search_index(atlas, query, limit=10):
    """Keyword lookup over stored chunks. Does not read source files or load vectors."""
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        raise AtlasError('查询不能为空且不能超过 4000 字符')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise AtlasError('limit 必须为 1–20')
    from .query import expand
    from .retrieval import lexical_text
    terms = list(dict.fromkeys(lexical_text(expand(query)).split()))[:64]
    if not terms:
        return dict(query=query, hits=[], warnings=['查询没有可匹配的词'])
    expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
    try:
        rows = atlas.db.conn.execute('''SELECT c.doc_id, c.page, c.section, c.excerpt, c.start, d.title
            FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
            JOIN documents d ON d.doc_id=c.doc_id
            WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts), c.id LIMIT ?''', (expression, limit)).fetchall()
    except sqlite3.OperationalError as exc:
        raise AtlasError('查询无法执行') from exc
    hits = []
    for row in rows:
        text = row['excerpt']
        hits.append(dict(doc_id=row['doc_id'], title=row['title'], page=row['page'], section=row['section'],
                         start=row['start'], excerpt=text if len(text) <= 800 else text[:800] + '…'))
    return dict(query=query, hits=hits, warnings=[])


def _require_doc_id(doc_id):
    if not isinstance(doc_id, str) or not DOC_ID.fullmatch(doc_id):
        raise AtlasError('doc_id 格式无效')


def make_server(data_dir, host, port):
    if host not in LOOPBACK:
        raise AtlasError('管理界面只监听本机地址。')
    if not PAGE.is_file():
        raise AtlasError('缺少管理页面文件。')
    with open_atlas(data_dir):
        pass
    page = PAGE.read_bytes()
    directory = str(Path(data_dir).resolve())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            self._dispatch(self._get)

        def do_POST(self):
            if not self._same_origin():
                self._send_json(dict(error='拒绝跨站请求'), 403)
                return
            self._dispatch(self._post)

        def _dispatch(self, route):
            try:
                route()
            except AtlasError as exc:
                self._send_json(dict(error=str(exc)), 404 if '不存在' in str(exc) else 400)
            except Exception:
                traceback.print_exc()
                self._send_json(dict(error='请求失败'), 500)

        def _same_origin(self):
            origin = self.headers.get('Origin')
            return not origin or urlparse(origin).netloc == self.headers.get('Host', '')

        def _get(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path
            if path in {'/', '/index.html'}:
                self._send_bytes(page, 'text/html; charset=utf-8')
                return
            with open_atlas(directory) as atlas:
                if path == '/api/summary':
                    payload = summary(atlas)
                elif path == '/api/papers':
                    payload = paper_list(atlas)
                elif path == '/api/paper':
                    payload = paper_detail(atlas, _one(query, 'doc_id'))
                elif path == '/api/page':
                    payload = page_detail(atlas, _one(query, 'doc_id'), _page(query))
                elif path == '/api/sources':
                    payload = source_report(atlas)
                elif path == '/api/search':
                    payload = search_index(atlas, _one(query, 'q'), _limit(query))
                else:
                    self._send_json(dict(error='没有这个地址'), 404)
                    return
            self._send_json(payload)

        def _post(self):
            parsed = urlparse(self.path)
            payload = self._body()
            with open_atlas(directory, write=True) as atlas:
                if parsed.path == '/api/remove':
                    doc_ids = payload.get('doc_ids')
                    if (not isinstance(doc_ids, list) or not doc_ids
                            or any(not isinstance(item, str) or not DOC_ID.fullmatch(item) for item in doc_ids)):
                        raise AtlasError('doc_ids 必须是非空的论文编号列表')
                    result = atlas.remove(doc_ids)
                elif parsed.path == '/api/prune':
                    dry_run = payload.get('dry_run', True)
                    if type(dry_run) is not bool:
                        raise AtlasError('dry_run 必须是布尔值')
                    result = atlas.prune(dry_run)
                else:
                    self._send_json(dict(error='没有这个地址'), 404)
                    return
            self._send_json(result)

        def _body(self):
            length = int(self.headers.get('Content-Length', '0') or 0)
            if length < 0 or length > 65536:
                raise AtlasError('请求过大')
            raw = self.rfile.read(length) if length else b'{}'
            try:
                data = json.loads(raw.decode('utf-8'))
            except (UnicodeError, json.JSONDecodeError):
                raise AtlasError('请求体不是 JSON')
            if not isinstance(data, dict):
                raise AtlasError('请求体应为对象')
            return data

        def _send_json(self, data, status=200):
            self._send_bytes(json.dumps(data, ensure_ascii=False).encode('utf-8'),
                             'application/json; charset=utf-8', status)

        def _send_bytes(self, body, content_type, status=200):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise AtlasError(f'无法监听 {host}:{port}（{exc}）') from exc
    server.daemon_threads = True
    return server


def _one(query, name):
    values = query.get(name)
    if not values or values[0] == '':
        raise AtlasError(f'缺少 {name}')
    return values[0]


def _page(query):
    try:
        return int(_one(query, 'page'))
    except ValueError:
        raise AtlasError('page 必须是从 1 开始的页码') from None


def _limit(query):
    raw = query.get('limit', ['10'])[0]
    try:
        return int(raw)
    except ValueError:
        raise AtlasError('limit 必须为 1–20') from None


def serve(data_dir, host='127.0.0.1', port=8765, open_browser=True):
    server = make_server(data_dir, host, port)
    bound = server.server_address
    url = f'http://{bound[0]}:{bound[1]}/'
    print(f'索引管理  {url}', flush=True)
    print('只监听本机。结束此进程即关闭页面。MCP 不受影响。', flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('已停止')
    finally:
        server.server_close()
