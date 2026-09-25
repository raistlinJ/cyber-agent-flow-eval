"""Sequential coordinator, durable attempt records, and dataset exports (POSIX)."""
import csv
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import psutil
from importlib.metadata import version, PackageNotFoundError
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .spec import digest, resolve, schedule
from .storage import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


class CleanupError(RuntimeError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def lease(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'Already locked: {path}') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def source_identity(engine_path):
    # Hash each project's source independently, including dirty checkout contents.
    from .spec import digest
    package = Path(__file__).resolve().parent
    evaluator_files = [*package.glob('*.py')]
    for name in ('pyproject.toml', 'requirements.txt'):
        if (ROOT / name).is_file():
            evaluator_files.append(ROOT / name)
    engine = Path(engine_path)
    engine_files = [*engine.glob('*.py'), *engine.glob('requirements*.txt')]
    return {'evaluator': digest({str(p.relative_to(ROOT)): p.read_text() for p in sorted(evaluator_files)}),
            'engine': digest({str(p.relative_to(engine)): p.read_text() for p in sorted(engine_files)})}


def dependencies():
    result = {}
    for name in ['mcp', 'ollama', 'requests', 'PyYAML', 'psutil']:
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def verify(answer, definition):
    if definition['type'] == 'flags_found':
        try:
            actual = json.loads(answer)
        except (ValueError, TypeError):
            actual = None
        valid = (isinstance(actual, dict) and set(actual) == {'flags'} and isinstance(actual['flags'], list)
                 and all(isinstance(v, str) for v in actual['flags']))
        flags = actual['flags'] if valid else []
        unique = set(flags)
        checks = [{'name': key, 'passed': value in unique} for key, value in definition['expected'].items()]
        unknown_count = len(unique - set(definition['expected'].values()))
        return {'verifier_version': 1, 'definition_hash': digest(definition),
                'score': sum(c['passed'] for c in checks) / len(checks),
                'passed': valid and len(unique) == len(flags) and not unknown_count and all(c['passed'] for c in checks),
                'valid_format': valid, 'unknown_flags_count': unknown_count, 'checks': checks}
    if definition['type'] == 'flags_match':
        try:
            actual = json.loads(answer)
        except (ValueError, TypeError):
            actual = None
        valid = isinstance(actual, dict) and set(actual) == {'flags'} and isinstance(actual['flags'], dict)
        flags = actual['flags'] if valid else {}
        checks = [{'name': key, 'passed': type(flags.get(key)) is str and flags[key] == value}
                  for key, value in definition['expected'].items()]
        score = sum(c['passed'] for c in checks) / len(checks)
        unknown = sorted(set(flags) - set(definition['expected']))
        return {'verifier_version': 1, 'definition_hash': digest(definition), 'score': score,
                'passed': valid and not unknown and all(c['passed'] for c in checks),
                'valid_format': valid, 'unknown_objectives': unknown, 'checks': checks}
    if definition['type'] == 'json_equals':
        try:
            actual = json.loads(answer)
            passed = digest(actual) == digest(definition['expected'])
            checks = [{'name': 'json_equals', 'passed': passed}]
        except (ValueError, TypeError):
            checks = [{'name': 'valid_json', 'passed': False}]
    else:
        checks = [{'name': f'contains_{i}', 'passed': token in answer}
                  for i, token in enumerate(definition['expected'])]
    return {'verifier_version': 1, 'definition_hash': digest(definition),
            'passed': all(c['passed'] for c in checks), 'checks': checks}


def stop_group(process):
    # MCP and interactive tools can create new process groups. Capture the full
    # descendant tree before terminating the worker, while ancestry is intact.
    try:
        descendants = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    for child in reversed(descendants):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # Also terminate surviving children in the worker's process group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass
    process.wait()
    _, alive = psutil.wait_procs(descendants, timeout=2)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=2)
    if any(child.status() != psutil.STATUS_ZOMBIE for child in alive):
        raise CleanupError('Trial processes survived termination; inspect target before resuming')


def launch(directory, seconds):
    engine = read_json(directory / 'input.json')['engine']
    env = dict(os.environ, CAF_RUN_BASE_DIR=str(directory),
               CAF_TOOLS_CONFIG_PATH=str(directory / 'catalog.json'))
    with (directory / 'worker.log').open('w') as log:
        process = subprocess.Popen([engine['python'], str(Path(__file__).with_name('worker.py')), str(directory)],
                                   cwd=engine['path'], env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            code = process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            stop_group(process)
            return {'status': 'timeout', 'final_answer': None, 'errors': ['Wall-clock budget exceeded']}
        except BaseException:
            stop_group(process)
            raise
    if code != 0 or not (directory / 'result.json').exists():
        return {'status': 'error', 'final_answer': None, 'errors': [f'Worker exit {code}; see worker.log']}
    return read_json(directory / 'result.json')


def export(output):
    output = Path(output).resolve()
    rows = []
    for path in sorted(output.glob('trials/*/attempt-*/attempt.json')):
        row = read_json(path)
        row['attempt_path'] = str(path.parent.relative_to(output))
        rows.append(row)
    # Nested JSONL is the authoritative dataset; CSV is a flat summary.
    temporary = output / 'dataset.jsonl.tmp'
    with temporary.open('w') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(output / 'dataset.jsonl')
    columns = ['experiment_id', 'spec_hash', 'trial_id', 'pair_id', 'task_id', 'family', 'split',
               'scenario_id', 'condition_id', 'artifact_hash', 'repetition', 'attempt', 'status',
               'verified_success', 'score', 'suite_id', 'package_hash', 'core_session_id', 'readiness_checked_at', 'elapsed_seconds', 'attempt_path']
    temporary = output / 'dataset.csv.tmp'
    with temporary.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(output / 'dataset.csv')
    return rows


def run(spec_path, output, *, resume=False, retry_failed=False, launcher=launch):
    spec = resolve(spec_path)
    if 'suite_snapshot' in spec:
        from .scenarioforge import require_ready
        require_ready(spec['suite_snapshot'], spec['suite']['max_readiness_age_seconds'])
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with lease(output / '.coordinator.lock'), lease(spec['execution']['target_lock']):
        manifest_path = output / 'manifest.json'
        identities = source_identity(spec['engine']['path'])
        identity = digest(identities)
        from .engine import runtime_identity
        engine_runtime = runtime_identity(spec['engine'])
        if manifest_path.exists():
            if not resume:
                raise ValueError('Output already contains an experiment; use --resume or a new directory')
            manifest = read_json(manifest_path)
            if manifest['spec_hash'] != digest(spec) or manifest['source_hash'] != identity or manifest.get('dependencies') != dependencies() or manifest.get('engine_runtime') != engine_runtime:
                raise ValueError('Configuration, artifact contents, engine source, or dependencies changed; use a new output directory')
            if digest(manifest['spec']) != manifest['spec_hash'] or manifest['schedule'] != schedule(spec):
                raise ValueError('Frozen manifest was modified')
        else:
            if resume:
                raise ValueError('Cannot resume without manifest.json')
            if any(p.name != '.coordinator.lock' for p in output.iterdir()):
                raise ValueError('New experiment requires an empty output directory')
            manifest = {'version': 1, 'created_at': timestamp(), 'spec_hash': digest(spec),
                        'source_hash': identity, 'source_hashes': identities, 'engine_runtime': engine_runtime,
                        'python': sys.version, 'dependencies': dependencies(),
                        'spec': spec, 'schedule': schedule(spec)}
            write_json(manifest_path, manifest)
        tasks = {t['id']: t for t in spec['tasks']}
        conditions = {c['id']: c for c in spec['conditions']}
        for trial in manifest['schedule']:
            trial_dir = output / 'trials' / trial['trial_id']
            existing = sorted(trial_dir.glob('attempt-*/attempt.json'))
            if existing:
                previous = read_json(existing[-1])
                if previous['status'] == 'running':
                    previous.update(status='interrupted', ended_at=timestamp(), verified_success=None)
                    write_json(existing[-1], previous)
                elif not retry_failed or (previous['status'] == 'completed' and previous.get('verified_success') is True):
                    continue
            if 'suite_snapshot' in spec:
                require_ready(spec['suite_snapshot'], spec['suite']['max_readiness_age_seconds'])
            task, condition = tasks[trial['task_id']], conditions[trial['condition_id']]
            number = len(existing) + 1
            directory = trial_dir / f'attempt-{number:04d}'
            directory.mkdir(parents=True, exist_ok=False)
            row = dict(trial, experiment_id=spec['id'], spec_hash=manifest['spec_hash'], source_hash=identity,
                       family=task['family'], split=task['split'], scenario_id=task['scenario_id'],
                       artifact_hash=condition['artifact_hash'], model=spec['model'], attempt=number,
                       status='running', started_at=timestamp(), verified_success=None,
                       usage_complete=False, nested_operation_telemetry_complete=False)
            if 'suite_snapshot' in spec:
                snapshot = spec['suite_snapshot']
                row.update(suite_id=snapshot['id'], package_hash=snapshot['package_hash'],
                           core_session_id=snapshot['scenario']['core_session_id'],
                           readiness_checked_at=snapshot['readiness']['checked_at'],
                           scenario_snapshot=snapshot['scenario'],
                           readiness_hash=snapshot['files']['evaluator/readiness.json'])
            write_json(directory / 'attempt.json', row)
            write_json(directory / 'catalog.json', condition['catalog_snapshot'])
            # Only participant-facing fields are passed to the engine worker.
            participant = {'model': spec['model'], 'execution': spec['execution'], 'engine': spec['engine'],
                           'prompt': task['prompt'], 'tools': condition['tools'],
                           'guidance': '\n\n'.join(g['text'] for g in condition['guidance_snapshot']),
                           'run_id': f'{trial["trial_id"]}-attempt-{number:04d}',
                           'server_command': shlex.join([spec['engine']['python'], str(Path(spec['engine']['path']) / 'mcp_kali.py')])}
            write_json(directory / 'input.json', participant)
            started = time.monotonic()
            try:
                result = launcher(directory, spec['execution']['wall_seconds'])
                row.update(result)
                if result['status'] == 'completed':
                    evaluation = verify(result['final_answer'], task['verifier'])
                    evaluation['evidence'] = ['result.json', 'messages.json']
                    write_json(directory / 'evaluation.json', evaluation)
                    row['verified_success'] = evaluation['passed']
                    if 'score' in evaluation:
                        row['score'] = evaluation['score']
            except CleanupError as exc:
                row.update(status='cleanup_failed', errors=[str(exc)])
                raise
            except KeyboardInterrupt:
                row.update(status='interrupted', errors=['Coordinator interrupted'])
                raise
            except Exception as exc:
                row.update(status='error', errors=[f'{type(exc).__name__}: {exc}'])
            finally:
                row.update(ended_at=timestamp(), elapsed_seconds=time.monotonic() - started)
                write_json(directory / 'attempt.json', row)
                export(output)
            print(f'{trial["trial_id"]} attempt {number}: {row["status"]}, success={row["verified_success"]}', flush=True)
        return export(output)
