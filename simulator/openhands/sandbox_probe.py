"""Opt-in, no-model exercise of actual SDK tools and Docker isolation."""
import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from ..episode import save
from .container import SDKContainer


PROBE = r'''
import json
from pathlib import Path
from types import SimpleNamespace as NS
import subprocess
from openhands.sdk.tool.registry import resolve_tool
from openhands.tools.terminal.definition import TerminalAction
from openhands.tools.file_editor.definition import FileEditorAction
from simulator.openhands.remote_tools import remote_tool_specs
cfg=json.loads(Path('/inbox/config.json').read_text())
state=NS(workspace=NS(working_dir='/workspace/candidate'), agent=NS(llm=NS(vision_is_active=lambda:False)))
specs=remote_tool_specs(cfg['role'],**cfg['remote_tools'])
tools={s.name:resolve_tool(s,state)[0] for s in specs if s.name!='task_tracker'}
results=[]
def terminal(name,command,**kwargs):
    observation=tools['terminal'].executor(TerminalAction(command=command,**kwargs))
    value=observation.model_dump(mode='json');results.append(dict(case=name,observation=value));return value
def file(name,command,path,**kwargs):
    observation=tools['file_editor'].executor(FileEditorAction(command=command,path=path,**kwargs))
    value=observation.model_dump(mode='json');results.append(dict(case=name,observation=value));return value
def content(value):return '\n'.join(c.get('text','') for c in value.get('content',[]))
def exitcode(value):return value.get('exit_code') if value.get('exit_code') is not None else (value.get('metadata') or {}).get('exit_code')
try:
    sentinel=subprocess.Popen(['python','-c','import time;time.sleep(180)','CONTROL_PROCESS_SENTINEL'],env={'CONTROL_ENV_SENTINEL':'probe-only','PATH':'/usr/local/bin:/usr/bin:/bin'})
    x=terminal('identity',"id; python -c 'import importlib.util; print(importlib.util.find_spec(\"click\")); print(importlib.util.find_spec(\"openhands\"))'; test ! -r /etc/ssh/ssh_host_ed25519_key")
    assert exitcode(x)==0 and 'uid=1000' in content(x),x
    for path in ['/sdk/private-sentinel','/inbox/config.json','/mailbox','/transport/id_ed25519','/var/run/docker.sock','/proc/1/root/sdk/private-sentinel','/usr/local/lib/python3.12/site-packages/click/core.py']:
        x=file('hidden-file-'+path,'view',path);assert x['is_error'],x
    x=terminal('hidden-shell',"for p in /sdk/private-sentinel /inbox/config.json /transport/id_ed25519 /reference/answer.txt; do test ! -r \"$p\" || exit 9; done" if cfg['role']=='code' else "test ! -r /sdk/private-sentinel")
    assert exitcode(x)==0,x
    x=terminal('symlink',"ln -sf /proc/1/root/sdk/private-sentinel /tmp/hidden-link; cat /tmp/hidden-link")
    assert exitcode(x)!=0,x
    x=file('symlink-file','view','/tmp/hidden-link');assert x['is_error'],x
    x=terminal('network',"python - <<'PY'\nimport socket\nfor host,port in [('127.0.0.1',8789),('1.1.1.1',443),('host.docker.internal',8789)]:\n try:\n  socket.create_connection((host,port),timeout=1).close(); raise RuntimeError('reachable '+host)\n except (OSError,socket.timeout): pass\nprint('connections blocked')\nPY")
    assert exitcode(x)==0 and 'connections blocked' in content(x),x
    x=terminal('proc-control-sentinel',"python - <<'PY'\nfrom pathlib import Path\nimport os\nfor process in Path('/proc').glob('[0-9]*'):\n for name,needle in [('cmdline',b'CONTROL_PROCESS_SENTINEL'),('environ',b'CONTROL_ENV_SENTINEL')]:\n  try: data=(process/name).read_bytes()\n  except OSError: continue\n  assert needle not in data,'control process visible'\nfor fd in Path('/proc/self/fd').glob('*'):\n try: target=os.readlink(fd)\n except OSError: continue\n assert not target.startswith(('/sdk/','/transport/','/inbox/','/mailbox/'))\nprint('control sentinel absent; private descriptors absent')\nPY")
    assert exitcode(x)==0 and sentinel.poll() is None,x
    target='/workspace/candidate/probe.txt' if cfg['role']=='code' else '/workspace/checks/probe.txt'
    assert not file('create','create',target,file_text='one\ntwo\n')['is_error']
    assert file('create-no-overwrite','create',target,file_text='bad')['is_error']
    assert not file('replace','str_replace',target,old_str='one',new_str='ONE')['is_error']
    assert not file('insert','insert',target,insert_line=1,new_str='middle')['is_error']
    assert 'middle' in content(file('view','view',target,view_range=[1,-1]))
    assert not file('undo','undo_edit',target)['is_error']
    assert 'middle' not in content(file('undo-view','view',target))
    assert not file('directory-view','view','/workspace/candidate')['is_error']
    x=terminal('cwd-env','cd /tmp; export PROBE_VALUE=retained');assert exitcode(x)==0,x
    x=terminal('cwd-env-next','pwd; echo "$PROBE_VALUE"');assert '/tmp' in content(x) and 'retained' in content(x),x
    x=terminal('timeout','sleep 1; echo resumed',timeout=.1);assert exitcode(x)==-1,x
    x=terminal('continuation','',timeout=3);assert exitcode(x)==0 and 'resumed' in content(x),x
    x=terminal('timeout-input-continuation','sleep 1; echo input-resumed; false',timeout=.1);assert exitcode(x)==-1 and x['timeout'],x
    x=terminal('input-continuation','',is_input=True,timeout=3);assert exitcode(x)==1 and not x['timeout'] and 'input-resumed' in content(x),x
    x=terminal('input-wait',"read -r answer; printf 'received:%s\n' \"$answer\"",timeout=.1);assert exitcode(x)==-1,x
    terminal('input-text','hello',is_input=True)
    terminal('input-enter','ENTER',is_input=True)
    x=terminal('input-result','',is_input=True,timeout=3);assert exitcode(x)==0 and 'received:hello' in content(x),x
    x=terminal('background','sleep 1 & echo background-ok');assert exitcode(x)==0 and 'background-ok' in content(x),x
    x=terminal('long-output',"python -c 'print(\"z\"*60000)'");assert exitcode(x)==0 and '/workspace/tool-output/' in content(x) and '/sdk/' not in content(x),x
    if cfg['role']=='judge':
        assert file('judge-candidate-ro','create','/workspace/candidate/forbidden',file_text='bad')['is_error']
        assert file('judge-reference-ro','create','/reference/forbidden',file_text='bad')['is_error']
        x=terminal('judge-ro-shell','touch /workspace/candidate/forbidden /reference/forbidden');assert exitcode(x)!=0,x
        wrapper=tools['file_editor'].executor
        async def disconnect():
            wrapper.executor.ssh.conn.close()
            await wrapper.executor.ssh.conn.wait_closed()
        wrapper.executor.ssh.call(disconnect())
        failed=False
        try: file('closed-transport','view','/workspace/candidate/sentinel.txt')
        except Exception: failed=True
        assert failed and wrapper.marker.exists(),'transport failure was not retained'
        denied=False
        try: terminal('no-fallback-after-failure','touch /workspace/checks/unsafe-replayed')
        except RuntimeError: denied=True
        assert denied,'another executor continued after transport failure'
        results.append(dict(case='closed-transport-no-fallback',passed=True))
    result=dict(passed=True,role=cfg['role'],results=results)
except Exception as error:
    result=dict(passed=False,role=cfg['role'],error=repr(error),results=results)
finally:
    if 'sentinel' in globals():
        sentinel.terminate()
        sentinel.wait()
Path('/outbox/tool-probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(dict(passed=result['passed'],role=cfg['role'],cases=len(results))))
'''


def run(output, control_image, execution_image):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    agents = []
    try:
        for role in ('code', 'judge'):
            work = root/(role+'-workspace')
            (work/'candidate').mkdir(parents=True)
            (work/'candidate/sentinel.txt').write_text(role+' candidate')
            reference = root/'reference'
            reference.mkdir(exist_ok=True)
            (reference/'answer.txt').write_text('REFERENCE_ONLY_SENTINEL')
            config = dict(model='unused-no-model', base_url='http://invalid', key_env='UNUSED',
                          execution_backend='ssh_sandbox', execution_image=execution_image)
            agent = SDKContainer(root/role, work, config, control_image, role, None,
                                 time.monotonic()+600, reference=reference if role=='judge' else None,
                                 readonly_candidate=role=='judge')
            agents.append(agent)
            (root/role/'sdk/private-sentinel').write_text('CONTROL_ONLY_SENTINEL')
            agent.start()
            process = subprocess.run(['docker','exec','-i',agent.name,'python','-'],input=PROBE,text=True,capture_output=True,timeout=180)
            save(root/(role+'-process.json'),dict(returncode=process.returncode,stdout=process.stdout,stderr=process.stderr))
            result_path=root/role/'outbox/tool-probe.json'
            result=json.loads(result_path.read_text()) if result_path.exists() else dict(passed=False,error='probe failed before results')
            print(json.dumps(dict(role=role,passed=result['passed'],error=result.get('error'))),flush=True)
            if not result['passed']:
                return False
            if role=='code':
                # Stop only the controller; its paused execution container retains tmux.
                agent.close()
                agents.pop()
                resumed=SDKContainer(root/role,work,config,control_image,role,None,time.monotonic()+600)
                agents.append(resumed)
                resumed.start(resume=True)
                resume_probe=PROBE[:PROBE.index('\ntry:\n')] + '''
x=terminal('restart-cwd-env','pwd; echo "$PROBE_VALUE"')
assert exitcode(x)==0 and '/tmp' in content(x) and 'retained' in content(x), x
assert not file('restart-undo','undo_edit','/workspace/candidate/probe.txt')['is_error']
x=file('restart-undo-view','view','/workspace/candidate/probe.txt')
assert 'ONE' not in content(x) and 'one' in content(x),x
Path('/outbox/resume-probe.json').write_text(json.dumps(dict(passed=True,results=results)))
'''
                process=subprocess.run(['docker','exec','-i',resumed.name,'python','-'],input=resume_probe,text=True,capture_output=True,timeout=90)
                save(root/'resume-process.json',dict(returncode=process.returncode,stdout=process.stdout,stderr=process.stderr))
                if process.returncode:
                    print(json.dumps(dict(role=role,passed=False,error='safe resume probe failed')),flush=True)
                    return False
                freeze_probe=PROBE[:PROBE.index('\ntry:\n')] + '''
x=terminal('background-writer',"(while :; do printf 'x' >> /workspace/candidate/ticks; sleep .1; done) & echo $! > /workspace/tool-output/probe-writer.pid")
assert exitcode(x)==0,x
'''
                process=subprocess.run(['docker','exec','-i',resumed.name,'python','-'],input=freeze_probe,text=True,capture_output=True,timeout=90)
                save(root/'freeze-process.json',dict(returncode=process.returncode,stdout=process.stdout,stderr=process.stderr))
                if process.returncode:
                    return False
                time.sleep(.3)
                resumed.pause()
                ticks=work/'candidate/ticks'
                before=ticks.read_bytes()
                time.sleep(.5)
                frozen=ticks.read_bytes()
                resumed.unpause()
                time.sleep(.5)
                after=ticks.read_bytes()
                verdict=dict(passed=bool(before) and before==frozen and len(after)>len(frozen),
                             before_hash=hashlib.sha256(before).hexdigest(),
                             frozen_hash=hashlib.sha256(frozen).hexdigest(),
                             resumed_size=len(after))
                save(root/'background-freeze.json',verdict)
                if not verdict['passed']:
                    return False
                stop_probe=PROBE[:PROBE.index('\ntry:\n')] + '''
x=terminal('stop-background-writer','kill $(cat /workspace/tool-output/probe-writer.pid)')
assert exitcode(x)==0,x
x=terminal('explicit-reset','pwd; echo reset-ok',reset=True)
assert exitcode(x)==0 and '/workspace/candidate' in content(x) and 'reset-ok' in content(x),x
Path('/outbox/reset-probe.json').write_text(json.dumps(dict(passed=True,results=results)))
'''
                process=subprocess.run(['docker','exec','-i',resumed.name,'python','-'],input=stop_probe,text=True,capture_output=True,timeout=90)
                save(root/'reset-process.json',dict(returncode=process.returncode,stdout=process.stdout,stderr=process.stderr))
                if process.returncode:
                    return False
        isolated=(root/'code-workspace/candidate/sentinel.txt').read_text()=='code candidate'
        isolated=isolated and (root/'reference/answer.txt').read_text()=='REFERENCE_ONLY_SENTINEL'
        isolated=isolated and not (root/'judge-workspace/checks/unsafe-replayed').exists()
        save(root/'separate-workspaces.json',dict(passed=isolated))
        if not isolated:
            return False
        return True
    finally:
        for agent in reversed(agents):
            agent.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--control-image',required=True)
    parser.add_argument('--execution-image',required=True)
    args=parser.parse_args()
    raise SystemExit(0 if run(args.output,args.control_image,args.execution_image) else 2)
