"""Integration tests use a configurable CAF checkout, never a vendored engine."""
import os
import sys
from pathlib import Path

CAF_ROOT = Path(os.environ.get('CAF_TEST_ENGINE_DIR', str(Path(__file__).resolve().parents[2] / 'cyber-agent-flow'))).resolve()
default_python = CAF_ROOT / 'venv/bin/python'
ENGINE = {'path': str(CAF_ROOT), 'python': os.environ.get('CAF_TEST_ENGINE_PYTHON', str(default_python) if default_python.is_file() else sys.executable)}
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
