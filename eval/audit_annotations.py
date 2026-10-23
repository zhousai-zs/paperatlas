"""Audit literal evidence spans without modifying the frozen query set.

Duplicate quote occurrences are proposals only: matching text does not establish
semantic relevance. No model is invoked and no source content is sent anywhere.
"""
import argparse
import json
from pathlib import Path

from paperatlas.core import Atlas
from paperatlas.errors import AtlasError


def audit(atlas, queries):
    checked, errors, proposals, seen_docs = 0, [], [], set()
    for question in queries['queries']:
        for evidence in question['evidence']:
            checked += 1
            try:
                if evidence['doc_id'] not in seen_docs:
                    atlas._fresh(evidence['doc_id'])
                    seen_docs.add(evidence['doc_id'])
                page = atlas.read_page(evidence['doc_id'], evidence['page'])
                if page['markdown'][evidence['start']:evidence['end']] != evidence['quote']:
                    raise ValueError('Stored character range differs from source quote')
                existing = {(e['doc_id'], e['page']) for e in question['evidence']}
                for row in atlas.db.conn.execute('SELECT page,text FROM pages WHERE doc_id=?', (evidence['doc_id'],)):
                    position = row['text'].find(evidence['quote'])
                    if position >= 0 and (evidence['doc_id'], row['page']) not in existing:
                        proposal = dict(query_id=question['id'], doc_id=evidence['doc_id'],
                            page=row['page'], start=position, end=position+len(evidence['quote']),
                            quote=evidence['quote'], review_status='candidate_not_semantically_verified')
                        if proposal not in proposals:
                            proposals.append(proposal)
            except (AtlasError, ValueError, KeyError, OSError) as exc:
                errors.append(dict(query_id=question['id'], error=str(exc)))
    return dict(queries=len(queries['queries']), spans_checked=checked, errors=errors,
                additional_occurrence_candidates=proposals, original_annotations_modified=False,
                independent_human_review=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='.paperatlas')
    parser.add_argument('--queries', required=True)
    parser.add_argument('--output', default='reports/annotation-audit.json')
    args = parser.parse_args()
    atlas = Atlas(args.data_dir, read_only=True)
    try:
        report = audit(atlas, json.loads(Path(args.queries).read_text('utf-8')))
    finally:
        atlas.close()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({key: (len(value) if key == 'additional_occurrence_candidates' else value)
                      for key,value in report.items()}, ensure_ascii=False))
    raise SystemExit(bool(report['errors']))
