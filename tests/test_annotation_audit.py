import importlib.util
from pathlib import Path


def test_annotation_audit_does_not_promote_duplicate_quotes(atlas, bundle):
    quote = 'PromptIR restores noisy images'
    source = bundle / 'document.md'
    source.write_text(source.read_text('utf-8') + '\n' + quote, 'utf-8')
    doc_id = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    page = atlas.read_page(doc_id, 1)['markdown']
    start = page.index(quote)
    data = {'queries':[dict(id='sample', evidence=[dict(doc_id=doc_id,page=1,start=start,end=start+len(quote),quote=quote)])]}
    spec = importlib.util.spec_from_file_location('audit_annotations', Path(__file__).parents[1]/'eval/audit_annotations.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.audit(atlas, data)
    assert not report['errors']
    assert report['additional_occurrence_candidates'][0]['page'] == 2
    assert not report['original_annotations_modified']
    assert len(data['queries'][0]['evidence']) == 1
    data['queries'][0]['evidence'][0]['start'] = 0
    assert module.audit(atlas, data)['errors']
