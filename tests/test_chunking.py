from paperatlas.chunking import Chunker


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
