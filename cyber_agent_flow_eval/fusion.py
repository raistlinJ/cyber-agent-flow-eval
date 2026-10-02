"""Fusion guest RPC through VMware Tools; shares the bounded Linux worker."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from .proxmox import GuestAgent, ProxmoxBackend

DEFAULT_VMRUN = '/Applications/VMware Fusion.app/Contents/Library/vmrun'


def inventory(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Fusion inventory must be a regular owner-only file (chmod 600)')
    data = json.loads(path.read_text())
    if data.get('version') != 1 or not isinstance(data.get('vms'), dict) or not data['vms']:
        raise ValueError('Invalid Fusion inventory')
    seen = set()
    for key, row in data['vms'].items():
        if not key.isdecimal() or not 100 <= int(key) <= 999999999:
            raise ValueError('Invalid Fusion VM identity')
        vmx = Path(row['vmx'])
        if not vmx.is_absolute() or vmx.suffix != '.vmx' or not vmx.is_file():
            raise ValueError('Fusion VMX must identify an existing absolute .vmx file')
        if vmx.resolve() in seen:
            raise ValueError('Duplicate Fusion VMX')
        seen.add(vmx.resolve())
        if not isinstance(row.get('username'), str) or not row['username'] or not isinstance(row.get('password'), str) or not row['password'] or '\n' in row['password']:
            raise ValueError('Fusion guest login credentials are required')
    return data['vms']


def vm_lock_path(config, vmid):
    if config.get('type') != 'fusion':
        return Path('/var/lock') / f'cyber-agent-flow-eval-vm-{vmid}.lock'
    import hashlib
    row = inventory(config['inventory_file'])[str(vmid)]
    key = hashlib.sha256(str(Path(row['vmx']).resolve()).encode()).hexdigest()
    return Path.home()/'.cache/cyber-agent-flow/locks'/f'fusion-{key}.lock'


class FusionGuestAgent(GuestAgent):
    def qm(self, args, *, fusion_status=None, **kwargs):
        # Existing host observers wrap this method. Fusion publishes logical RPC
        # status here so stage output streaming works without invoking qm.
        if fusion_status is None:
            raise ValueError('Direct Proxmox commands are unavailable for Fusion')
        return fusion_status

    def _run(self, vmid, operation, *args, timeout=None):
        if self.authorize:
            self.authorize(['guest', 'exec', vmid])
        row = inventory(self.config['inventory_file']).get(str(vmid))
        if row is None:
            raise ValueError('Fusion VM is not in the configured inventory')
        argv = [self.config.get('vmrun', DEFAULT_VMRUN), '-T', 'fusion', '-gu', row['username'], '-gp', row['password'], operation, row['vmx'], *map(str,args)]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout or self.config['command_timeout'])
        except (OSError, subprocess.TimeoutExpired):
            # Never stringify the command: vmrun requires the password in argv.
            raise ValueError(f'Fusion {operation} unavailable or timed out') from None
        if result.returncode and operation == 'fileExistsInGuest' and any(message in (result.stdout+result.stderr).lower() for message in ('file does not exist', 'file not found')):
            return 'The file does not exist.'
        if result.returncode:
            error = result.stderr.replace(row['password'], '[REDACTED]')[-1000:]
            raise ValueError(f'Fusion {operation} failed: {error}')
        return result.stdout

    def call(self, vmid, op, *, timeout=None, **data):
        timeout = timeout or self.config['command_timeout']
        row = inventory(self.config['inventory_file']).get(str(vmid))
        if row is None:
            raise ValueError('Fusion VM is not in the configured inventory')
        payload = json.dumps(dict(data, op=op))
        if len(payload.encode()) > 1024*1024:
            raise ValueError('Guest RPC exceeds input limit')
        remote_dir = '/tmp/caf-fusion-' + uuid.uuid4().hex
        remote = remote_dir + '/request.json'
        self._run(vmid,'runProgramInGuest',self.config['guest_python'],'-c', 'import os,sys;os.mkdir(sys.argv[1],0o700)',remote_dir)
        # A single launch publishes an atomic reply. Poll copies never relaunch
        # the guest operation, even if vmrun dispatch returns slowly.
        wrapper = '''import json,os,subprocess,sys
os.umask(0o077)
source=sys.argv[1]
os.chmod(source,0o600)
request=json.load(open(source))
os.unlink(source)
try:
 root_code="import json,sys;line=sys.stdin.readline();d=json.loads(line if line.startswith('{') else sys.stdin.read());sys.argv=['guest_rpc',d['payload']];exec(compile(d['script'],'guest_rpc','exec'),{'__name__':'__main__'})"
 result=subprocess.run(['/usr/bin/sudo','-S','-p','',request['python'],'-c',root_code],input=request['password']+'\\n'+json.dumps({'script':request['script'],'payload':request['payload']}),text=True,capture_output=True,timeout=request['timeout'])
 reply={'exitcode':result.returncode,'out-data':result.stdout,'err-data':result.stderr.replace(request['password'],'[REDACTED]')}
except Exception:
 reply={'exitcode':1,'err-data':'Fusion guest RPC execution failed or timed out'}
with open(source+'.result.tmp','w') as f: json.dump(reply,f)
os.replace(source+'.result.tmp',source+'.result')
'''
        with tempfile.TemporaryDirectory(prefix='caf-fusion-') as directory:
            local = Path(directory)/'request.json'
            local.write_text(json.dumps(dict(script=self.script,payload=payload,password=row['password'],python=self.config['guest_python'],timeout=timeout)))
            local.chmod(0o600)
            launcher = Path(directory)/'rpc.py'; launcher.write_text(wrapper)
            try:
                self._run(vmid,'copyFileFromHostToGuest',local,remote)
                self._run(vmid,'copyFileFromHostToGuest',launcher,remote+'.py')
                self._run(vmid,'runProgramInGuest','-noWait',self.config['guest_python'],remote+'.py',remote)
                deadline=time.monotonic()+timeout+5
                result_path=Path(directory)/'result.json'
                while True:
                    exists=self._run(vmid,'fileExistsInGuest',remote+'.result').strip().lower()
                    self.qm(['guest','exec-status',vmid,remote], fusion_status={'exited':exists in ('', 'the file exists.')})
                    if exists in ('', 'the file exists.'):
                        self._run(vmid,'copyFileFromGuestToHost',remote+'.result',result_path)
                        result=json.loads(result_path.read_text())
                        if result['exitcode']:
                            raise ValueError(f'Guest operation {op} failed: '+result.get('out-data','')+' '+result.get('err-data',''))
                        try:
                            return json.loads(result.get('out-data',''))
                        except ValueError:
                            raise ValueError(f'Guest operation {op} returned invalid JSON') from None
                    if exists != 'the file does not exist.':
                        raise ValueError('Unrecognized Fusion file-existence response')
                    if time.monotonic()>=deadline:
                        raise ValueError(f'Guest operation {op} timed out; Fusion reply pending')
                    time.sleep(self.config['poll_seconds'])
            finally:
                for suffix in ('','.py','.result'):
                    try:self._run(vmid,'deleteFileInGuest',remote+suffix)
                    except ValueError:pass
                try:self._run(vmid,'deleteDirectoryInGuest',remote_dir)
                except ValueError:pass


class FusionBackend(ProxmoxBackend):
    def lock(self):
        from .runner import lease
        return lease(vm_lock_path(self.config,self.vmid))
