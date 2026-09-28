"""Host workflow integration API; the evaluator has no orchestrator dependency.

These exports let a host workflow reserve a lab, stage guest jobs using the same
transport, import a suite, and run the ordinary standalone evaluation pipeline.
"""
from pathlib import Path

import yaml

from .backends import resolve_backend
from .engine import resolve_engine
from .proxmox import GuestAgent, ProxmoxBackend
from .runner import TargetReservation, export, lease, run, source_identity
from .scenarioforge import import_config, load_suite, require_ready
from .spec import StrictLoader, digest, fields, identifier, positive, resolve, schedule
from .storage import read_json, write_json


def read_runtime(path):
    """Rebase a runtime template, which may omit tasks and suite until import."""
    path = Path(path).resolve()
    spec = yaml.load(path.read_text(), Loader=StrictLoader)
    if not isinstance(spec, dict):
        raise ValueError('Runtime template must be a mapping')
    spec['backend'] = resolve_backend(spec.get('backend', {'type': 'local'}))
    spec['engine'] = resolve_engine(spec.get('engine'), path.parent,
                                    remote=spec['backend']['type'] != 'local')
    spec['execution']['target_lock'] = str((path.parent / spec['execution']['target_lock']).resolve())
    for condition in spec['conditions']:
        condition['catalog'] = str((path.parent / condition['catalog']).resolve())
        condition['guidance_files'] = [str((path.parent / p).resolve()) for p in condition.get('guidance_files', [])]
    return spec


def recover(runtime, output):
    """Stop and collect journaled remote attempts without launching new work."""
    backend = ProxmoxBackend(runtime['backend'], runtime['engine'])
    output = Path(output)
    if not output.exists():
        return
    manifest = output / 'manifest.json'
    if manifest.exists():
        recorded = read_json(manifest)['spec']['backend']
        if recorded['participant_vmid'] != runtime['backend']['participant_vmid']:
            raise ValueError('Recovery participant VM differs from recorded manifest')
    with lease(output / '.coordinator.lock'), backend.lock():
        backend.recover(output)
