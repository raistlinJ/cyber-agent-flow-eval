import json
from pathlib import Path
import subprocess
import pytest
from cyber_agent_flow_eval import backends
from cyber_agent_flow_eval.fusion import FusionGuestAgent, FusionBackend, inventory, vm_lock_path
from cyber_agent_flow_eval.proxmox import GuestAgent


@pytest.fixture
def config(tmp_path):
    vmx=tmp_path/'Participant VM.vmx';vmx.touch()
    path=tmp_path/'inventory.json';path.write_text(json.dumps(dict(version=1,vms={'9403':dict(vmx=str(vmx),username='participant',password='secret-test')})));path.chmod(0o600)
    return backends.resolve_backend(dict(type='fusion',participant_vmid=9403,user='participant',inventory_file=str(path)))


def test_fusion_factory_and_host_lock(config):
    assert isinstance(GuestAgent(config), FusionGuestAgent)
    backend=backends.create_backend(dict(backend=config,engine={}))
    assert isinstance(backend,FusionBackend)
    assert vm_lock_path(config,9403).is_relative_to(Path.home())
    assert vm_lock_path(config,9403) != vm_lock_path(dict(type='proxmox'),9403)


def test_fusion_rpc_launches_once_and_collects_atomic_reply(config,monkeypatch):
    agent=GuestAgent(config);calls=[];polls=0
    def run(vmid,op,*args,**kwargs):
        nonlocal polls
        calls.append((op,args))
        if op=='fileExistsInGuest':
            polls+=1
            return 'The file does not exist.' if polls==1 else 'The file exists.'
        if op=='copyFileFromGuestToHost':
            Path(args[1]).write_text(json.dumps({'exitcode':0,'out-data':'{"ready":true}'}))
        if op=='copyFileFromHostToGuest' and str(args[0]).endswith('rpc.py'):
            compile(Path(args[0]).read_text(),'rpc.py','exec')
        return ''
    monkeypatch.setattr(agent,'_run',run)
    monkeypatch.setattr('cyber_agent_flow_eval.fusion.time.sleep',lambda _:None)
    assert agent.call(9403,'preflight',units=[])=={'ready':True}
    launches=[args for op,args in calls if op=='runProgramInGuest' and '-noWait' in args]
    assert len(launches)==1
    assert any(op=='deleteDirectoryInGuest' for op,args in calls)


def test_fusion_credentials_never_appear_in_timeout_errors(config,monkeypatch):
    def timeout(argv,**kwargs):raise subprocess.TimeoutExpired(argv,1)
    monkeypatch.setattr('cyber_agent_flow_eval.fusion.subprocess.run',timeout)
    with pytest.raises(ValueError,match='Fusion') as error:GuestAgent(config)._run(9403,'checkToolsState')
    assert 'secret-test' not in str(error.value)


def test_fusion_inventory_rejects_public_credentials(config):
    path=Path(config['inventory_file']);path.chmod(0o644)
    with pytest.raises(ValueError,match='owner-only'):inventory(path)


def test_fusion_wrapper_executes_large_stdin_rpc_without_argument_limit(config,monkeypatch):
    import os
    import shutil
    import sys
    agent=GuestAgent(config)
    agent.script="import json,sys; d=json.loads(sys.argv[1]); print(json.dumps({'size':len(d['content'])}))"
    real_run=subprocess.run
    def sudo_run(argv,**kwargs):
        assert argv[:3]==['/usr/bin/sudo','-S','-p']
        assert 'secret-test' not in ' '.join(argv)
        return real_run([sys.executable,'-c',argv[-1]],**kwargs)
    monkeypatch.setattr('cyber_agent_flow_eval.fusion.subprocess.run',sudo_run)
    def run(vmid,op,*args,**kwargs):
        if op=='runProgramInGuest' and '-noWait' not in args:
            os.mkdir(args[-1],0o700)
        elif op=='copyFileFromHostToGuest':shutil.copyfile(args[0],args[1])
        elif op=='runProgramInGuest':
            monkeypatch.setattr(sys,'argv',[args[-2],args[-1]])
            exec(compile(Path(args[-2]).read_text(),'rpc.py','exec'),{})
        elif op=='fileExistsInGuest':return 'The file exists.' if Path(args[0]).exists() else 'The file does not exist.'
        elif op=='copyFileFromGuestToHost':shutil.copyfile(args[0],args[1])
        elif op=='deleteFileInGuest':Path(args[0]).unlink(missing_ok=True)
        elif op=='deleteDirectoryInGuest':Path(args[0]).rmdir()
        return ''
    monkeypatch.setattr(agent,'_run',run)
    assert agent.call(9403,'write',content='x'*600000)=={'size':600000}


@pytest.mark.parametrize('fusion', [False, True])
def test_guest_agent_copy_preserves_transport_and_isolates_script(config, fusion):
    from copy import copy
    backend=config if fusion else dict(type='proxmox',command_timeout=5)
    authorize=lambda args: None
    agent=GuestAgent(backend,authorize=authorize)
    copied=copy(agent)
    assert type(copied) is type(agent)
    assert copied is not agent
    assert copied.config is agent.config
    assert copied.authorize is authorize
    original=agent.script
    copied.script='replacement capture helper'
    assert agent.script==original
    assert copied.script!=agent.script
