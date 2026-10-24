"""Locate structured bibliography text in the effective Markdown, never rewrite it."""
import re


def flatten(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(flatten(v) for v in value)
    return flatten(value.get('content', '')) if isinstance(value, dict) else ''


def normalized(text):
    # Ignore Markdown punctuation and spacing while retaining original offsets.
    # A rendered link contains its label once, not a second copy of its URL.
    targets = set()
    for match in re.finditer(r'\]\((?:[^()]|\([^()]*\))*\)', text):
        targets.update(range(match.start() + 1, match.end()))
    pairs = [(c.lower(), i) for i, c in enumerate(text) if c.isalnum() and i not in targets]
    return ''.join(c for c, _ in pairs), [i for _, i in pairs]


def reference_ranges(text, blocks):
    haystack, offsets = normalized(text)
    ranges = []
    for block in blocks:
        if block.get('type') != 'ref_text':
            continue
        needle, _ = normalized(flatten(block))
        if len(needle) < 10:
            continue
        start = haystack.find(needle)
        if start >= 0:
            ranges.append((offsets[start], offsets[start + len(needle) - 1] + 1))
    return sorted(set(ranges))


REFERENCE_HEADING = re.compile(r'^(?:(?:\d+|[IVX]+)[.)]?\s*)?(?:references?|bibliography|参考文献|literature cited)\b',
                               re.I)


def reference_heading(section):
    return bool(REFERENCE_HEADING.match(section.strip()))


def bibliography_entry(text):
    """Conservative fallback for numbered entries missing structured alignment.

    Only meaningful inside a bibliography context: numbered body lists can cite
    venues and years too. Callers decide the context.
    """
    return bool(re.match(r'^\s*(?:\[\d+\]|\d+\.)\s+', text)
                and re.search(r'\b(?:19|20)\d{2}\b', text)
                and re.search(r'\b(?:arxiv|proceedings|ieee|cvpr|iccv|eccv|neurips|'
                              r'iclr|journal|transactions|conference|et al)\b|'
                              r'\b[A-Z][\w-]+,\s*[A-Z]\.', text, re.I))
