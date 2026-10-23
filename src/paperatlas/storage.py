import json
import sqlite3
from pathlib import Path

from .errors import AtlasError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents (
 doc_id TEXT PRIMARY KEY, title TEXT NOT NULL, root TEXT NOT NULL,
 fingerprint TEXT NOT NULL, index_key TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pages (
 doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
 page INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(doc_id,page));
CREATE TABLE IF NOT EXISTS chunks (
 id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL, page INTEGER NOT NULL,
 start INTEGER NOT NULL, end INTEGER NOT NULL, section TEXT NOT NULL,
 excerpt TEXT NOT NULL, retrieval TEXT NOT NULL, warnings TEXT NOT NULL,
 vector BLOB, model_key TEXT,
 FOREIGN KEY(doc_id,page) REFERENCES pages(doc_id,page) ON DELETE CASCADE);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text);
CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN
 DELETE FROM chunks_fts WHERE rowid=old.id;
END;
'''


class Database:
    def __init__(self, data_dir, create=False, read_only=False):
        self.directory = Path(data_dir).resolve()
        path = self.directory / 'atlas.db'
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        elif not path.is_file():
            raise AtlasError('知识库未初始化；先运行 paperatlas init。')
        self.conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30) if read_only else sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        version = self.conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in {0, 1}:
            self.conn.close()
            raise AtlasError(f'不支持的索引版本 {version}；请使用独立数据目录重建。')
        if create:
            self.conn.executescript(SCHEMA)
            self.conn.execute('PRAGMA user_version=1')
            self.conn.commit()

    def close(self):
        self.conn.close()

    def document(self, doc_id):
        row = self.conn.execute('SELECT * FROM documents WHERE doc_id=?', (doc_id,)).fetchone()
        if row is None:
            raise AtlasError('论文 ID 不存在；使用 list 查看完整 doc_id。')
        return dict(row)

    def put(self, bundle, chunks, index_key, vectors=None, model_key=None):
        metadata = {k: v for k, v in bundle.items() if k != 'pages'}
        # One document transaction: an exception rolls back the old index as well.
        with self.conn:
            self.conn.execute('DELETE FROM documents WHERE doc_id=?', (bundle['doc_id'],))
            self.conn.execute('INSERT INTO documents VALUES(?,?,?,?,?,?)',
                              (bundle['doc_id'], bundle['title'], bundle['root'], bundle['fingerprint'],
                               index_key, json.dumps(metadata, ensure_ascii=False)))
            for page in bundle['pages']:
                self.conn.execute('INSERT INTO pages VALUES(?,?,?)', (bundle['doc_id'], page['page'], page['text']))
            from .retrieval import lexical_text
            for i, chunk in enumerate(chunks):
                row = self.conn.execute('''INSERT INTO chunks
                    (doc_id,page,start,end,section,excerpt,retrieval,warnings,vector,model_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (bundle['doc_id'], chunk['page'], chunk['start'], chunk['end'], chunk['section'],
                     chunk['excerpt'], chunk['retrieval'], json.dumps(chunk['warnings'], ensure_ascii=False),
                     vectors[i].tobytes() if vectors is not None else None, model_key))
                self.conn.execute('INSERT INTO chunks_fts(rowid,text) VALUES(?,?)',
                                  (row.lastrowid, lexical_text(chunk['retrieval'])))
