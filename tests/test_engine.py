import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from cyber_agent_flow_eval.engine import resolve_engine
from cyber_agent_flow_eval.runner import run
from cyber_agent_flow_eval.spec import resolve
from test_experiments import specification, successful_worker

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine_copy(tmp_path):
    root = tmp_path / 'configured engine'
    root.mkdir()
    (root / 'mcp_client.py').write_text('class MCPSession:\n    def __init__(self, *, allowed_tools=None, guidance_text=None, reveal_network_policy=True): pass\n')
    (root / 'mcp_kali.py').write_text('# test engine\n')
    (root / 'session_logger.py').write_text('# test logger\n')
    return root


def test_engine_validation_preserves_python_symlink(engine_copy, tmp_path):
    link = tmp_path / 'selected-python'
    link.symlink_to(sys.executable)
    result = resolve_engine({'path': 'configured engine', 'python': 'selected-python'}, tmp_path)
    assert result == {'path': str(engine_copy), 'python': str(link)}
    (engine_copy / 'mcp_client.py').write_text('class MCPSession:\n    def __init__(self): pass\n')
    with pytest.raises(ValueError, match='evaluation controls') as failure:
        resolve_engine({'path': str(engine_copy)}, tmp_path)
    assert str(engine_copy) in str(failure.value)
    assert 'allowed_tools, guidance_text, reveal_network_policy' in str(failure.value)


@pytest.mark.parametrize('options,missing', [
    ('', 'allowed_tools, guidance_text, reveal_network_policy'),
    (', *, allowed_tools=None, guidance_text=None', 'reveal_network_policy'),
    (', *, allowed_tools=None, guidance_text=None, reveal_network_policy=True', None),
])
def test_guest_probe_reports_configured_path_and_missing_controls(engine_copy, monkeypatch, options, missing):
    from cyber_agent_flow_eval import guest_agent
    # Probe must parse the actual guest source, not import it or inspect host CAF.
    (engine_copy / 'mcp_client.py').write_text(
        f'class MCPSession:\n    def __init__(self{options}): pass\n'
        'raise RuntimeError("Engine must not be imported")\n')
    def command(argv, **kwargs):
        assert missing is None, 'Incompatible engine must fail before runtime/guest commands'
        return '250' if argv[0] == 'systemctl' else '{"dependencies": {}}'
    monkeypatch.setattr(guest_agent, 'command', command)
    data = {'op': 'probe', 'engine': {'path': str(engine_copy), 'python': sys.executable}}
    if missing:
        with pytest.raises(ValueError, match='evaluation controls') as failure:
            guest_agent.dispatch(data)
        message = str(failure.value)
        assert f'guest engine.path={engine_copy}' in message
        assert f'is missing {missing}.' in message
        assert 'inside the participant VM' in message
    else:
        assert len(guest_agent.dispatch(data)['engine']) == 64


@pytest.mark.parametrize('settings', [{}, {'path': ''}, {'path': 'missing'}, {'path': 'configured engine', 'python': 'absent'},
                                     {'path': 'configured engine', 'unknown': True}])
def test_bad_engine_config_fails_early(engine_copy, tmp_path, settings):
    with pytest.raises(ValueError):
        resolve_engine(settings, tmp_path)


def test_engine_source_change_blocks_resume(specification, engine_copy, tmp_path):
    raw = yaml.safe_load(specification.read_text())
    raw['engine'] = {'path': str(engine_copy), 'python': sys.executable}
    specification.write_text(yaml.safe_dump(raw))
    output = tmp_path / 'results'
    run(specification, output, launcher=successful_worker)
    manifest = json.loads((output / 'manifest.json').read_text())
    assert set(manifest['source_hashes']) == {'engine', 'evaluator'}
    assert manifest['engine_runtime']['python']
    (engine_copy / 'session_logger.py').write_text('# changed engine\n')
    with pytest.raises(ValueError, match='engine source'):
        run(specification, output, resume=True, launcher=lambda *args: pytest.fail('Changed engine launched'))


def test_cli_plan_works_outside_checkouts_without_importing_engine(specification, engine_copy, tmp_path):
    # Importing this source would fail; planning only parses its compatibility signature.
    client = engine_copy / 'mcp_client.py'
    client.write_text(client.read_text() + "\nraise RuntimeError('Coordinator imported engine')\n")
    raw = yaml.safe_load(specification.read_text())
    raw['engine'] = {'path': str(engine_copy)}
    specification.write_text(yaml.safe_dump(raw))
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run([sys.executable, '-m', 'cyber_agent_flow_eval', 'plan', str(specification)],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)['schedule']) == 2
