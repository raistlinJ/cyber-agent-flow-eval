"""Validate and resolve experiment files without starting a model or MCP server."""
import hashlib
import json
import random
import re
from pathlib import Path

import yaml


class StrictLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f'Duplicate YAML key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fields(value, allowed, required, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    unknown, missing = set(value) - set(allowed), set(required) - set(value)
    if unknown or missing:
        raise ValueError(f"{label}: unknown fields {sorted(unknown)}, missing fields {sorted(missing)}")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        raise ValueError(f"Invalid ID: {value!r}")


def positive(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def strings(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(f"{label} must be a list of nonempty strings")


def resolve(path):
    path = Path(path).resolve()
    spec = yaml.load(path.read_text(), Loader=StrictLoader)
    fields(spec, ['version', 'id', 'engine', 'description', 'model', 'execution', 'repetitions', 'order_seed', 'tasks', 'suite', 'conditions'],
           ['version', 'id', 'engine', 'model', 'execution', 'conditions'], 'experiment')
    if type(spec['version']) is not int or spec['version'] != 1:
        raise ValueError('Only schema version 1 is supported')
    identifier(spec['id'])
    from .engine import resolve_engine
    spec['engine'] = resolve_engine(spec['engine'], path.parent)
    if ('tasks' in spec) == ('suite' in spec):
        raise ValueError('Specify either inline tasks or a ScenarioForge suite')
    if 'suite' in spec:
        from .scenarioforge import load_suite
        suite = spec['suite']
        fields(suite, ['path', 'max_readiness_age_seconds'], ['path'], 'suite')
        if not isinstance(suite['path'], str) or not suite['path']:
            raise ValueError('suite.path must be a nonempty path')
        suite.setdefault('max_readiness_age_seconds', 3600)
        positive(suite['max_readiness_age_seconds'], 'max_readiness_age_seconds')
        suite['path'] = str((path.parent / suite['path']).resolve())
        spec['tasks'], spec['suite_snapshot'] = load_suite(suite['path'])
    spec.setdefault('repetitions', 1)
    spec.setdefault('order_seed', 0)
    positive(spec['repetitions'], 'repetitions')
    if type(spec['order_seed']) is not int:
        raise ValueError('order_seed must be an integer')
    model = spec['model']
    fields(model, ['provider', 'url', 'name', 'api_key_env', 'ssl_verify'], ['provider', 'url', 'name'], 'model')
    for key in ['provider', 'url', 'name']:
        if not isinstance(model[key], str) or not model[key]:
            raise ValueError(f'model.{key} must be a nonempty string')
    if model['provider'] not in ['ollama_direct', 'openai', 'litellm', 'claude']:
        raise ValueError('Unsupported provider')
    if 'api_key_env' in model and (not isinstance(model['api_key_env'], str) or not model['api_key_env']):
        raise ValueError('api_key_env must name an environment variable')
    if 'ssl_verify' in model and type(model['ssl_verify']) is not bool:
        raise ValueError('ssl_verify must be boolean')
    execution = spec['execution']
    fields(execution, ['wall_seconds', 'max_turns', 'tool_timeout', 'context_window', 'network_policy', 'target_lock', 'reveal_network_policy'],
           ['wall_seconds', 'max_turns', 'network_policy', 'target_lock'], 'execution')
    execution.setdefault('reveal_network_policy', False)
    if type(execution['reveal_network_policy']) is not bool:
        raise ValueError('reveal_network_policy must be boolean')
    execution.setdefault('tool_timeout', 60)
    execution.setdefault('context_window', 8192)
    for key in ['wall_seconds', 'max_turns', 'tool_timeout', 'context_window']:
        positive(execution[key], key)
    policy = execution['network_policy']
    fields(policy, ['allow', 'disallow'], ['allow', 'disallow'], 'network_policy')
    strings(policy['allow'], 'allow'); strings(policy['disallow'], 'disallow')
    if 'suite_snapshot' in spec:
        from .scenarioforge import require_objectives_in_scope
        require_objectives_in_scope(spec['suite_snapshot'], policy)
    if not isinstance(execution['target_lock'], str) or not execution['target_lock']:
        raise ValueError('target_lock must be a path')
    execution['target_lock'] = str((path.parent / execution['target_lock']).resolve())
    for kind in ['tasks', 'conditions']:
        if not isinstance(spec[kind], list) or not spec[kind]:
            raise ValueError(f'{kind} must be a nonempty list')
        seen = set()
        for item in spec[kind]:
            if kind == 'tasks':
                fields(item, ['id', 'prompt', 'family', 'split', 'scenario_id', 'verifier'],
                       ['id', 'prompt', 'family', 'split', 'scenario_id', 'verifier'], 'task')
                for key in ['prompt', 'family', 'scenario_id']:
                    if not isinstance(item[key], str) or not item[key]:
                        raise ValueError(f'task.{key} must be a nonempty string')
                if item['split'] not in ['development', 'validation', 'test']:
                    raise ValueError('Invalid task split')
                verifier = item['verifier']
                fields(verifier, ['type', 'expected'], ['type', 'expected'], 'verifier')
                if verifier['type'] not in ['json_equals', 'contains_all', 'flags_match', 'flags_found']:
                    raise ValueError('Unsupported verifier')
                if verifier['type'] in {'flags_match', 'flags_found'}:
                    expected = verifier['expected']
                    if not isinstance(expected, dict) or not expected or any(
                            not isinstance(k, str) or not k or not isinstance(v, str) or not v
                            for k, v in expected.items()):
                        raise ValueError('flags_match requires nonempty objective-to-flag strings')
                if verifier['type'] == 'contains_all':
                    strings(verifier['expected'], 'verifier.expected')
                    if not verifier['expected']:
                        raise ValueError('contains_all requires at least one check')
            else:
                fields(item, ['id', 'tools', 'catalog', 'guidance_files'], ['id', 'tools', 'catalog'], 'condition')
                strings(item['tools'], 'tools')
                if len(set(item['tools'])) != len(item['tools']):
                    raise ValueError('Duplicate tool selection')
                catalog = json.loads((path.parent / item.pop('catalog')).read_text())
                if not isinstance(catalog, dict) or not isinstance(catalog.get('tools'), list):
                    raise ValueError('Catalog must contain a tools list')
                item['catalog_snapshot'] = catalog
                strings(item.get('guidance_files', []), 'guidance_files')
                item['guidance_snapshot'] = [
                    {'name': Path(p).name, 'text': (path.parent / p).read_text()}
                    for p in item.pop('guidance_files', [])
                ]
                item['artifact_hash'] = digest({'catalog': catalog, 'guidance': item['guidance_snapshot'], 'tools': item['tools']})
            identifier(item['id'])
            if item['id'] in seen:
                raise ValueError(f'Duplicate {kind} ID: {item["id"]}')
            seen.add(item['id'])
    from .scenarioforge import require_discovery_separation
    require_discovery_separation(spec)
    # Also reject YAML-only values (dates, binary, non-finite floats) in frozen JSON.
    json.dumps(spec, allow_nan=False)
    return spec


def schedule(spec):
    rng = random.Random(spec['order_seed'])
    rows = []
    for task in spec['tasks']:
        for repetition in range(spec['repetitions']):
            conditions = list(spec['conditions'])
            rng.shuffle(conditions)
            for condition in conditions:
                rows.append({'trial_id': f'trial-{len(rows) + 1:06d}', 'task_id': task['id'],
                             'condition_id': condition['id'], 'repetition': repetition,
                             'pair_id': f'{task["id"]}:{repetition}'})
    return rows
