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
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from .spec import digest, resolve, schedule
from .storage import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


class CleanupError(RuntimeError):
    pass


class PreparationError(RuntimeError):
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


class TargetReservation:
    """Hold the same lab lock across preparation and evaluation in one process."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.active = False

    def __enter__(self):
        self._lease = lease(self.path)
        self._lease.__enter__()
        self.active = True
        return self

    def __exit__(self, *args):
        self.active = False
        return self._lease.__exit__(*args)

    def covers(self, path):
        return self.active and self.path == Path(path).resolve()


def source_identity(engine_path):
    # Hash each project's source independently, including dirty checkout contents.
    from .spec import digest
    package = Path(__file__).resolve().parent
    evaluator_files = [*package.glob('*.py')]
    for name in ('pyproject.toml', 'requirements.txt'):
        if (ROOT / name).is_file():
            evaluator_files.append(ROOT / name)
    result = {'evaluator': digest({str(p.relative_to(ROOT)): p.read_text() for p in sorted(evaluator_files)})}
    if engine_path is not None:
        engine = Path(engine_path)
        engine_files = [*engine.glob('*.py'), *engine.glob('requirements*.txt')]
        result['engine'] = digest({str(p.relative_to(engine)): p.read_text() for p in sorted(engine_files)})
    return result


def dependencies():
    result = {}
    for name in ['mcp', 'ollama', 'requests', 'PyYAML', 'psutil']:
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def verify(answer, definition):
    if definition['type'] == 'rubric':
        return {'passed': None, 'checks': [], 'reason': 'Rubric requires evidence-based judge review'}
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


def launch(directory, seconds, hint_controller=None):
    engine = read_json(directory / 'input.json')['engine']
    env = dict(os.environ, CAF_RUN_BASE_DIR=str(directory),
               CAF_TOOLS_CONFIG_PATH=str(directory / 'catalog.json'))
    with (directory / 'worker.log').open('w') as log:
        process = subprocess.Popen([engine['python'], str(Path(__file__).with_name('worker.py')), str(directory)],
                                   cwd=engine['path'], env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + seconds
            while process.poll() is None:
                if hint_controller:
                    hint_controller.poll()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, seconds)
                try:
                    process.wait(timeout=min(.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
            code = process.returncode
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
        from .hints import metrics
        if (path.parent / 'assistance.json').is_file():
            row.update(metrics(path.parent, True, row.get('verified_success')))
            if row.get('guidance_supplied') and row.get('verified_success') is True:
                row.update(unassisted_success=False, assisted_success=True)
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
               'scenario_id', 'scenario_definition_sha256', 'condition_id', 'artifact_hash', 'repetition', 'attempt', 'status',
               'task_outcome', 'execution_status', 'worker_status', 'judge_status', 'assistance_level',
               'criterion_results', 'rubric_hash', 'rubric_version', 'verification_mode', 'reset_seconds',
               'participant_prompt_tokens', 'participant_output_tokens', 'participant_cost_usd',
               'judge_cost_usd', 'participant_usage_complete',
               'verified_success', 'judge_enabled', 'judge_passed', 'judge_score', 'judge_seconds', 'judge_calls', 'judge_prompt_tokens', 'judge_output_tokens', 'judge_execution_trace_reviewed', 'judge_evidence_warning', 'judge_evidence_files', 'deterministic_passed', 'judge_error', 'provide_progressive_hints', 'progressive_hints_available', 'progressive_hints_reason', 'hints_released', 'facts_revealed', 'max_tries_before_solution', 'solutions_released', 'solution_provided', 'retries_requested', 'solution_assisted_success', 'hints_assisted_success', 'assisted_success', 'unassisted_success', 'score', 'suite_id', 'package_hash', 'core_session_id', 'readiness_checked_at',
               'elapsed_seconds', 'execution_seconds', 'flags_observed', 'progress_score', 'time_to_first_flag_seconds', 'attempt_path']
    temporary = output / 'dataset.csv.tmp'
    with temporary.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(output / 'dataset.csv')
    return rows


def run(spec_path, output, *, resume=False, retry_failed=False, launcher=launch, reservation=None, progress=print, prepare_trial=None):
    spec = resolve(spec_path)
    from .backends import create_backend
    from .progress import record_progress
    backend = create_backend(spec)
    if 'suite_snapshot' in spec:
        from .scenarioforge import require_ready
        require_ready(spec['suite_snapshot'], spec['suite']['max_readiness_age_seconds'])
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if reservation is not None and (not isinstance(reservation, TargetReservation) or not reservation.covers(spec['execution']['target_lock'])):
        raise ValueError('A live reservation for this target lock is required')
    target = nullcontext() if reservation is not None else lease(spec['execution']['target_lock'])
    with lease(output / '.coordinator.lock'), target, (backend.lock() if backend else nullcontext()):
        manifest_path = output / 'manifest.json'
        identities = source_identity(None if backend else spec['engine']['path'])
        remote_identity = backend.identities() if backend else None
        if backend:
            identities['engine'] = remote_identity['engine']
        identity = digest(identities)
        from .engine import runtime_identity
        engine_runtime = remote_identity['runtime'] if backend else runtime_identity(spec['engine'])
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
            from .hints import resolve_policy
            manifest = {'progressive_hint_policy': resolve_policy(spec['execution']) if spec['execution'].get('provide_progressive_hints') else None,
                        'version': 1, 'created_at': timestamp(), 'spec_hash': digest(spec),
                        'source_hash': identity, 'source_hashes': identities, 'engine_runtime': engine_runtime,
                        'python': sys.version, 'dependencies': dependencies(),
                        'spec': spec, 'schedule': schedule(spec)}
            write_json(manifest_path, manifest)
        if backend:
            backend.recover(output)
        tasks = {t['id']: t for t in spec['tasks']}
        conditions = {c['id']: c for c in spec['conditions']}
        for trial in manifest['schedule']:
            trial_dir = output / 'trials' / trial['trial_id']
            existing = sorted(trial_dir.glob('attempt-*/attempt.json'))
            if existing:
                previous = read_json(existing[-1])
                if backend:
                    previous.update(record_progress(existing[-1].parent, tasks[trial['task_id']]['verifier'],
                                                    spec['execution']['progress_seconds']))
                    write_json(existing[-1], previous)
                if previous['status'] == 'running':
                    previous.update(status='interrupted', ended_at=timestamp(), verified_success=None)
                    previous.update(record_progress(existing[-1].parent, tasks[trial['task_id']]['verifier'],
                                                    spec['execution']['progress_seconds']))
                    write_json(existing[-1], previous)
                elif not retry_failed or (previous['status'] == 'completed' and previous.get('verified_success') is True):
                    continue
            if 'suite_snapshot' in spec and prepare_trial is None:
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
            if 'orchestration' in spec:
                row['orchestration'] = spec['orchestration']
                if spec['orchestration'].get('scenario_definition_sha256'):
                    row['scenario_definition_sha256'] = spec['orchestration']['scenario_definition_sha256']
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
            participant_execution = {key: value for key, value in spec['execution'].items() if key not in {'target_lock', 'progress_seconds', 'max_tries_before_solution'}}
            hints_enabled = condition.get('provide_progressive_hints', spec['execution'].get('provide_progressive_hints', False))
            participant_execution['provide_progressive_hints'] = hints_enabled
            participant = {'model': spec['model'], 'execution': participant_execution, 'engine': spec['engine'],
                           'prompt': task['prompt'], 'tools': condition['tools'],
                           'guidance': '\n\n'.join(g['text'] for g in condition['guidance_snapshot']),
                           'run_id': f'{trial["trial_id"]}-attempt-{number:04d}',
                           'server_command': shlex.join([spec['engine']['python'], str(Path(spec['engine']['path']) / 'mcp_kali.py')])}
            if task.get('rubric') and 'Challenge requirements:' not in participant['prompt']:
                from .rubric import participant_scaffold
                participant['prompt'] += '\n\n' + participant_scaffold(task['rubric'])
            write_json(directory / 'input.json', participant)
            started = time.monotonic()
            try:
                current_identity = backend.identities() if backend else dict(engine=source_identity(spec['engine']['path'])['engine'], runtime=runtime_identity(spec['engine']))
                if current_identity['engine'] != identities['engine'] or current_identity['runtime'] != engine_runtime:
                    raise PreparationError('CAF engine source or dependencies changed between trials')
                if spec.get('reset'):
                    from .reset import execute as reset_environment
                    audit = reset_environment(spec['reset'], directory)
                    row['reset_seconds'] = audit['seconds']
                if prepare_trial is not None:
                    reset_started = time.monotonic()
                    audit = prepare_trial(directory, trial)
                    if not isinstance(audit, dict) or audit.get('passed') is not True:
                        raise PreparationError('Lab reset/readiness did not pass')
                    row['reset_seconds'] = time.monotonic() - reset_started
                    write_json(directory / 'reset.json', audit)
                if backend:
                    hook_started = time.monotonic()
                    backend.before_trial(directory)
                    row['reset_seconds'] = row.get('reset_seconds', 0) + time.monotonic() - hook_started
                execution_started = time.monotonic()
                from .hints import HintController
                hints = None
                if hints_enabled:
                    metadata = dict(spec.get('suite_snapshot', {}).get('task_metadata', {}).get(task['id'], {}), task_prompt=task['prompt'])
                    hints = HintController(directory, metadata, task['verifier'], spec['execution'])
                    if not hints.available:
                        if progress is not None:
                            progress(f'{trial["trial_id"]}: progressive hints unavailable — {hints.unavailable_reason}')
                        participant['execution']['provide_progressive_hints'] = False
                        write_json(directory / 'input.json', participant)
                        hints = None
                result = (backend.launch if backend else launcher)(directory, spec['execution']['wall_seconds'],
                         **({'hint_controller': hints} if hints else {}))
                row.update(result)
                row['worker_status'] = result['status']
                write_json(directory / 'worker-result.json', result)
                from .evidence_manifest import capture
                capture(directory)
                row.setdefault('execution_seconds', time.monotonic() - execution_started)
                if result['status'] == 'completed' or (task.get('rubric') and result['status'] in {'timeout', 'budget_exceeded'}):
                    evaluation = verify(result['final_answer'], task['verifier'])
                    prefix = 'guest-output/' if backend else ''
                    evaluation['evidence'] = [prefix + 'result.json', prefix + 'messages.json']
                    mode = task.get('verification_mode', 'judge' if task['verifier']['type'] == 'rubric' else 'both' if spec.get('judge', {}).get('enabled') else 'exact')
                    row['verification_mode'] = mode
                    if mode in {'judge', 'both'} and spec.get('judge', {}).get('enabled'):
                        from .judge import judge_trial, JudgeError
                        if progress is not None:
                            progress(f'{trial["trial_id"]}: judge agent reviewing saved trial evidence')
                        deterministic = dict(evaluation)
                        row.update(judge_enabled=True, deterministic_passed=deterministic['passed'])
                        try:
                            verdict = judge_trial(spec['judge'], directory, task, result['final_answer'], deterministic,
                                                  progress=(lambda message: progress(f'{trial["trial_id"]}: judge {message}')) if progress is not None else None)
                            if task.get('rubric'):
                                row.update(rubric_hash=verdict['rubric_hash'],rubric_version=verdict['rubric_version'])
                            row.update(judge_passed=verdict['passed'], judge_score=verdict['score'], judge_reason=verdict['reason'])
                            passed = verdict['passed'] if mode == 'judge' or verdict['passed'] is None else (False if deterministic['passed'] is False else verdict['passed'])
                            evaluation = dict(deterministic=deterministic, judge=verdict,
                                              passed=passed,
                                              score=verdict['score'] if mode == 'judge' else min(deterministic.get('score', 1 if deterministic['passed'] else 0),verdict['score']),
                                              outcome=verdict.get('outcome', 'success' if passed else 'fail') if passed is not False else ('partial' if verdict['score'] > 0 else 'fail'),
                                              criteria=verdict.get('criteria', []), evidence=[*deterministic['evidence'],'judge.json'])
                        except JudgeError as exc:
                            row.update(status='judge_error', judge_error=str(exc), errors=[str(exc)])
                            evaluation = dict(deterministic=deterministic, passed=None, judge_error=str(exc), evidence=['judge.json'])
                        audit = read_json(directory / 'judge.json')
                        row['judge_status'] = audit['status']
                        row.update(judge_seconds=audit['elapsed_seconds'], judge_calls=len(audit['calls']))
                        row.update(judge_execution_trace_reviewed=audit['execution_trace_reviewed'],
                                   judge_evidence_warning=audit['evidence_warning'], judge_evidence_files=audit['evidence_files_read'])
                        for source, field in [('prompt_tokens','judge_prompt_tokens'),('output_tokens','judge_output_tokens')]:
                            values=[call['usage'].get(source) for call in audit['calls']]
                            row[field]=sum(values) if values and all(type(value) is int for value in values) else None
                    write_json(directory / 'evaluation.json', evaluation)
                    row['verified_success'] = evaluation['passed']
                    row['task_outcome'] = evaluation.get('outcome', 'unverified' if evaluation['passed'] is None else 'success' if evaluation['passed'] else 'partial' if evaluation.get('score', 0) > 0 else 'fail')
                    row['criterion_results'] = evaluation.get('criteria', [])
                    if 'score' in evaluation:
                        row['score'] = evaluation['score']
            except CleanupError as exc:
                row.update(status='cleanup_failed', errors=[str(exc)])
                raise
            except PreparationError as exc:
                row.update(status='preparation_failed', errors=[str(exc)])
                raise
            except KeyboardInterrupt:
                row.update(status='interrupted', errors=['Coordinator interrupted'])
                raise
            except Exception as exc:
                row.update(status='error', errors=[f'{type(exc).__name__}: {exc}'])
            finally:
                from .hints import metrics
                row.update(metrics(directory, hints_enabled, row.get('verified_success')))
                from .usage import collect
                row.update(collect(directory, row, spec.get('pricing', {})))
                row.setdefault('task_outcome', 'unverified')
                row['execution_status'] = row.get('worker_status', row['status'])
                row['guidance_supplied'] = bool(condition['guidance_snapshot'])
                row['assistance_level'] = 'solution' if row.get('solutions_released') else 'hints' if row.get('hints_released') or row.get('facts_revealed') or row.get('retries_requested') else 'guidance' if row['guidance_supplied'] else 'none'
                if row['guidance_supplied'] and row.get('verified_success') is True:
                    row['unassisted_success'] = False
                    row['assisted_success'] = True
                row.update(ended_at=timestamp(), elapsed_seconds=time.monotonic() - started)
                row.update(record_progress(directory, task['verifier'], spec['execution']['progress_seconds']))
                write_json(directory / 'attempt.json', row)
                export(output)
            if progress is not None:
                progress(f'{trial["trial_id"]} attempt {number}: {row["status"]}, success={row["verified_success"]}')
        return export(output)
