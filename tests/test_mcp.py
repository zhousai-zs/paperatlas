import asyncio
from collections import Counter
import json
import os
import sys
import threading

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_cached_connections_are_thread_local_and_see_index_updates(atlas, bundle, monkeypatch):
    from paperatlas import mcp_server
    from paperatlas.core import Atlas

    doc = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    opened_threads = []

    class TrackedAtlas(Atlas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            assert kwargs['read_only'] is True
            opened_threads.append(threading.get_ident())

    monkeypatch.setattr(mcp_server, 'Atlas', TrackedAtlas)
    server = mcp_server.create_server(atlas.db.directory)

    async def call(name, arguments):
        result = await server.call_tool(name, arguments)
        assert not result.is_error
        return result.structured_content or json.loads(result.content[0].text)

    for _ in range(2):
        results = await asyncio.gather(*[
            call('list_papers', {}) for _ in range(8)
        ])
        assert all(r['total'] == 1 for r in results)
    assert opened_threads and max(Counter(opened_threads).values()) == 1
    page = await call('read_page', {'doc_id': doc, 'page': 2})
    assert '31.2' in page['markdown']
    atlas.remove([doc])
    listing = await call('list_papers', {})
    assert listing['total'] == 0


@pytest.mark.asyncio
async def test_stdio_protocol_list_read_error(atlas, bundle):
    doc = atlas.index(bundle, 'lexical')['results'][0]['doc_id']
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'paperatlas', '--data-dir', str(atlas.db.directory), 'mcp'],
        env={**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=30) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert {t.name for t in tools.tools} == {'list_papers', 'search_papers', 'read_page'}
            listing = await client.call_tool('list_papers', {})
            assert not listing.is_error
            page = await client.call_tool('read_page', dict(doc_id=doc, page=2, before=1))
            assert not page.is_error
            assert '31.2' in json.dumps(page.model_dump(), ensure_ascii=False)
            assert 'PromptIR' in json.dumps(page.model_dump(), ensure_ascii=False)
            invalid = await client.call_tool('read_page', dict(doc_id='../source.pdf', page=1))
            assert invalid.is_error
            missing_model = await client.call_tool('search_papers', dict(query='noise'))
            assert missing_model.is_error
