import json
import os
from pathlib import Path
import runpy
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

from conftest import ENGINE
from cyber_agent_flow_eval.scenarioforge import import_config
from cyber_agent_flow_eval.spec import resolve, schedule

ROOT = Path(__file__).resolve().parents[1]


def test_example_schedules_and_controlled_tool_addition(tmp_path):
    smoke = resolve(ROOT / 'examples/01-smoke.yaml')
    comparison = resolve(ROOT / 'examples/02-tools-vs-added.yaml')
    assert len(schedule(smoke)) == 1
    assert len(schedule(comparison)) == 6
    baseline, added = comparison['conditions']
    base_definitions = baseline['catalog_snapshot']['tools']
    added_definitions = added['catalog_snapshot']['tools']
    assert added_definitions[:-1] == base_definitions
    assert added['tools'] == baseline['tools'] + ['http_flag_walk']
    assert baseline['guidance_snapshot'] == added['guidance_snapshot']
    for flag in comparison['tasks'][0]['verifier']['expected'].values():
        assert flag not in comparison['tasks'][0]['prompt']
        assert flag not in json.dumps(added_definitions)
    package = Path(__file__).parent / 'fixtures/scenarioforge_discovery_suite'
    imported = tmp_path / 'study.yaml'
    import_config(package, ROOT / 'examples/03-scenarioforge-runtime.yaml', imported)
    assert len(schedule(resolve(imported))) == 4


@pytest.fixture
def demo_site():
    source = runpy.run_path(str(ROOT / 'examples/http-demo.py'))['SERVER']
    namespace = {'__name__': 'test_fixture'}
    exec(source, namespace)
    handler = namespace['Handler']
    handler.log_message = lambda *args: None
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_example_helper_through_actual_caf_mcp(demo_site, tmp_path):
    # Exercise the exact frozen catalog through the real native MCP server,
    # including command construction and network policy, without a model or VM.
    script = '''
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    env = dict(os.environ, CAF_TOOLS_CONFIG_PATH=sys.argv[2], CAF_RUN_BASE_DIR=sys.argv[4],
               MCP_NETWORK_POLICY=json.dumps({'allow': ['127.0.0.1'], 'disallow': []}))
    params = StdioServerParameters(command=sys.executable, args=[sys.argv[1]], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool('http_flag_walk', {'args': sys.argv[3]})
            print(json.dumps(result.model_dump()))
asyncio.run(main())
'''
    result = subprocess.run([ENGINE['python'], '-c', script,
                             str(Path(ENGINE['path']) / 'mcp_kali.py'),
                             str(ROOT / 'examples/catalogs/with-http-helper.json'), demo_site, str(tmp_path)],
                            cwd=ENGINE['path'], env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert not response.get('isError')
    text = '\n'.join(block.get('text', '') for block in response['content'])
    assert 'FLAG{demo_entry}' in text and 'FLAG{demo_archive}' in text, text
    assert '"pages_read": 4' in text
    assert '"errors": []' in text
