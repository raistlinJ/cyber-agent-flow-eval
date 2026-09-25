"""Resolve an external CAF checkout without importing it into the coordinator."""
import ast
import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

REQUIRED_OPTIONS = {'allowed_tools', 'guidance_text', 'reveal_network_policy'}


def resolve_engine(config, base):
    if not isinstance(config, dict) or set(config) - {'path', 'python'} or 'path' not in config:
        raise ValueError('engine requires path and accepts optional python')
    if any(not isinstance(value, str) or not value.strip() for value in config.values()):
        raise ValueError('engine.path and engine.python must be nonempty paths')
    root = (Path(base) / Path(config['path']).expanduser()).resolve()
    for name in ('mcp_client.py', 'mcp_kali.py', 'session_logger.py'):
        if not (root / name).is_file():
            raise ValueError(f'engine.path is not a compatible CAF checkout: missing {name} in {root}')
    # Preserve the executable's symlink path: resolving a venv python to its base
    # binary would silently discard that virtual environment's dependencies.
    executable = os.path.abspath(Path(base) / Path(config.get('python', sys.executable)).expanduser())
    if not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f'engine.python is not an executable file: {executable}')
    try:
        tree = ast.parse((root / 'mcp_client.py').read_text())
        session = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MCPSession')
        init = next(n for n in session.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        options = {arg.arg for arg in [*init.args.args, *init.args.kwonlyargs]}
        if not REQUIRED_OPTIONS <= options:
            raise ValueError('CAF engine lacks evaluation controls; update the configured checkout')
    except (SyntaxError, StopIteration) as exc:
        raise ValueError('Cannot find a compatible MCPSession in engine.path') from exc
    return {'path': str(root), 'python': executable}


def runtime_identity(engine):
    script = '''import json, sys
from importlib.metadata import version, PackageNotFoundError
packages = {}
for name in ['mcp', 'ollama', 'requests', 'PyYAML', 'psutil']:
    try: packages[name] = version(name)
    except PackageNotFoundError: packages[name] = None
print(json.dumps({'python': sys.version, 'executable': sys.executable, 'dependencies': packages}))
'''
    try:
        result = subprocess.run([engine['python'], '-c', script], cwd=engine['path'],
                                capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError(f'Cannot inspect engine.python runtime: {exc}') from exc


def load_session(engine):
    """Worker-only import, with a guard against accidentally loading another CAF."""
    root = Path(engine['path']).resolve()
    sys.path.insert(0, str(root))
    module = importlib.import_module('mcp_client')
    if Path(module.__file__).resolve() != root / 'mcp_client.py':
        raise ValueError('A different CAF engine is already imported; use a fresh worker')
    if not REQUIRED_OPTIONS <= set(inspect.signature(module.MCPSession).parameters):
        raise ValueError('Configured CAF engine lacks required evaluation controls')
    return module.MCPSession
