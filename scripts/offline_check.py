"""Invoked with scripts/offline_guard on PYTHONPATH; verifies the guard and local operations."""
import asyncio
import json
from pathlib import Path
import socket
import argparse

from paperatlas.core import Atlas
from mcp_smoke import run


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=r'D:\KnowledgeBase\papers\图像复原')
    parser.add_argument('--data-dir', default='.paperatlas')
    parser.add_argument('--output', default='reports/offline-verification.json')
    args = parser.parse_args()
    try:
        socket.create_connection(('127.0.0.1', 9), timeout=.1)
    except RuntimeError as exc:
        assert 'blocked a TCP' in str(exc)
    else:
        raise AssertionError('TCP guard is not active')
    atlas = Atlas(args.data_dir)
    try:
        import_report = atlas.index(args.root)
        assert not any(r['result'] == 'failed' for r in import_report['results'])
        assert atlas.search_papers('DiTTo ORA')['results']
    finally:
        atlas.close()
    report = dict(tcp_denied=True, import_results=import_report,
                  mcp=asyncio.run(run(args.data_dir)))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print('Offline import/search/MCP passed with TCP connections denied.')
