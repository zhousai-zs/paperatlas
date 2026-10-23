"""Real stdio client/server round trip; distinct from an interactive Agent-host check."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def unpack(result):
    if result.is_error:
        raise RuntimeError(str(result.content))
    data = result.structured_content
    if data is not None:
        return data
    return json.loads(next(c.text for c in result.content if hasattr(c, 'text')))


async def run(data_dir):
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'paperatlas', '--data-dir', str(Path(data_dir).resolve()), 'mcp'],
        env={**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
    start = time.perf_counter()
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=180) as client:
            await client.initialize()
            tools = await client.list_tools()
            listing = unpack(await client.call_tool('list_papers', {}))
            search = unpack(await client.call_tool('search_papers', {'query': 'DPMambaIR DP-SSM fine-grained degradation extractor', 'limit': 5}))
            hit = search['results'][0]
            page = unpack(await client.call_tool('read_page', {'doc_id': hit['doc_id'], 'page': hit['page']}))
            assert hit['excerpt'] in page['markdown']
            repeated = unpack(await client.call_tool('search_papers', {'query': 'SEAR Pruning-Aware Monte Carlo Tree Search', 'limit': 5}))
            assert repeated['results']
            return dict(protocol='stdio', tools=[t.name for t in tools.tools], papers=listing['total'],
                        search_title=hit['title'], page=hit['page'], excerpt_verified=True,
                        repeated_search=True, elapsed_seconds=time.perf_counter()-start,
                        agent_host_verified=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default='.paperatlas')
    parser.add_argument('--output')
    args = parser.parse_args()
    report = asyncio.run(run(args.data_dir))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, 'utf-8')
    print(text)
