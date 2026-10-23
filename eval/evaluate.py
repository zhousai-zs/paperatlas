"""Reproducible page-level evidence retrieval evaluation. No generated answers."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics
import time

import psutil

from paperatlas.core import Atlas
from paperatlas.errors import AtlasError


def evaluate(data_dir, queries_path, split):
    source = json.loads(Path(queries_path).read_text('utf-8'))
    atlas = Atlas(data_dir)
    try:
        labels = []
        for question in source['queries']:
            if split != 'all' and question['split'] != split:
                continue
            for evidence in question['evidence']:
                page = atlas.read_page(evidence['doc_id'], evidence['page'])
                assert page['markdown'][evidence['start']:evidence['end']] == evidence['quote']
            labels.append(question)
        # Warm-up measured separately from warm query latency.
        warm_start = time.perf_counter()
        atlas.search_papers('image restoration', mode='hybrid')
        warm_seconds = time.perf_counter() - warm_start
        measurements = []
        for mode in ['lexical', 'semantic', 'hybrid']:
            for question in labels:
                start = time.perf_counter()
                response = atlas.search_papers(question['query'], mode=mode)
                elapsed = time.perf_counter() - start
                expected = {(e['doc_id'], e['page']) for e in question['evidence']}
                reciprocal = 0.0
                for rank, result in enumerate(response['results'], 1):
                    if (result['doc_id'], result['page']) in expected:
                        reciprocal = 1.0 / rank
                        break
                measurements.append(dict(id=question['id'], split=question['split'], mode=mode,
                    category=question['category'], hit=bool(reciprocal), reciprocal_rank=reciprocal,
                    seconds=elapsed, results=[dict(doc_id=r['doc_id'], page=r['page'],
                                                  chunk_id=r['chunk_id']) for r in response['results']]))
        summary = {}
        for mode in ['lexical', 'semantic', 'hybrid']:
            subset = [m for m in measurements if m['mode'] == mode]
            latencies = sorted(m['seconds'] for m in subset)
            import math
            summary[mode] = dict(queries=len(subset), hit_at_5=statistics.mean(m['hit'] for m in subset),
                                 mrr_at_5=statistics.mean(m['reciprocal_rank'] for m in subset),
                                 p95_seconds=latencies[math.ceil(.95 * len(latencies)) - 1])
        negatives = []
        for query in source['negatives']:
            try:
                result = atlas.search_papers(query)
                negatives.append(dict(query=query if len(query) < 100 else '<overlong>',
                                      outcome='candidates' if result['results'] else 'empty',
                                      candidates=len(result['results']), generates_answer=False))
            except AtlasError as exc:
                negatives.append(dict(query=query if len(query) < 100 else '<overlong>', outcome='rejected', error=str(exc)))
        return dict(split=split, annotation=source['annotation'], source_span_checks=len(labels),
                    summary=summary, warmup_seconds=warm_seconds,
                    process_rss_mb=psutil.Process().memory_info().rss / 1024**2,
                    model=atlas.embedder.info, measurements=measurements, negative_cases=negatives,
                    note='Page-level Hit@5 and first relevant chunk MRR@5; not exhaustive Recall or answer correctness.')
    finally:
        atlas.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='.paperatlas')
    parser.add_argument('--queries', default=str(Path(__file__).with_name('queries.json')))
    parser.add_argument('--split', choices=['dev', 'acceptance', 'all'], default='dev')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = evaluate(args.data_dir, args.queries, args.split)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report['summary'], indent=2))
