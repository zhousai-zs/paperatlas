from paperatlas.chunking import Chunker
from paperatlas.references import reference_ranges


def test_long_text_math_and_table_preserve_sources():
    text = '# Results\n\n' + 'restoration noise image ' * 600 + '\n\n$$\n' + 'x + y ' * 500 + '\n$$\n\n'
    text += '<table><tr><th>Dataset</th><th>PSNR</th></tr>' + ''.join(f'<tr><td>Rain{i}</td><td>31.2</td></tr>' for i in range(120)) + '</table>\n'
    chunker = Chunker()
    chunks, section = chunker.chunks(text, 'Example paper')
    assert section == 'Results' and len(chunks) > 8
    assert all(c['excerpt'] == text[c['start']:c['end']] for c in chunks)
    assert all(chunker.count('passage: ' + c['retrieval']) + 2 <= 512 for c in chunks)
    assert any(c['warnings'] for c in chunks)
    assert any('Dataset' in c['retrieval'] and 'Rain' in c['retrieval'] for c in chunks)


def test_navigation_and_bare_headings_do_not_become_evidence():
    text = '[原页 2](pages/page-0002.png)\n\n# Method\n\n---\n\n原图核对：[表格 1](evidence/t.png)\n\nActual restoration evidence.\n'
    chunks, _ = Chunker().chunks(text, 'Paper title')
    assert len(chunks) == 1
    assert chunks[0]['excerpt'].strip() == 'Actual restoration evidence.'
    assert chunks[0]['section'] == 'Method'


def test_structured_references_do_not_leak_past_figure_headings_or_merge_into_body():
    citation = '99. Zhang, C., Zhu, Y.: All-in-one degradation network. In: CVPR (2023).'
    text = '# Instruction examples\n\nUseful body before.\n\n' + citation + '\n\nUseful body after.\n'
    ranges = reference_ranges(text, [dict(type='ref_text', content=[dict(content=citation)])])
    chunks, _ = Chunker().chunks(text, 'Paper', reference_ranges=ranges)
    assert len(chunks) == 2
    assert all('Zhang' not in c['excerpt'] for c in chunks)
    assert 'Useful body before.' in chunks[0]['excerpt']
    assert 'Useful body after.' in chunks[1]['excerpt']
    # A numbered experimental procedure with a year is not a bibliography entry.
    chunks, _ = Chunker().chunks('# Experiments\n\n1. Train using 2023 images and compare the results.\n', 'Paper')
    assert chunks


def test_reference_continuation_without_heading_and_appendix_are_separate():
    citation = 'IEEE/CVF Conference on Computer Vision (2023), pp. 5815–5824.'
    text = citation + '\n\n# Appendix\n\nAdditional experimental details.\n'
    ranges = reference_ranges(text, [dict(type='ref_text', content=citation)])
    chunks, section = Chunker().chunks(text, 'Paper', 'References', ranges)
    assert section == 'Appendix' and len(chunks) == 1
    assert 'Additional experimental details.' in chunks[0]['excerpt']


def test_markdown_punctuation_does_not_break_structured_reference_alignment():
    text = '[62] A. Author. **Restoration** with prompts. CVPR, 2023.\n'
    ranges = reference_ranges(text, [dict(type='ref_text', content='62 A Author Restoration with prompts CVPR 2023')])
    assert ranges and not Chunker().chunks(text, 'Paper', reference_ranges=ranges)[0]


def test_reference_urls_and_short_continuations_keep_original_offsets():
    citation = 'OpenAI. Model card, 2023. URL https://example.com/card.pdf.'
    text = citation.replace('https://example.com/card.pdf', '[https://example.com/card.pdf](https://example.com/card.pdf)')
    text += '\n\n28 208–28 220.\n'
    ranges = reference_ranges(text, [dict(type='ref_text', content=citation),
                                    dict(type='ref_text', content='28 208–28 220.')])
    assert len(ranges) == 2
    assert not Chunker().chunks(text, 'Paper', reference_ranges=ranges)[0]
