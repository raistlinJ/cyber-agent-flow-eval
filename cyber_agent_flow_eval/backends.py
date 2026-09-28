"""Execution backend configuration. Host platforms are explicit extension points."""
from pathlib import PurePosixPath
import re


PLACEHOLDERS = {'macos', 'linux', 'windows'}


def guest_path(value, label):
    if (not isinstance(value, str) or not PurePosixPath(value).is_absolute()
            or '..' in PurePosixPath(value).parts or any(c in value for c in '\0\n\r')):
        raise ValueError(f'{label} requires an absolute guest path')


def resolve_backend(value):
    from .spec import fields, positive, strings
    if not isinstance(value, dict):
        raise ValueError('backend must be a mapping')
    kind = value.get('type')
    if kind in PLACEHOLDERS:
        raise ValueError(f'Backend {kind} is a placeholder; guest integration is not implemented. '
                         'Use local for same-machine execution or proxmox for QEMU guest execution.')
    if kind == 'local':
        fields(value, ['type'], ['type'], 'backend')
        return dict(value)
    if kind != 'proxmox':
        raise ValueError('backend.type must be local, proxmox, macos, linux, or windows')
    allowed = ['type', 'participant_vmid', 'app_vmid', 'workspace', 'user', 'guest_python',
               'environment_file', 'command_timeout', 'poll_seconds', 'max_transfer_bytes', 'before_trial']
    fields(value, allowed, ['type', 'participant_vmid', 'user'], 'backend')
    value = dict(value)
    for key in ('participant_vmid', 'app_vmid'):
        if key in value:
            positive(value[key], f'backend.{key}')
            if not 100 <= value[key] <= 999999999:
                raise ValueError(f'backend.{key} must be a Proxmox VM ID (100–999999999)')
    if value.get('app_vmid') == value['participant_vmid']:
        raise ValueError('app_vmid and participant_vmid must be different')
    if not isinstance(value['user'], str) or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', value['user']):
        raise ValueError('backend.user must name a Linux guest account')
    value.setdefault('workspace', '/var/lib/cyber-agent-flow-eval')
    value.setdefault('guest_python', '/usr/bin/python3')
    value.setdefault('command_timeout', 30)
    value.setdefault('poll_seconds', 1)
    value.setdefault('max_transfer_bytes', 256 * 1024 * 1024)
    for key in ('workspace', 'guest_python', 'environment_file'):
        if key in value:
            guest_path(value[key], f'backend.{key}')
    if value['workspace'] == '/':
        raise ValueError('backend.workspace cannot be /')
    for key in ('command_timeout', 'poll_seconds', 'max_transfer_bytes'):
        positive(value[key], f'backend.{key}')
    hooks = value.setdefault('before_trial', [])
    if not isinstance(hooks, list):
        raise ValueError('backend.before_trial must be a list')
    for hook in hooks:
        fields(hook, ['vmid', 'argv', 'timeout_seconds', 'user', 'cwd', 'environment_file'], ['vmid', 'argv', 'timeout_seconds'], 'before_trial hook')
        positive(hook['vmid'], 'hook.vmid')
        for key in ('cwd', 'environment_file'):
            if key in hook:
                guest_path(hook[key], 'hook.' + key)
        if 'user' in hook and (not isinstance(hook['user'], str) or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', hook['user'])):
            raise ValueError('hook.user must name a Linux guest account')
        positive(hook['timeout_seconds'], 'hook.timeout_seconds')
        strings(hook['argv'], 'hook.argv')
        if not hook['argv'] or not hook['argv'][0].startswith('/'):
            raise ValueError('hook.argv must start with an absolute guest executable')
    return value


def create_backend(spec):
    if spec['backend']['type'] == 'proxmox':
        from .proxmox import ProxmoxBackend
        return ProxmoxBackend(spec['backend'], spec['engine'])
    return None
