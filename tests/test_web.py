import hashlib
import json
import shutil
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from paperatlas.cli import main
from paperatlas.errors import AtlasError
from paperatlas.web import make_server


def test_help_lists_web_separately_from_mcp(capsys):
    with pytest.raises(SystemExit) as exc:
        main(['--help'])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert 'web' in text and 'mcp' in text
    page = Path('src/paperatlas/static/index.html').read_text(encoding='utf-8')
    source = Path('src/paperatlas/web.py').read_text(encoding='utf-8')
    assert 'innerHTML' not in page and 'insertAdjacentHTML' not in page
    assert 'mcp_server' not in source and 'Embedder' not in source


def test_web_rejects_non_loopback(atlas):
    with pytest.raises(AtlasError, match='本机'):
        make_server(str(atlas.db.directory), '0.0.0.0', 8765)


@pytest.fixture
def browser(atlas, bundle):
    atlas.index(bundle, 'lexical')
    server = make_server(str(atlas.db.directory), '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    wait_until_ready(base)
    yield base, bundle
    server.shutdown()
    server.server_close()


def wait_until_ready(base):
    for _ in range(50):
        try:
            with urlopen(base + '/api/summary', timeout=0.2) as response:
                response.read()
            return
        except (URLError, TimeoutError, OSError):
            time.sleep(0.05)
    raise AssertionError('管理页面没有启动')


def get(base, path):
    with urlopen(base + path) as response:
        return response.status, response.read()


def post(base, path, payload, origin=None):
    data = json.dumps(payload).encode()
    headers = {'Content-Type': 'application/json'}
    if origin:
        headers['Origin'] = origin
    request = Request(base + path, data=data, headers=headers)
    try:
        with urlopen(request) as response:
            return response.status, json.loads(response.read().decode())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_browse_search_and_remove_keeps_source_files(browser):
    base, bundle = browser
    status, page = get(base, '/')
    assert status == 200 and '索引'.encode() in page
    summary = json.loads(get(base, '/api/summary')[1])
    assert summary['documents'] == 1 and summary['vectors'] == 0 and summary['modes'] == ['仅关键词']
    papers = json.loads(get(base, '/api/papers')[1])['papers']
    assert papers[0]['title'] == 'Example restoration'
    doc_id = papers[0]['doc_id']
    body = json.loads(get(base, '/api/page?doc_id=' + doc_id + '&page=2')[1])
    assert '31.2' in body['text']
    assert any('PSNR' in chunk['excerpt'] for chunk in body['chunks'])
    hits = json.loads(get(base, '/api/search?q=PSNR')[1])['hits']
    assert hits[0]['page'] == 2 and hits[0]['doc_id'] == doc_id
    (bundle / 'document.md').unlink()
    report = json.loads(get(base, '/api/sources')[1])
    assert report['missing'] == 1
    assert '31.2' in json.loads(get(base, '/api/page?doc_id=' + doc_id + '&page=2')[1])['text']
    code, removed = post(base, '/api/remove', {'doc_ids': [doc_id]})
    assert code == 200 and removed['removed'][0]['doc_id'] == doc_id
    assert (bundle / 'source.pdf').is_file()
    assert json.loads(get(base, '/api/summary')[1])['documents'] == 0
    assert post(base, '/api/remove', {'doc_ids': ['sha256:' + 'ab' * 32]}, origin='http://evil.example')[0] == 403


def test_prune_only_drops_missing_sources(atlas, bundle, tmp_path):
    atlas.index(bundle, 'lexical')
    kept = tmp_path / 'kept'
    shutil.copytree(bundle, kept)
    pdf = kept / 'source.pdf'
    pdf.write_bytes(b'%PDF-kept source')
    document = json.loads((kept / 'document.json').read_text(encoding='utf-8'))
    document['source_sha256'] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    document['pages'][0]['blocks'][0]['content'][0]['content'] = 'Kept paper'
    (kept / 'document.json').write_text(json.dumps(document), encoding='utf-8')
    atlas.index(kept, 'lexical')
    shutil.rmtree(bundle)
    server = make_server(str(atlas.db.directory), '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    wait_until_ready(base)
    try:
        code, preview = post(base, '/api/prune', {})
        assert code == 200 and preview['dry_run'] is True
        assert [item['title'] for item in preview['candidates']] == ['Example restoration']
        assert post(base, '/api/prune', {'dry_run': False})[1]['removed']
        papers = json.loads(get(base, '/api/papers')[1])['papers']
        assert [item['title'] for item in papers] == ['Kept paper']
    finally:
        server.shutdown()
        server.server_close()
    assert (kept / 'source.pdf').is_file()
