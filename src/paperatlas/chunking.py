"""Single-page chunks with literal character offsets and bounded embedding inputs."""
import re
from html import unescape
from markdown_it import MarkdownIt
from .references import bibliography_entry, reference_heading

VERSION = 'page-block-v7'
TARGET_TOKENS = 350
PARSER = MarkdownIt('commonmark', {'html': True}).enable('table')


def plain(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    return re.sub(r'\s+', ' ', unescape(text)).strip()


class Chunker:
    def __init__(self, tokenizer=None):
        self.tokenizer = tokenizer
        self.key = VERSION + (':e5' if tokenizer else ':lexical')

    def offsets(self, text):
        if self.tokenizer:
            return self.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True,
                                  verbose=False)['offset_mapping']
        return [(m.start(), m.end()) for m in re.finditer(r'[\u4e00-\u9fff]|\w+|[^\w\s]', text)]

    def count(self, text):
        return len(self.offsets(text))

    def chunks(self, text, title, section='', reference_ranges=()):
        lines = text.splitlines(keepends=True)
        starts = [0]
        for line in lines:
            starts.append(starts[-1] + len(line))
        tokens = PARSER.parse(text)
        spans = []
        after_reference = False
        for token in tokens:
            if token.level == 0 and token.map and token.type not in {'inline', 'heading_close'}:
                begin, end = (starts[i] for i in token.map)
                if token.type == 'heading_open':
                    section = plain(text[begin:end].lstrip('#').strip())
                    after_reference = False
                    continue  # The heading accompanies body chunks as context, never as empty evidence.
                fragment = text[begin:end].strip()
                if (re.fullmatch(r'\[原页\s*\d+\]\([^)]*\)', fragment)
                        or fragment.startswith('原图核对：')
                        or not re.search(r'[\w\u4e00-\u9fff]', plain(fragment))):
                    continue
                # Block-level bibliography detection survives intervening figure headings
                # and references continuing on later pages. Raw pages remain available.
                # The heuristic fallback applies only in a bibliography context (its heading,
                # or right after aligned entries), so numbered body lists citing venues survive.
                structured = any(begin < b and end > a for a, b in reference_ranges)
                if structured or ((reference_heading(section) or after_reference)
                                  and bibliography_entry(plain(fragment))):
                    after_reference = True
                    continue
                after_reference = False
                spans.append((begin, end, section, token.type))
        # Math delimiters may span blank paragraphs. Coalesce all overlapping math regions.
        math_spans = [(m.start(), m.end()) for m in re.finditer(r'\$\$.*?\$\$', text, re.S)]
        for a, b in math_spans:
            covered = [s for s in spans if s[0] < b and s[1] > a]
            if covered:
                spans = [s for s in spans if s not in covered]
                spans.append((min(a, covered[0][0]), max(b, covered[-1][1]), covered[0][2], 'math'))
        spans.sort()
        table_headers = []
        expanded = []
        for a, b, heading, kind in spans:
            fragment = text[a:b]
            if kind in {'html_block', 'table_open'} and self.count(fragment) > TARGET_TOKENS and ('<table' in fragment or kind == 'table_open'):
                rows = list(re.finditer(r'<tr\b.*?</tr>' if '<table' in fragment else r'^\|[^\n]*', fragment, re.I | re.S if '<table' in fragment else re.M))
                if rows:
                    table_headers.append((a, b, plain(rows[0][0])[:300]))
                    group_start, group_end = a, a
                    for row in rows:
                        row_end = a + row.end()
                        if group_end > group_start and self.count(text[group_start:row_end]) > TARGET_TOKENS:
                            expanded.append((group_start, group_end, heading, 'table_fragment'))
                            group_start = a + row.start()
                        group_end = row_end
                    expanded.append((group_start, b, heading, 'table_fragment'))
                    continue
            expanded.append((a, b, heading, kind))
        spans = expanded
        groups = []
        for span in spans:
            if (groups and span[2] == groups[-1][2]
                    and span[3] == groups[-1][3] == 'paragraph_open'
                    and not text[groups[-1][1]:span[0]].strip()
                    and self.count(text[groups[-1][0]:span[1]]) <= TARGET_TOKENS):
                old = groups[-1]
                groups[-1] = (old[0], span[1], old[2], old[3])
            else:
                groups.append(span)
        output = []
        for a, b, heading, kind in groups:
            if not text[a:b].strip():
                continue
            context = plain(title + ' / ' + heading)
            context_tokens = self.offsets(context)
            if len(context_tokens) > 80:
                context = context[:context_tokens[79][1]]
            header = ''
            if kind == 'table_fragment':
                header = next(h for first, last, h in table_headers if first <= a < last)
            if kind in {'html_block', 'table_open'}:
                match = re.search(r'<tr\b.*?</tr>|^\|[^\n]+', text[a:b], re.I | re.S)
                if match:
                    header = plain(match[0])[:300]
            prefix = context + (' / ' + header if header else '') + '\n'
            # Leave ample room for prefixes, tokenizer boundary effects and special tokens.
            budget = min(TARGET_TOKENS, 490 - self.count('passage: ' + prefix))
            if budget < 32:
                prefix = context + '\n'
                budget = min(TARGET_TOKENS, 490 - self.count('passage: ' + prefix))
            offsets = self.offsets(text[a:b])
            position = 0
            while position < len(offsets):
                stop = min(position + budget, len(offsets))
                start_char = a if position == 0 else a + offsets[position][0]
                end_char = b if stop == len(offsets) else a + offsets[stop - 1][1]
                excerpt = text[start_char:end_char]
                retrieval = prefix + plain(excerpt)
                # HTML unescaping/tokenizer boundaries can expand token counts.
                while self.count('passage: ' + retrieval) + 2 > 512 and stop > position + 1:
                    stop -= 1
                    end_char = a + offsets[stop - 1][1]
                    excerpt = text[start_char:end_char]
                    retrieval = prefix + plain(excerpt)
                if self.count('passage: ' + retrieval) + 2 > 512:
                    raise ValueError('无法将检索片段限制在模型输入长度内')
                warnings = []
                if kind == 'table_fragment' or (len(offsets) > budget and ('$$' in text[a:b] or kind in {'math', 'table_open', 'html_block'})):
                    warnings.append('公式或表格片段不完整；请读取完整原页。')
                if excerpt.strip() and plain(excerpt):
                    output.append(dict(start=start_char, end=end_char, excerpt=excerpt,
                                       section=heading, kind=kind, retrieval=retrieval, warnings=warnings))
                if stop == len(offsets):
                    break
                position = max(position + 1, stop - (40 if kind == 'paragraph_open' else 0))
        return output, section
