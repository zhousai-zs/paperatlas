"""Exercise Codex's own MCP client without running a model turn or saving a chat.

Uses a temporary in-memory app-server thread (ephemeral=True), not a new user chat.
No Codex user configuration is written. Test commands exist only for this process.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


def run(codex, data_dir):
    settings = {'paperatlas': dict(command=sys.executable,
        args=['-m', 'paperatlas', '--data-dir', str(Path(data_dir).resolve()), 'mcp'],
        startup_timeout_sec=30, tool_timeout_sec=120,
        env={'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})}
    def toml(value):
        if isinstance(value, dict):
            return '{' + ','.join(f'{k}={toml(v)}' for k, v in value.items()) + '}'
        if isinstance(value, list):
            return '[' + ','.join(toml(v) for v in value) + ']'
        return json.dumps(value)
    command = [codex, 'app-server', '--stdio', '-c', 'mcp_servers=' + toml(settings),
               '-c', 'analytics.enabled=false']
    log_path = Path('.cache/codex-client-stderr.log')
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open('w', encoding='utf-8') as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errors, text=True, encoding='utf-8', cwd=Path(__file__).resolve().parents[1])
        inbox = queue.Queue()
        def reader():
            for line in process.stdout:
                try:
                    inbox.put(json.loads(line))
                except json.JSONDecodeError:
                    pass
        threading.Thread(target=reader, daemon=True).start()
        sequence = 0
        def request(method, params, timeout=180):
            nonlocal sequence
            sequence += 1
            identity = sequence
            process.stdin.write(json.dumps(dict(id=identity, method=method, params=params)) + '\n')
            process.stdin.flush()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                event = inbox.get(timeout=max(.1, deadline-time.monotonic()))
                if event.get('id') == identity:
                    if 'error' in event:
                        raise RuntimeError(event['error'])
                    return event['result']
            raise TimeoutError(method)
        try:
            request('initialize', dict(clientInfo=dict(name='paperatlas_acceptance', version='0.1.0'),
                                       capabilities=dict(experimentalApi=True)))
            process.stdin.write(json.dumps(dict(method='initialized')) + '\n')
            process.stdin.flush()
            thread = request('thread/start', dict(cwd=str(Path.cwd()), ephemeral=True,
                approvalPolicy='never', sandbox='read-only', config={'mcp_servers': settings}))
            thread_id = thread['thread']['id']
            def tool(name, arguments):
                value = request('mcpServer/tool/call', dict(threadId=thread_id, server='paperatlas',
                                                           tool=name, arguments=arguments))
                if value.get('isError'):
                    raise RuntimeError(value)
                return value.get('structuredContent') or json.loads(next(c['text'] for c in value['content'] if c.get('type') == 'text'))
            listing = tool('list_papers', dict(limit=50))
            search = tool('search_papers', dict(query='DPMambaIR DP-SSM fine-grained degradation extractor', limit=3))
            hit = search['results'][0]
            page = tool('read_page', dict(doc_id=hit['doc_id'], page=hit['page']))
            assert hit['excerpt'] in page['markdown']
            return dict(client='codex-cli app-server', ephemeral=True, model_turns=0,
                        papers=listing['total'], search_title=hit['title'], page=hit['page'],
                        list_search_read_passed=True, excerpt_verified=True,
                        user_configuration_modified=False,
                        limitation='Direct tool calls through the actual Codex MCP client; autonomous model planning not tested.')
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--codex', required=True)
    parser.add_argument('--data-dir', default='.paperatlas')
    parser.add_argument('--output', default='reports/codex-client-verification.json')
    args = parser.parse_args()
    output = run(args.codex, args.data_dir)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(output, ensure_ascii=False, indent=2))
