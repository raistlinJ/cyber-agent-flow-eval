import pytest
import yaml

from cyber_agent_flow_eval.runner import TargetReservation, run, lease
from cyber_agent_flow_eval.spec import resolve
from test_experiments import specification, successful_worker


def test_reservation_spans_preparation_and_trials(specification, tmp_path):
    spec = resolve(specification)
    with TargetReservation(spec['execution']['target_lock']) as reservation:
        with pytest.raises(ValueError, match='Already locked'):
            with lease(spec['execution']['target_lock']):
                pass
        rows = run(specification, tmp_path / 'output', reservation=reservation, launcher=successful_worker)
        assert all(r['verified_success'] for r in rows)
    with pytest.raises(ValueError, match='live reservation'):
        run(specification, tmp_path / 'other', reservation=reservation, launcher=successful_worker)


def test_wrong_target_reservation_rejected(specification, tmp_path):
    with TargetReservation(tmp_path / 'other.lock') as reservation:
        with pytest.raises(ValueError, match='live reservation'):
            run(specification, tmp_path / 'output', reservation=reservation)


def test_orchestration_metadata_stays_outside_worker(specification, tmp_path):
    raw = yaml.safe_load(specification.read_text())
    raw['orchestration'] = {'workflow_id': 'lab-study', 'workflow_hash': 'a' * 64}
    specification.write_text(yaml.safe_dump(raw))
    def launch(directory, seconds):
        assert 'orchestration' not in (directory / 'input.json').read_text()
        return successful_worker(directory, seconds)
    rows = run(specification, tmp_path / 'output', launcher=launch)
    assert all(r['orchestration'] == raw['orchestration'] for r in rows)


def test_guest_hook_uses_account_directory_environment_and_recoverable_log(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import io
    import pwd
    import os
    from cyber_agent_flow_eval import guest_agent
    calls = []
    @contextmanager
    def control(data):
        yield io.StringIO('')
    monkeypatch.setattr(guest_agent, 'service_control', control)
    monkeypatch.setattr(guest_agent, 'command', lambda argv: calls.append(argv))
    monkeypatch.setattr(guest_agent, 'service_status', lambda unit: {'SubState': 'exited', 'Result': 'success', 'ExecMainStatus': '0'})
    user = pwd.getpwuid(os.getuid()).pw_name
    log = tmp_path / 'recoverable.log'
    result = guest_agent.dispatch(dict(op='hook', unit='test-hook', seconds=10, argv=['/bin/true'],
                                       user=user, cwd='/opt/with%specifier', environment_file='/etc/test.env', log_path=str(log)))
    assert result == {'exitcode': 0, 'log_path': str(log)}
    argv = calls[0]
    assert '--property=User=' + user in argv
    assert '--property=WorkingDirectory=/opt/with%%specifier' in argv
    assert '--property=EnvironmentFile=/etc/test.env' in argv
    assert '--property=RuntimeMaxSec=10' in argv
    assert argv[-2:] == ['--', '/bin/true']
