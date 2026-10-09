"""Read-only run inspection and explicit result exports, shared by CLI/UI clients.

Attempt records are authoritative. Summaries use the latest attempt for each
trial, so retries do not inflate success rates. No engine or guest calls occur.
"""
from collections import Counter, defaultdict, deque
import csv
import fcntl
import json
import math
from pathlib import Path

from .storage import read_json, write_json


def active(lock):
    """Inspect an existing advisory lock without creating files or trusting PIDs."""
    try:
        stream = Path(lock).open('r')
    except FileNotFoundError:
        return False
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def within(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Path is outside the run directory')
    return path


def manifest(output):
    root = Path(output).resolve()
    if not (root / 'manifest.json').is_file():
        raise ValueError(f'No experiment manifest: {root}')
    return root, read_json(root / 'manifest.json')


def attempts(output, *, all_attempts=False):
    root, _ = manifest(output)
    rows = []
    for path in sorted(root.glob('trials/*/attempt-*/attempt.json')):
        row = read_json(within(root, path))
        from .hints import metrics
        if within(root, path.parent / 'assistance.json').is_file():
            row.update(metrics(path.parent, True, row.get('verified_success')))
        row['attempt_path'] = str(path.parent.relative_to(root))
        rows.append(row)
    rows.sort(key=lambda r: (r['trial_id'], int(r['attempt'])))
    if all_attempts:
        return rows
    latest = {row['trial_id']: row for row in rows}
    return list(latest.values())


def mean(rows, key):
    values = [r[key] for r in rows if type(r.get(key)) in (float, int) and math.isfinite(r[key])]
    return sum(values) / len(values) if values else None


def summarize(rows):
    completed = [r for r in rows if r['status'] == 'completed']
    verified = [r for r in completed if type(r.get('verified_success')) is bool]
    passed = sum(r['verified_success'] for r in verified)
    return {'trials_observed': len(rows), 'status_counts': dict(Counter(r['status'] for r in rows)),
            'verified_trials': len(verified), 'verified_successes': passed,
            'solution_assisted_successes': sum(bool(r.get('solution_assisted_success')) for r in verified),
            'hints_assisted_successes': sum(bool(r.get('hints_assisted_success', r.get('assisted_success') and not r.get('solution_provided'))) for r in verified),
            'solutions_released': sum(r.get('solutions_released', 0) for r in rows),
            'assisted_successes': sum(bool(r.get('assisted_success')) for r in verified),
            'unassisted_successes': sum(bool(r.get('unassisted_success', r['verified_success'] and not (r.get('hints_released') or r.get('solutions_released') or r.get('solution_provided') or r.get('retries_requested') or r.get('assisted_success'))) ) for r in verified),
            'hints_released': sum(r.get('hints_released', 0) for r in rows),
            'facts_revealed': sum(r.get('facts_revealed', 0) for r in rows),
            'success_rate': passed / len(verified) if verified else None,
            'mean_score': mean(completed, 'score'),
            'judge_reviews': sum(bool(row.get('judge_enabled')) for row in rows),
            'judge_errors': sum(row.get('status')=='judge_error' for row in rows),
            'mean_judge_seconds': mean(rows, 'judge_seconds'),
            'judge_calls': sum(row.get('judge_calls',0) for row in rows),
            'mean_execution_seconds': mean(rows, 'execution_seconds'),
            'mean_progress_score': mean(rows, 'progress_score'),
            'mean_time_to_first_flag_seconds': mean(rows, 'time_to_first_flag_seconds')}


def results(output, *, all_attempts=False):
    root, data = manifest(output)
    rows = attempts(root, all_attempts=True)
    latest = list({r['trial_id']: r for r in rows}.values())
    grouped = defaultdict(list)
    for row in latest:
        grouped[row['condition_id']].append(row)
    for condition in data.get('spec', {}).get('conditions', []):
        grouped[condition['id']]
    planned = len(data['schedule'])
    observed = {r['trial_id'] for r in latest}
    return {'output': str(root), 'experiment_id': data['spec']['id'], 'spec_hash': data['spec_hash'],
            'coordinator_active': active(root / '.coordinator.lock'),
            'planned_trials': planned, 'unstarted_trials': sum(t['trial_id'] not in observed for t in data['schedule']),
            'attempt_count': len(rows), 'summary_basis': 'latest attempt per trial',
            'summary': summarize(latest),
            'conditions': {name: summarize(grouped[name]) for name in sorted(grouped)},
            'attempts': rows if all_attempts else latest}


def status(output):
    result = results(output)
    result.pop('attempts')
    return result


def list_runs(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'Run root does not exist: {root}')
    runs = []
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not (child / 'manifest.json').is_file():
            continue
        try:
            runs.append(status(child))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            runs.append({'output': str(child), 'error': str(exc)})
    return runs


def tail(root, path, lines=100):
    if type(lines) is not int or not 1 <= lines <= 10000:
        raise ValueError('lines must be between 1 and 10000')
    path = within(root, path)
    with path.open(errors='replace') as stream:
        return ''.join(deque(stream, maxlen=lines))


def logs(output, trial_id, *, attempt=None, lines=100):
    root, _ = manifest(output)
    matches = [r for r in attempts(root, all_attempts=True) if r['trial_id'] == trial_id
               and (attempt is None or r['attempt'] == attempt)]
    if not matches:
        raise ValueError('No matching trial attempt')
    row = matches[-1]
    folder = within(root, root / row['attempt_path'])
    paths = [folder / 'guest-output/worker.log', folder / 'worker.log']
    paths.extend(sorted(folder.glob('hook-*.log')))
    available = {str(p.relative_to(root)): tail(root, p, lines) for p in paths if p.is_file()}
    return {'trial_id': trial_id, 'attempt': row['attempt'], 'logs': available}


def write_export(directory, report):
    """Write a caller-owned directory; no prompts, verifiers or raw logs added."""
    directory = Path(directory)
    rows = report['attempts']
    write_json(directory / 'summary.json', {k: v for k, v in report.items() if k != 'attempts'})
    with (directory / 'dataset.jsonl').open('x') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    columns = ['experiment_id', 'trial_id', 'condition_id', 'task_id', 'repetition', 'attempt', 'status',
               'verified_success', 'judge_enabled', 'judge_passed', 'judge_score', 'judge_seconds', 'judge_calls', 'judge_prompt_tokens', 'judge_output_tokens', 'judge_execution_trace_reviewed', 'judge_evidence_warning', 'judge_evidence_files', 'deterministic_passed', 'judge_error', 'provide_progressive_hints', 'progressive_hints_available', 'progressive_hints_reason', 'hints_released', 'facts_revealed', 'max_tries_before_solution', 'solutions_released', 'solution_provided', 'retries_requested', 'solution_assisted_success', 'hints_assisted_success', 'assisted_success', 'unassisted_success', 'score', 'execution_seconds', 'elapsed_seconds', 'progress_score',
               'time_to_first_flag_seconds', 'artifact_hash', 'spec_hash', 'attempt_path']
    with (directory / 'dataset.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def export_results(output, destination, *, all_attempts=False):
    from .runner import lease
    root, _ = manifest(output)
    destination = Path(destination).resolve()
    if destination.is_relative_to(root):
        raise ValueError('Export destination must be outside the run directory')
    with lease(root / '.coordinator.lock'):
        report = results(root, all_attempts=all_attempts)
        report['coordinator_active'] = False  # This process holds the inspection lease.
        destination.mkdir(parents=True, exist_ok=False)
        try:
            write_export(destination, report)
        except BaseException:
            import shutil
            shutil.rmtree(destination)
            raise
    return {'destination': str(destination), 'attempts': len(report['attempts']),
            'selection': 'all attempts' if all_attempts else 'latest attempt per trial'}
