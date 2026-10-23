import json
import hashlib
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


def test_search_expands_context_without_including_excluded_bibliography(atlas, bundle):
    text = ('<!-- source_page: 1 -->\n# Method\n\n'
            + 'Background context for restoration. ' * 60 + '\n\n'
            + 'UniqueAnchor explains the central mechanism. ' * 30 + '\n\n'
            + 'Surrounding implementation details. ' * 40 + '\n\n'
            + '99. Zhang, C.: Unrelated restoration work. In: CVPR (2023).\n\n'
            + 'A separate claim after the citation.\n\n'
            + '<!-- source_page: 2 -->\n# Experiments\n\nEvaluation details.\n')
    (bundle / 'document.md').write_text(text, 'utf-8')
    atlas.index(bundle, 'lexical')
    hit = atlas.search_papers('UniqueAnchor', mode='lexical')['results'][0]
    page = atlas.read_page(hit['doc_id'], 1)
    start, end = hit['source_range']['start'], hit['source_range']['end']
    assert hit['excerpt'] == page['markdown'][start:end]
    assert start <= hit['match_range']['start'] < hit['match_range']['end'] <= end
    assert end - start > hit['match_range']['end'] - hit['match_range']['start']
    assert 'Background context' in hit['excerpt'] and 'Surrounding implementation' in hit['excerpt']
    assert 'Zhang' not in hit['excerpt'] and 'A separate claim' not in hit['excerpt']


def test_read_page_neighbors_are_complete_bounded_and_keep_page_attribution(atlas, bundle):
    doc = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    page = atlas.read_page(doc, 1, before=2, after=2)
    assert [p['page'] for p in page['neighboring_pages']] == [2]
    assert '31.2' in page['neighboring_pages'][0]['markdown']
    assert page['neighboring_pages'][0]['source']['page_image'].endswith('page-0002.png')
    assert not page['omitted_pages']
    for bad in [-1, 3, True, 1.5]:
        with pytest.raises(AtlasError):
            atlas.read_page(doc, 1, after=bad)
    with atlas.db.conn:
        atlas.db.conn.execute('UPDATE pages SET text=? WHERE page=2', ('very long table ' * 3000,))
    page = atlas.read_page(doc, 1, after=1)
    assert not page['neighboring_pages'] and page['omitted_pages'] == [2]
    assert atlas.read_page(doc, 2)['markdown'] == 'very long table ' * 3000


def test_topic_search_spreads_evidence_and_doc_filter_allows_deeper_reading(atlas, bundle):
    document = json.loads((bundle / 'document.json').read_text('utf-8'))
    document['pages'] += [dict(page_idx=i,blocks=[]) for i in (2,3)]
    for page in (3,4):
        (bundle / f'pages/page-{page:04d}.png').write_bytes(b'image')
    (bundle / 'document.json').write_text(json.dumps(document), 'utf-8')
    (bundle / 'document.md').write_text(''.join(
        f'<!-- source_page: {p} -->\n# Method\n\nrestoration shared evidence {p}.\n' for p in range(1,5)), 'utf-8')
    first = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    second = bundle.parent / 'second'
    shutil.copytree(bundle, second)
    (second / 'source.pdf').write_bytes(b'%PDF-second-source')
    document['source_sha256'] = hashlib.sha256((second / 'source.pdf').read_bytes()).hexdigest()
    (second / 'document.json').write_text(json.dumps(document), 'utf-8')
    atlas.index(second, 'lexical')
    hits = atlas.search_papers('restoration', mode='lexical', diverse=True)['results']
    assert len(hits) == 4 and len({h['doc_id'] for h in hits}) == 2
    assert len(atlas.search_papers('restoration', doc_id=first, mode='lexical', diverse=True)['results']) == 4
    assert len(atlas.search_papers('restoration', mode='lexical')['results']) == 5
