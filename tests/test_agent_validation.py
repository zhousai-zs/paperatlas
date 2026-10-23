"""Synthetic checks for the real-model acceptance grader; no model/network needed."""
import importlib.util
from pathlib import Path


def grader():
    spec = importlib.util.spec_from_file_location('agent_eval', Path(__file__).parents[1] / 'scripts/codex_agent_eval.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate


def test_agent_cannot_pass_with_fabricated_or_unread_citations(atlas, bundle):
    doc = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    evidence = dict(doc_id=doc, page=1, quote='PromptIR restores noisy images', claim='Restoration method')
    answer = dict(answer='PromptIR', evidence=[evidence], source_warnings=['OCR unverified'], insufficient_evidence=False)
    events = [dict(type='item.completed', item=dict(type='mcp_tool_call', server='paperatlas', tool='search_papers', arguments={'query':'PromptIR'})),
              dict(type='item.completed', item=dict(type='mcp_tool_call', server='paperatlas', tool='read_page',
                   arguments=dict(doc_id=doc,page=1), result={'structuredContent': atlas.read_page(doc, 1)}))]
    validate = grader()
    assert validate(atlas, {}, answer, events)['passed']
    evidence['quote'] = 'A fabricated statement not present in the paper'
    assert not validate(atlas, {}, answer, events)['passed']
    evidence['quote'] = 'LOL-v2 benchmark achieves PSNR 31.2.'
    evidence['page'] = 2
    assert 'Cited a page not read in this turn' in validate(atlas, {}, answer, events)['errors']


def test_agent_can_cite_returned_neighbors_but_not_failed_or_omitted_pages(atlas, bundle):
    doc = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    payload = atlas.read_page(doc, 1, after=1)
    event = dict(type='item.completed', item=dict(type='mcp_tool_call', server='paperatlas', tool='read_page',
        arguments=dict(doc_id=doc,page=1,after=1), result={'structuredContent': payload}))
    events = [dict(type='item.completed', item=dict(type='mcp_tool_call', server='paperatlas',
              tool='search_papers', arguments={'query':'LOL-v2'})), event]
    answer = dict(answer='31.2', evidence=[dict(doc_id=doc,page=2,quote='LOL-v2 benchmark achieves PSNR 31.2.',claim='Result')],
                  source_warnings=['OCR unverified'],insufficient_evidence=False)
    validate = grader()
    assert validate(atlas, {}, answer, events)['passed']
    payload['neighboring_pages'] = []
    assert not validate(atlas, {}, answer, events)['passed']
    payload['neighboring_pages'] = [dict(page=2,markdown='Wrong returned content')]
    assert not validate(atlas, {}, answer, events)['passed']
    event['item']['result']['isError'] = True
    assert not validate(atlas, {}, answer, events)['passed']


def test_agent_bypass_and_false_sufficiency_are_rejected(atlas):
    answer = dict(answer='unfounded claim', evidence=[], source_warnings=['OCR unverified'], insufficient_evidence=False)
    events = [dict(type='item.completed', item=dict(type='command_execution'))]
    report = grader()(atlas, {'insufficient_evidence': True}, answer, events)
    assert not report['passed']
    assert any('outside acceptance scope' in error for error in report['errors'])
    assert any('sufficiency' in error for error in report['errors'])


def test_usage_limit_is_an_execution_blocker_not_a_content_grade():
    spec = importlib.util.spec_from_file_location('agent_eval', Path(__file__).parents[1] / 'scripts/codex_agent_eval.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    failure = [dict(type='turn.failed', error={'message': 'You have hit your usage limit'})]
    assert module.execution_status(1, failure) == 'blocked_usage_limit'
    assert module.execution_status(1, [dict(type='error', message='Tool failed')]) == 'execution_failed'
    # A quoted phrase in a successful model answer must not be treated as a service error.
    assert module.execution_status(0, [dict(type='item.completed', item={'text': 'usage limit'})]) == 'completed'
