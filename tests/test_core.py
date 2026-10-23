import json
import shutil
import sqlite3

import pytest

from paperatlas.errors import AtlasError
from paperatlas.source import read_bundle


def test_import_search_read_literal_offsets(atlas, bundle):
    report = atlas.index(bundle, 'lexical')
    assert report['results'][0]['result'] == 'indexed'
    result = atlas.search_papers('LOL-v2 PSNR', mode='lexical')['results'][0]
    assert result['page'] == 2
    page = atlas.read_page(result['doc_id'], 2)
    span = result['source_range']
    assert page['markdown'][span['start']:span['end']] == result['excerpt']
    assert '31.2' in page['markdown']
    assert page['warnings'] and not page['integrity']


def test_idempotent_relocation_incremental_and_stale(atlas, bundle, tmp_path):
    first = atlas.index(bundle, 'lexical')['results'][0]
    count = atlas.db.conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    assert atlas.index(bundle, 'lexical')['results'][0]['result'] == 'skipped'
    moved = tmp_path / 'moved'
    shutil.copytree(bundle, moved)
    assert atlas.index(moved, 'lexical')['results'][0]['result'] == 'relocated'
    assert atlas.db.conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] == count
    path = moved / 'document.md'
    path.write_text(path.read_text('utf-8') + '\nNew evidence\n', 'utf-8')
    with pytest.raises(AtlasError, match='过期'):
        atlas.read_page(first['doc_id'], 1)
    assert atlas.index(moved, 'lexical')['results'][0]['result'] == 'updated'
    assert 'New evidence' in atlas.read_page(first['doc_id'], 2)['markdown']


def test_failure_preserves_old_transaction(atlas, bundle):
    first = atlas.index(bundle, 'lexical')['results'][0]
    old = atlas.db.document(first['doc_id'])
    atlas.db.conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON chunks BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    path = bundle / 'document.md'
    path.write_text(path.read_text('utf-8') + '\nchange', 'utf-8')
    assert atlas.index(bundle, 'lexical')['results'][0]['result'] == 'failed'
    assert atlas.db.document(first['doc_id']) == old


@pytest.mark.parametrize('change, message', [
    ('hash', 'SHA-256'), ('schema', 'schema_version'), ('pages', '页码'),
    ('missing', '缺失文件'), ('traversal', '越界'), ('revision', '未合并修订'), ('integrity', '未通过')])
def test_reject_invalid_bundle(bundle, change, message):
    path = bundle / 'document.json'
    doc = json.loads(path.read_text('utf-8'))
    if change == 'hash':
        (bundle / 'source.pdf').write_bytes(b'changed')
    elif change == 'schema':
        doc['schema_version'] = 9
    elif change == 'pages':
        doc['pages'][1]['page_idx'] = 3
    elif change == 'missing':
        (bundle / 'pages/page-0001.png').unlink()
    elif change == 'traversal':
        (bundle / 'document.md').write_text((bundle / 'document.md').read_text('utf-8') + '\n![](../outside.png)', 'utf-8')
    elif change == 'revision':
        (bundle / 'review').mkdir()
        (bundle / 'review/revisions.json').write_text('{"pages":{"1":{"markdown":"new"}}}', 'utf-8')
    elif change == 'integrity':
        (bundle / 'integrity.json').write_text('{"passed":false}', 'utf-8')
    path.write_text(json.dumps(doc), 'utf-8')
    with pytest.raises(AtlasError, match=message):
        read_bundle(bundle)


def test_review_state_and_batch_continuation(atlas, bundle):
    second = bundle.parent / 'broken'
    second.mkdir()
    (second / 'document.json').write_text('{}', 'utf-8')
    (bundle / 'report.json').write_text(json.dumps(dict(status='needs_review', issues=[dict(code='unbalanced_display_math')])), 'utf-8')
    report = atlas.index(bundle.parent, 'lexical')
    assert {r['result'] for r in report['results']} == {'failed', 'indexed'}
    result = atlas.search_papers('PromptIR', mode='lexical')['results'][0]
    assert result['status'] == 'needs_review'
    assert any('unbalanced_display_math' in w for w in result['warnings'])
    assert atlas.read_page(result['doc_id'], 1)['status'] == 'needs_review'


def test_query_validation_and_literal_fts(atlas, bundle):
    atlas.index(bundle, 'lexical')
    for query in ['', ' ', 'x' * 4001]:
        with pytest.raises(AtlasError):
            atlas.search_papers(query, mode='lexical')
    for query in ['" OR * NEAR(', '不存在的术语zzzzxyzz', 'AND NOT OR']:
        assert isinstance(atlas.search_papers(query, mode='lexical')['results'], list)
    assert not atlas.search_papers('zzzzxyzz', mode='lexical')['results']
    with pytest.raises(AtlasError, match='本地模型未准备'):
        atlas.search_papers('test')


def test_deleted_sources_and_invalid_identity(atlas, bundle):
    entry = atlas.index(bundle, 'lexical')['results'][0]
    with pytest.raises(AtlasError):
        atlas.read_page('../source.pdf', 1)
    with pytest.raises(AtlasError):
        atlas.read_page(entry['doc_id'], 0)
    (bundle / 'document.md').unlink()
    with pytest.raises(AtlasError):
        atlas.read_page(entry['doc_id'], 1)


def test_chinese_domain_terms_find_english_evidence(atlas, bundle):
    atlas.index(bundle, 'lexical')
    response = atlas.search_papers('退化提示', mode='lexical')
    assert any('degradation prompts' in r['excerpt'] for r in response['results'])
    assert response['query'] == '退化提示'


def test_mcp_database_connection_really_read_only(atlas):
    from paperatlas.core import Atlas
    reader = Atlas(atlas.db.directory, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.db.conn.execute('DELETE FROM documents')
    finally:
        reader.close()
