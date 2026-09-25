"""Read ScenarioForge evaluation packages v1/v2/v3 without importing its application."""
import hashlib
import ipaddress
import json
from datetime import datetime, timezone
from pathlib import Path

FILES = {
    'participant/tasks.json', 'participant/network-policy.json',
    'evaluator/verifiers.json', 'evaluator/task-metadata.json',
    'evaluator/readiness.json', 'evaluator/attack-graph.json', 'evaluator/scenario.xml',
}


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def load_suite(path):
    root = Path(path).resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    if not isinstance(manifest, dict):
        raise ValueError('ScenarioForge manifest must be an object')
    if manifest.get('format') != 'scenarioforge-evaluation' or manifest.get('version') not in {1, 2, 3}:
        raise ValueError('Unsupported ScenarioForge evaluation package format/version')
    original = dict(manifest)
    package_hash = original.pop('package_hash', None)
    if sha256(encoded(original)) != package_hash:
        raise ValueError('ScenarioForge manifest hash mismatch')
    if not isinstance(manifest.get('files'), dict) or set(manifest['files']) != (FILES if manifest['version'] == 1 else FILES - {'participant/network-policy.json'}):
        raise ValueError('ScenarioForge package has missing or unexpected files')
    contents = {}
    for name, expected in manifest['files'].items():
        file = root / name
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            raise ValueError('Package files must remain within the package directory')
        data = file.read_bytes()
        if sha256(data) != expected:
            raise ValueError(f'ScenarioForge file hash mismatch: {name}')
        if name.endswith('.json'):
            contents[name] = json.loads(data)
    scenario = manifest.get('scenario')
    if not isinstance(scenario, dict) or set(scenario) != {
            'id', 'name', 'xml_sha256', 'graph_sha256', 'core_session_id', 'core_host'}:
        raise ValueError('Invalid scenario identity fields')
    if scenario.get('xml_sha256') != manifest['files']['evaluator/scenario.xml']:
        raise ValueError('Scenario XML identity mismatch')
    if scenario.get('graph_sha256') != manifest['files']['evaluator/attack-graph.json']:
        raise ValueError('Attack graph identity mismatch')
    graph = contents['evaluator/attack-graph.json']
    if not isinstance(graph, dict):
        raise ValueError('Attack graph must be an object')
    if graph.get('schema_version') != 2 or graph.get('scenario') != scenario.get('name'):
        raise ValueError('Attack graph scenario mismatch')
    public = contents['participant/tasks.json']
    verifiers = contents['evaluator/verifiers.json']
    metadata = contents['evaluator/task-metadata.json']
    if not isinstance(verifiers, dict) or not isinstance(metadata, dict):
        raise ValueError('Evaluator definitions must be objects keyed by task ID')
    if not isinstance(contents['evaluator/readiness.json'], dict):
        raise ValueError('Readiness report must be an object')
    if not isinstance(public, list) or not public:
        raise ValueError('Suite has no participant tasks')
    tasks = []
    for task in public:
        if not isinstance(task, dict) or set(task) != {'id', 'family', 'split', 'scenario_id', 'prompt'}:
            raise ValueError('Invalid participant task fields')
        if task['scenario_id'] != scenario['id']:
            raise ValueError('Task scenario mismatch')
        if task['id'] not in verifiers or task['id'] not in metadata:
            raise ValueError('Task lacks evaluator definitions')
        if not isinstance(metadata[task['id']], dict):
            raise ValueError('Task metadata must be an object')
        requirements = metadata[task['id']].get('required_checks')
        if not isinstance(requirements, list) or not requirements or any(not isinstance(v, str) or not v for v in requirements):
            raise ValueError('Task lacks required readiness checks')
        tasks.append(dict(task, verifier=verifiers[task['id']]))
    ids = [t['id'] for t in tasks]
    if len(set(ids)) != len(ids) or set(ids) != set(verifiers) or set(ids) != set(metadata):
        raise ValueError('Participant/evaluator task IDs do not match uniquely')
    snapshot = {'id': manifest['id'], 'package_hash': package_hash, 'scenario': scenario,
                'files': manifest['files'], 'task_metadata': metadata,
                'readiness': contents['evaluator/readiness.json'], 'graph': graph}
    return tasks, snapshot


def require_ready(snapshot, max_age_seconds, *, now=None):
    """Historical readiness gate; this does not probe/reset the remote environment."""
    report, scenario = snapshot['readiness'], snapshot['scenario']
    if report.get('status') != 'complete' or report.get('ok') is not True or report.get('overall') != 'pass':
        raise ValueError('ScenarioForge readiness is missing, incomplete, or not passing; rerun check-artifacts and export')
    if report.get('session_confirmed') is not True:
        raise ValueError('ScenarioForge readiness did not confirm a live CORE session')
    if (report.get('xml_sha256') != scenario['xml_sha256'] or report.get('scenario') != scenario['name']
            or report.get('session_id') != scenario['core_session_id']
            or report.get('core_host') != scenario['core_host']
            or not scenario.get('core_host') or type(scenario.get('core_session_id')) is not int):
        raise ValueError('Readiness does not identify the frozen scenario/deployment')
    try:
        checked = datetime.fromisoformat(report['checked_at'])
        if checked.tzinfo is None:
            raise ValueError('Missing timestamp timezone')
        age = ((now or datetime.now(timezone.utc)) - checked).total_seconds()
    except (KeyError, TypeError, ValueError):
        raise ValueError('Readiness requires a timestamp with timezone') from None
    if age < -60 or age > max_age_seconds:
        raise ValueError('Readiness evidence is stale or future-dated; rerun checks and export a new package')
    rows = report.get('checks')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Readiness contains no checks')
    checks = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('key'), str) or row['key'] in checks:
            raise ValueError('Readiness check IDs must be unique')
        if row.get('status') not in {'pass', 'skip'}:
            raise ValueError('Readiness contains a failed, warning, or unfinished check')
        checks[row['key']] = row['status']
        for item in row.get('items', []):
            if isinstance(item, dict) and item.get('status') not in {None, 'pass', 'skip'}:
                raise ValueError('Readiness contains an unsuccessful detail check')
    for definition in snapshot['task_metadata'].values():
        if any(checks.get(key) != 'pass' for key in definition['required_checks']):
            raise ValueError('A task prerequisite check is missing, skipped, or failed')


def import_config(package, template, output, max_age_seconds=3600):
    """Create a YAML referencing the frozen suite and rebased runtime conditions."""
    import os
    import tempfile
    import yaml
    from .spec import StrictLoader, identifier, positive, resolve

    package, template, output = Path(package).resolve(), Path(template).resolve(), Path(output).resolve()
    tasks, snapshot = load_suite(package)
    positive(max_age_seconds, 'max_readiness_age_seconds')
    spec = yaml.load(template.read_text(), Loader=StrictLoader)
    if not isinstance(spec, dict):
        raise ValueError('Configuration template must be a mapping')
    from .engine import resolve_engine
    spec['engine'] = resolve_engine(spec.get('engine'), template.parent)
    identifier(snapshot['id'])
    spec['id'] = snapshot['id']
    spec.pop('tasks', None)
    spec['suite'] = {'path': str(package), 'max_readiness_age_seconds': max_age_seconds}
    for condition in spec['conditions']:
        condition['catalog'] = str((template.parent / condition['catalog']).resolve())
        if 'guidance_files' in condition:
            condition['guidance_files'] = [str((template.parent / p).resolve()) for p in condition['guidance_files']]
    spec['execution']['target_lock'] = str((template.parent / spec['execution']['target_lock']).resolve())
    if output.exists():
        raise ValueError('Output configuration already exists; choose a new file')
    output.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(spec, sort_keys=False)
    fd, temporary = tempfile.mkstemp(prefix='.suite-', suffix='.yaml', dir=output.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        resolve(temporary)
        with output.open('x') as stream:
            stream.write(content)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {'output': str(output), 'tasks': len(tasks), 'package_hash': snapshot['package_hash']}


def require_objectives_in_scope(snapshot, policy):
    """Validate known objective IPs against CAF policy; never modify that policy.

    This is a prerequisite check, not proof that every intermediate route is usable.
    Custom tasks without source nodes have no machine-checkable target inventory.
    """
    networks = {}
    for key in ('allow', 'disallow'):
        networks[key] = [ipaddress.ip_network(v, strict=False) for v in policy[key] if v != '*']
    def matches(address, key):
        return '*' in policy[key] or any(address in net for net in networks[key])
    nodes = {str(n['id']): n for n in snapshot['graph'].get('nodes', [])}
    for task, metadata in snapshot['task_metadata'].items():
        for ref in metadata.get('source_nodes', []):
            node = nodes.get(str(ref), {})
            try:
                address = ipaddress.ip_address(node.get('ipv4', ''))
            except ValueError:
                raise ValueError(f'Task {task} objective {ref} lacks a resolved target address') from None
            if matches(address, 'disallow') or not matches(address, 'allow'):
                raise ValueError(f'Task {task} objective {ref} ({address}) conflicts with CAF network scope; '
                                 'review the task or CAF policy explicitly')


def require_discovery_separation(spec):
    """Guard authored discovery facts against direct briefing/artifact disclosure."""
    import re
    metadata = spec.get('suite_snapshot', {}).get('task_metadata', {})
    discovery = [m for m in metadata.values() if m.get('discovery')]
    if not discovery:
        return
    if spec['execution']['reveal_network_policy']:
        raise ValueError('Discovery evaluation requires reveal_network_policy: false')
    public = json.dumps({'tasks': [{k: v for k, v in t.items() if k != 'verifier'} for t in spec['tasks']],
                         'conditions': spec['conditions']})
    for definition in discovery:
        for fact in definition.get('discoverable_facts', []):
            if fact['value'] in public:
                raise ValueError('Discoverable fact appears in participant tasks or condition artifacts')
            if fact['artifact'] == 'InternalNetwork(subnet)':
                network = ipaddress.ip_network(fact['value'], strict=False)
                for token in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', public):
                    try:
                        address = ipaddress.ip_address(token)
                    except ValueError:
                        continue
                    if address in network:
                        raise ValueError('Hidden subnet address appears in participant tasks or condition artifacts')
