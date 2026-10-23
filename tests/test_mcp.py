import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
