"""Integration tests use a configurable CAF checkout, never a vendored engine."""
import os
import sys
import pytest
from pathlib import Path

CAF_ROOT = Path(os.environ.get('CAF_TEST_ENGINE_DIR', str(Path(__file__).resolve().parents[2] / 'cyber-agent-flow'))).resolve()
default_python = CAF_ROOT / 'venv/bin/python'
ENGINE = {'path': str(CAF_ROOT), 'python': os.environ.get('CAF_TEST_ENGINE_PYTHON', str(default_python) if default_python.is_file() else sys.executable)}
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def guest_maintenance_paths(tmp_path, monkeypatch):
    from cyber_agent_flow_eval import guest_agent
    monkeypatch.setattr(guest_agent, 'MAINTENANCE_LOCK', tmp_path / 'maintenance.lock')
    monkeypatch.setattr(guest_agent, 'MAINTENANCE_PENDING', tmp_path / 'maintenance.pending')
