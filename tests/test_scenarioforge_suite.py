import copy
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from conftest import CAF_ROOT, ENGINE

from cyber_agent_flow_eval.scenarioforge import encoded, import_config, load_suite, require_ready, sha256
from cyber_agent_flow_eval.spec import resolve
from cyber_agent_flow_eval.runner import run, verify
from cyber_agent_flow_eval.storage import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/scenarioforge_suite'


def rehash(package):
    manifest = read_json(package / 'manifest.json')
    for name in manifest['files']:
        manifest['files'][name] = sha256((package / name).read_bytes())
    manifest['scenario']['xml_sha256'] = manifest['files']['evaluator/scenario.xml']
    manifest['scenario']['graph_sha256'] = manifest['files']['evaluator/attack-graph.json']
    manifest.pop('package_hash')
    manifest['package_hash'] = sha256(encoded(manifest))
    (package / 'manifest.json').write_bytes(encoded(manifest))


@pytest.fixture
def runtime_template(tmp_path):
    raw = yaml.safe_load((ROOT / 'configs/experiments/example.yaml').read_text())
    raw['engine'] = dict(ENGINE)
    raw['execution']['network_policy'] = {'allow': ['10.77.0.0/24'], 'disallow': []}
    for condition in raw['conditions']:
        condition['catalog'] = str((ROOT / 'configs/experiments' / condition['catalog']).resolve())
        condition['guidance_files'] = [str((ROOT / 'configs/experiments' / p).resolve())
                                       for p in condition.get('guidance_files', [])]
    path = tmp_path / 'runtime.yaml'
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.fixture
def package(tmp_path):
    root = tmp_path / 'suite'
    shutil.copytree(FIXTURE, root)
    manifest = read_json(root / 'manifest.json')
    manifest['scenario'].update(core_session_id=9, core_host='fixture-only.invalid')
    (root / 'manifest.json').write_bytes(encoded(manifest))
    report = {'status': 'complete', 'ok': True, 'overall': 'pass', 'scenario': 'Evaluation fixture',
              'session_id': 9, 'core_host': 'fixture-only.invalid', 'session_confirmed': True,
              'xml_sha256': manifest['scenario']['xml_sha256'],
              'checked_at': datetime.now(timezone.utc).isoformat(),
              'checks': [{'key': key, 'status': 'pass', 'items': []}
                         for key in ['containers', 'services', 'ports', 'injects']]}
    (root / 'evaluator/readiness.json').write_bytes(encoded(report))
    rehash(root)
    return root


def test_real_exporter_fixture_loads_as_draft():
    tasks, snapshot = load_suite(FIXTURE)
    assert len(tasks) == 1
    assert tasks[0]['verifier']['expected'] == {'entry': 'FLAG{fixture-entry}', 'target': 'FLAG{fixture-target}'}
    assert 'FLAG{' not in tasks[0]['prompt']
    with pytest.raises(ValueError, match='readiness'):
        require_ready(snapshot, 3600)


def test_import_rebases_and_run_records_provenance_without_answer_leakage(package, tmp_path, runtime_template):
    config = tmp_path / 'configs' / 'imported.yaml'
    import_config(package, runtime_template, config)
    spec = resolve(config)
    assert len(spec['tasks']) == 1
    assert spec['conditions'][0]['tools'] == []
    assert spec['execution']['network_policy'] == yaml.safe_load(runtime_template.read_text())['execution']['network_policy']
    # Avoid sharing the checked-in example's lock during tests.
    raw = yaml.safe_load(config.read_text())
    raw['execution']['target_lock'] = str(tmp_path / 'target.lock')
    config.write_text(yaml.safe_dump(raw))
    def worker(directory, seconds):
        participant = read_json(directory / 'input.json')
        serialized = json.dumps(participant)
        for hidden in ['FLAG{fixture-entry}', 'FLAG{fixture-target}', 'verifier', 'attack-graph', 'readiness']:
            assert hidden not in serialized
        result = {'status': 'completed', 'final_answer': '{"flags":{"entry":"FLAG{fixture-entry}"}}'}
        write_json(directory / 'result.json', result)
        write_json(directory / 'messages.json', [])
        return result
    rows = run(config, tmp_path / 'output', launcher=worker)
    assert len(rows) == 4
    assert all(r['score'] == 0.5 and r['verified_success'] is False for r in rows)
    assert all(r['package_hash'] == spec['suite_snapshot']['package_hash'] and r['core_session_id'] == 9 for r in rows)
    evaluation = read_json(tmp_path / 'output' / rows[0]['attempt_path'] / 'evaluation.json')
    assert [c['passed'] for c in evaluation['checks']] == [True, False]
    assert 'FLAG{' not in json.dumps(evaluation)
    with pytest.raises(ValueError, match='already exists'):
        import_config(package, config, config)


@pytest.mark.parametrize('change', ['hash', 'missing', 'symlink', 'manifest', 'scope'])
def test_reject_invalid_package_or_scope(package, tmp_path, change, runtime_template):
    if change == 'hash':
        with (package / 'participant/tasks.json').open('a') as stream:
            stream.write(' ')
    elif change == 'missing':
        (package / 'evaluator/verifiers.json').unlink()
    elif change == 'symlink':
        contents = (package / 'evaluator/verifiers.json').read_text()
        external = tmp_path / 'outside.json'
        external.write_text(contents)
        (package / 'evaluator/verifiers.json').unlink()
        (package / 'evaluator/verifiers.json').symlink_to(external)
    elif change == 'manifest':
        data = read_json(package / 'manifest.json')
        data['id'] = 'tampered'
        write_json(package / 'manifest.json', data)
    else:
        config = tmp_path / 'config.yaml'
        import_config(package, runtime_template, config)
        raw = yaml.safe_load(config.read_text())
        raw['execution']['network_policy']['disallow'] = ['10.77.0.20/32']
        config.write_text(yaml.safe_dump(raw))
        with pytest.raises(ValueError, match='scope'):
            resolve(config)
        return
    with pytest.raises((ValueError, OSError)):
        load_suite(package)


@pytest.mark.parametrize('change', ['stale', 'future', 'failed', 'skip', 'missing', 'session', 'xml', 'unconfirmed', 'details', 'duplicate'])
def test_readiness_failures_block_before_launch(package, tmp_path, change, runtime_template):
    report = read_json(package / 'evaluator/readiness.json')
    if change in {'stale', 'future'}:
        report['checked_at'] = (datetime.now(timezone.utc) + timedelta(hours=-2 if change == 'stale' else 2)).isoformat()
    elif change == 'failed':
        report['overall'] = 'fail'
    elif change == 'skip':
        report['checks'][0]['status'] = 'skip'
    elif change == 'missing':
        report['checks'].pop()
    elif change == 'session':
        report['session_id'] = 10
    elif change == 'xml':
        report['xml_sha256'] = 'wrong'
    elif change == 'unconfirmed':
        report['session_confirmed'] = False
    elif change == 'details':
        report['checks'][0]['items'] = [{'status': 'fail'}]
    else:
        report['checks'].append(copy.deepcopy(report['checks'][0]))
    (package / 'evaluator/readiness.json').write_bytes(encoded(report))
    rehash(package)
    config = tmp_path / 'config.yaml'
    import_config(package, runtime_template, config)
    with pytest.raises(ValueError):
        run(config, tmp_path / 'output', launcher=lambda *a: pytest.fail('Worker launched with invalid readiness'))
    assert not (tmp_path / 'output').exists()


def test_flags_exact_objective_scoring():
    verifier = {'type': 'flags_match', 'expected': {'first': 'FLAG{one}', 'second': 'FLAG{two}'}}
    result = verify('{"flags":{"second":"FLAG{two}","first":"FLAG{one}"}}', verifier)
    assert result['passed'] is True and result['score'] == 1
    for answer in ['FLAG{one} FLAG{two}', '[]', '{"flags":[]}', '{"flags":{"first":"prefix FLAG{one}"}}']:
        result = verify(answer, verifier)
        assert result['passed'] is False and result['score'] == 0
    assert not verify('{"flags":{"first":"FLAG{one}","second":"FLAG{two}","extra":"x"}}', verifier)['passed']


@pytest.mark.parametrize('version', [1, 2])
def test_import_preserves_caf_policy_and_checks_objectives(package, tmp_path, version):
    if version == 2:
        manifest = read_json(package / 'manifest.json')
        manifest['version'] = 2
        del manifest['files']['participant/network-policy.json']
        (package / 'participant/network-policy.json').unlink()
        write_json(package / 'manifest.json', manifest)
        rehash(package)
    raw = yaml.safe_load((ROOT / 'configs/experiments/example.yaml').read_text())
    raw['engine'] = dict(ENGINE)
    # Absolute template references keep the fixture independent of its location.
    for condition in raw['conditions']:
        condition['catalog'] = str((ROOT / 'configs/experiments' / condition['catalog']).resolve())
        condition['guidance_files'] = [str((ROOT / 'configs/experiments' / p).resolve())
                                       for p in condition.get('guidance_files', [])]
    policy = {'allow': ['10.77.0.0/24', '10.78.0.0/24'], 'disallow': ['10.77.0.1/32']}
    raw['execution']['network_policy'] = policy
    template = tmp_path / 'template.yaml'
    template.write_text(yaml.safe_dump(raw))
    output = tmp_path / 'imported.yaml'
    import_config(package, template, output)
    assert resolve(output)['execution']['network_policy'] == policy
    raw['execution']['network_policy']['disallow'].append('10.77.0.20/32')
    template.write_text(yaml.safe_dump(raw))
    rejected = tmp_path / 'rejected.yaml'
    with pytest.raises(ValueError, match='conflicts with CAF network scope'):
        import_config(package, template, rejected)
    assert not rejected.exists()


def test_real_discovery_export_preserves_scope_and_rejects_leaking_guidance(runtime_template, tmp_path):
    fixture = ROOT / 'tests/fixtures/scenarioforge_discovery_suite'
    tasks, snapshot = load_suite(fixture)
    assert tasks[0]['verifier']['type'] == 'flags_found'
    assert '10.78.' not in tasks[0]['prompt']
    assert '10.77.0.0/24' in tasks[0]['prompt']
    assert snapshot['task_metadata']['discover-and-recover']['discovery']
    raw = yaml.safe_load(runtime_template.read_text())
    raw['execution']['network_policy']['allow'].append('10.78.0.0/24')
    runtime_template.write_text(yaml.safe_dump(raw))
    output = tmp_path / 'discovery.yaml'
    import_config(fixture, runtime_template, output)
    spec = resolve(output)
    assert spec['execution']['network_policy'] == raw['execution']['network_policy']
    assert spec['execution']['reveal_network_policy'] is False
    imported = yaml.safe_load(output.read_text())
    imported['execution']['reveal_network_policy'] = True
    output.write_text(yaml.safe_dump(imported))
    with pytest.raises(ValueError, match='reveal_network_policy'):
        resolve(output)
    imported['execution']['reveal_network_policy'] = False
    guide = tmp_path / 'leaking.md'
    guide.write_text('Try scanning 10.78.0.20')
    imported['conditions'][0]['guidance_files'] = [str(guide)]
    output.write_text(yaml.safe_dump(imported))
    with pytest.raises(ValueError, match='Hidden subnet'):
        resolve(output)


def test_import_rebases_relative_engine_checkout_and_python(package, runtime_template, tmp_path):
    import os
    raw = yaml.safe_load(runtime_template.read_text())
    raw['engine'] = {key: os.path.relpath(value, runtime_template.parent) for key, value in ENGINE.items()}
    runtime_template.write_text(yaml.safe_dump(raw))
    output = tmp_path / 'elsewhere/nested/imported.yaml'
    import_config(package, runtime_template, output)
    assert resolve(output)['engine'] == ENGINE
    assert yaml.safe_load(output.read_text())['engine'] == ENGINE
