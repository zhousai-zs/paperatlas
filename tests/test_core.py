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
    # An isolated citation in a body section is excluded through upstream ref_text alignment.
    document = json.loads((bundle / 'document.json').read_text('utf-8'))
    document['pages'][0]['blocks'].append(
        dict(type='ref_text', content='99. Zhang, C.: Unrelated restoration work. In: CVPR (2023).'))
    (bundle / 'document.json').write_text(json.dumps(document), 'utf-8')
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


def copy_bundle(bundle, name, pdf):
    copy = bundle.parent / name
    shutil.copytree(bundle, copy)
    (copy / 'source.pdf').write_bytes(pdf)
    document = json.loads((copy / 'document.json').read_text('utf-8'))
    document['source_sha256'] = hashlib.sha256(pdf).hexdigest()
    (copy / 'document.json').write_text(json.dumps(document), 'utf-8')
    return copy


def test_stale_sources_are_skipped_without_failing_search(atlas, bundle):
    kept = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    deleted_root = copy_bundle(bundle, 'deleted', b'%PDF-deleted')
    changed_root = copy_bundle(bundle, 'changed', b'%PDF-changed')
    deleted = atlas.index(deleted_root, 'lexical')['results'][0]['doc_id']
    changed = atlas.index(changed_root, 'lexical')['results'][0]['doc_id']
    shutil.rmtree(deleted_root)
    path = changed_root / 'document.md'
    path.write_text(path.read_text('utf-8') + '\nEdited\n', 'utf-8')
    response = atlas.search_papers('PromptIR', mode='lexical')
    assert {r['doc_id'] for r in response['results']} == {kept}
    assert {(d['doc_id'], d['reason']) for d in response['stale_documents']} == {(deleted, 'missing'), (changed, 'changed')}
    assert any('stale_documents' in w for w in response['warnings'])
    # Explicitly targeting or reading a stale paper still fails loudly.
    with pytest.raises(AtlasError, match='remove/prune'):
        atlas.search_papers('PromptIR', mode='lexical', doc_id=deleted)
    with pytest.raises(AtlasError, match='过期'):
        atlas.read_page(changed, 1)
    clean = atlas.search_papers('PromptIR', mode='lexical', doc_id=kept)
    assert clean['results'] and not clean['stale_documents'] and not clean['warnings']


def test_prune_removes_only_unavailable_sources_and_remove_cascades(atlas, bundle):
    kept = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    deleted_root = copy_bundle(bundle, 'deleted', b'%PDF-deleted')
    changed_root = copy_bundle(bundle, 'changed', b'%PDF-changed')
    deleted = atlas.index(deleted_root, 'lexical')['results'][0]['doc_id']
    changed = atlas.index(changed_root, 'lexical')['results'][0]['doc_id']
    shutil.rmtree(deleted_root)
    (changed_root / 'report.json').write_text(json.dumps(dict(status='needs_review')), 'utf-8')
    preview = atlas.prune(dry_run=True)
    assert [c['doc_id'] for c in preview['candidates']] == [deleted]
    assert atlas.list_papers()['total'] == 3
    assert [r['doc_id'] for r in atlas.prune()['removed']] == [deleted]
    assert atlas.prune() == dict(dry_run=False, removed=[])
    assert [d['doc_id'] for d in atlas.search_papers('PromptIR', mode='lexical')['stale_documents']] == [changed]
    with pytest.raises(AtlasError, match='不存在'):
        atlas.remove([kept, 'sha256:unknown'])
    assert atlas.list_papers()['total'] == 2
    assert atlas.remove([changed])['removed'][0]['doc_id'] == changed
    conn = atlas.db.conn
    assert [r[0] for r in conn.execute('SELECT doc_id FROM documents')] == [kept]
    assert conn.execute('SELECT COUNT(*) FROM pages WHERE doc_id!=?', (kept,)).fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM chunks WHERE doc_id!=?', (kept,)).fetchone()[0] == 0
    assert (conn.execute('SELECT COUNT(*) FROM chunks_fts').fetchone()[0]
            == conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0])
    # Removing from the index never touches source files.
    assert (changed_root / 'source.pdf').is_file()


def test_cli_remove_and_prune(atlas, bundle, capsys):
    from paperatlas.cli import main
    doc = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    data = str(atlas.db.directory)
    assert main(['--data-dir', data, 'prune', '--dry-run']) == 0
    assert json.loads(capsys.readouterr().out) == dict(dry_run=True, candidates=[])
    assert main(['--data-dir', data, 'remove', 'sha256:unknown']) == 2
    assert main(['--data-dir', data, 'remove', doc]) == 0
    assert json.loads(capsys.readouterr().out)['removed'][0]['doc_id'] == doc
    assert atlas.list_papers()['total'] == 0


def test_long_query_is_fit_to_semantic_model_while_lexical_uses_all_terms(atlas, bundle, fake_embedder):
    atlas._embedder = fake_embedder
    atlas.index(bundle)
    short = atlas.search_papers('PromptIR degradation')
    assert short['results'] and not short['warnings']
    query = '图像退化恢复' * 200 + ' PromptIR'
    assert fake_embedder.tokenizer.encode('query: ' + query)[512:]
    response = atlas.search_papers(query)
    assert response['results'] and any('512 tokens' in w for w in response['warnings'])
    model_input = fake_embedder.model.inputs[-1]
    assert model_input.startswith('query: 图像退化恢复') and len(fake_embedder.tokenizer.encode(model_input)) <= 512
    # The term beyond the model window still reaches the keyword channel.
    assert any('PromptIR' in r['excerpt'] for r in atlas.search_papers(query, mode='lexical')['results'])
    assert atlas.search_papers(query, mode='semantic')['warnings']


def test_atlas_context_manager(tmp_path):
    from paperatlas.core import Atlas
    data_dir = tmp_path / 'ctx_data'
    with Atlas(data_dir, create=True) as instance:
        assert instance.list_papers()['total'] == 0
        conn = instance.db.conn
    with pytest.raises(sqlite3.ProgrammingError, match='Cannot operate on a closed database'):
        conn.execute('SELECT 1')
