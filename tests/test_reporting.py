import json
from pathlib import Path

import pytest

from cyber_agent_flow_eval import reporting
from cyber_agent_flow_eval.__main__ import main
from cyber_agent_flow_eval.runner import lease
from cyber_agent_flow_eval.storage import write_json


@pytest.fixture
def recorded(tmp_path):
    root = tmp_path / 'runs' / 'study'
    write_json(root / 'manifest.json', {'spec': {'id': 'study', 'conditions': [{'id': 'baseline'}, {'id': 'added'}],
                                                'private': 'EXPECTED_SECRET'},
                                        'spec_hash': 'hash',
                                        'schedule': [{'trial_id': name} for name in ['a', 'b', 'c']]})
    for trial, condition, attempt, status, success, score in [
            ('a', 'baseline', 1, 'timeout', None, None),
            ('a', 'baseline', 2, 'completed', True, 1),
            ('b', 'added', 1, 'completed', False, .5)]:
        folder = root / 'trials' / trial / f'attempt-{attempt:04d}'
        write_json(folder / 'attempt.json', {'experiment_id': 'study', 'trial_id': trial, 'condition_id': condition,
                                            'attempt': attempt, 'status': status, 'verified_success': success, 'score': score})
        (folder / 'worker.log').write_text('first\nsecond\nlast\n')
    (root / 'dataset.jsonl').write_text('deliberately stale cached export')
    return root


def test_results_use_latest_attempts_and_authoritative_records(recorded):
    report = reporting.results(recorded)
    assert len(report['attempts']) == 2
    assert report['attempt_count'] == 3 and report['unstarted_trials'] == 1
    assert report['summary']['success_rate'] == .5
    assert report['summary']['mean_score'] == .75
    assert report['conditions']['baseline']['verified_successes'] == 1
    assert 'EXPECTED_SECRET' not in json.dumps(report)
    all_report = reporting.results(recorded, all_attempts=True)
    assert len(all_report['attempts']) == 3
    assert all_report['summary'] == report['summary']


def test_judge_errors_are_unverified_and_usage_is_reported(recorded):
    passed=recorded/'trials/a/attempt-0002/attempt.json'
    row=json.loads(passed.read_text())
    row.update(judge_enabled=True,judge_passed=True,judge_seconds=2,judge_calls=2)
    write_json(passed,row)
    failed=recorded/'trials/b/attempt-0001/attempt.json'
    row=json.loads(failed.read_text())
    row.update(status='judge_error',verified_success=None,judge_enabled=True,judge_error='Unavailable',judge_seconds=4,judge_calls=1)
    write_json(failed,row)
    summary=reporting.results(recorded)['summary']
    assert summary['verified_trials']==1 and summary['verified_successes']==1
    assert summary['judge_reviews']==2 and summary['judge_errors']==1
    assert summary['mean_judge_seconds']==3 and summary['judge_calls']==3


def test_read_only_status_does_not_create_locks_and_checks_active_coordinator(recorded):
    before = sorted(str(p) for p in recorded.rglob('*'))
    assert reporting.status(recorded)['coordinator_active'] is False
    assert sorted(str(p) for p in recorded.rglob('*')) == before
    with lease(recorded / '.coordinator.lock'):
        assert reporting.status(recorded)['coordinator_active'] is True
    assert reporting.status(recorded)['coordinator_active'] is False


def test_export_preserves_source_and_refuses_active_or_existing_destination(recorded, tmp_path):
    original = (recorded / 'dataset.jsonl').read_bytes()
    destination = tmp_path / 'export'
    with lease(recorded / '.coordinator.lock'):
        with pytest.raises(ValueError, match='Already locked'):
            reporting.export_results(recorded, destination)
    assert not destination.exists()
    result = reporting.export_results(recorded, destination)
    assert result['attempts'] == 2
    assert len((destination / 'dataset.jsonl').read_text().splitlines()) == 2
    assert (recorded / 'dataset.jsonl').read_bytes() == original
    assert not (destination / 'manifest.json').exists()
    assert json.loads((destination / 'summary.json').read_text())['coordinator_active'] is False
    with pytest.raises(FileExistsError):
        reporting.export_results(recorded, destination)
    with pytest.raises(ValueError, match='outside'):
        reporting.export_results(recorded, recorded / 'export')


def test_logs_select_attempt_and_reject_paths_outside_run(recorded, tmp_path):
    logs = reporting.logs(recorded, 'a', lines=1)
    assert logs['attempt'] == 2
    assert list(logs['logs'].values()) == ['last\n']
    with pytest.raises(ValueError, match='No matching'):
        reporting.logs(recorded, '../other')
    outside = tmp_path / 'private.txt'
    outside.write_text('do not expose')
    log = recorded / 'trials/a/attempt-0002/worker.log'
    log.unlink()
    log.symlink_to(outside)
    with pytest.raises(ValueError, match='outside'):
        reporting.logs(recorded, 'a')


def test_listing_includes_corrupt_run_error_without_hiding_valid_runs(recorded):
    bad = recorded.parent / 'bad'
    bad.mkdir()
    (bad / 'manifest.json').write_text('{')
    records = reporting.list_runs(recorded.parent)
    assert len(records) == 2 and 'error' in records[0]
    assert records[1]['experiment_id'] == 'study'


def test_cli_results_export_and_stderr(recorded, tmp_path, capsys):
    assert main(['results', str(recorded), '--all-attempts']) == 0
    assert len(json.loads(capsys.readouterr().out)['attempts']) == 3
    assert main(['export', str(recorded), '--destination', str(tmp_path / 'out'), '--all-attempts']) == 0
    assert json.loads(capsys.readouterr().out)['attempts'] == 3
    assert main(['status', str(tmp_path / 'absent')]) == 2
    captured = capsys.readouterr()
    assert not captured.out and 'No experiment manifest' in captured.err


def test_summary_separates_hint_and_solution_assisted_passes():
    rows = [
        dict(status='completed',verified_success=True,unassisted_success=True),
        dict(status='completed',verified_success=True,assisted_success=True,hints_assisted_success=True,hints_released=1),
        dict(status='completed',verified_success=True,assisted_success=True,solution_assisted_success=True,solution_provided=True,solutions_released=1),
    ]
    summary=reporting.summarize(rows)
    assert summary['verified_successes']==3
    assert summary['unassisted_successes']==1
    assert summary['hints_assisted_successes']==1
    assert summary['solution_assisted_successes']==1
    assert summary['assisted_successes']==2
    assert summary['hints_released']==1 and summary['solutions_released']==1
